from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import io
import zipfile
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Set

from fastapi import FastAPI, Body, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import httpx

from app.jellyfin_client import JellyfinClient
from app.store import Cache
from app.trakt_client import TraktService


JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").strip()
JELLYFIN_APIKEY = os.environ.get("JELLYFIN_APIKEY", "").strip()

WATCH_THRESHOLD = float(os.environ.get("WATCH_THRESHOLD", "0.95"))
REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "30"))
TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID", "").strip()
TRAKT_CLIENT_SECRET = os.environ.get("TRAKT_CLIENT_SECRET", "").strip()
TRAKT_STATE_PATH = os.environ.get("TRAKT_STATE_PATH", "trakt_accounts.json")
THUMB_CACHE_DIR = os.environ.get("THUMB_CACHE_DIR", "").strip()
THUMB_CACHE_TTL_HOURS = float(os.environ.get("THUMB_CACHE_TTL_HOURS", "72"))
PROXY_IMAGES = os.environ.get("PROXY_IMAGES", "true").strip().lower() in ("1", "true", "yes", "on")
IMAGE_CACHE_SECONDS = int(os.environ.get("IMAGE_CACHE_SECONDS", "86400"))


def _default_trakt_db_path() -> str:
    env_path = os.environ.get("TRAKT_DB_PATH", "").strip()
    if env_path:
        return env_path
    base_dir = os.path.dirname(TRAKT_STATE_PATH) or "."
    return os.path.join(base_dir, "trakt_sync.db")


def _default_jellyfin_state_path() -> str:
    env_path = os.environ.get("JELLYFIN_STATE_PATH", "").strip()
    if env_path:
        return env_path
    base_dir = os.path.dirname(TRAKT_STATE_PATH) or "."
    return os.path.join(base_dir, "jellyfin_state.json")


JELLYFIN_STATE_PATH = _default_jellyfin_state_path()
TRAKT_DB_PATH = _default_trakt_db_path()
if not THUMB_CACHE_DIR:
    base_dir = os.path.dirname(TRAKT_STATE_PATH) or "."
    THUMB_CACHE_DIR = os.path.join(base_dir, "thumb_cache")

if not (JELLYFIN_URL and JELLYFIN_APIKEY):
    raise RuntimeError("Missing required env vars: JELLYFIN_URL, JELLYFIN_APIKEY")

jellyfin = JellyfinClient(JELLYFIN_URL, JELLYFIN_APIKEY)
trakt_service = TraktService(TRAKT_CLIENT_ID, TRAKT_CLIENT_SECRET, TRAKT_STATE_PATH, TRAKT_DB_PATH)

cache = Cache()
thumb_cache_last_refresh = 0.0
app = FastAPI(title="Trakt Multi-Scrobbler")

app.mount("/static", StaticFiles(directory="static"), name="static")
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
app.mount("/thumbs", StaticFiles(directory=THUMB_CACHE_DIR), name="thumbs")

logger = logging.getLogger("trakt-multi-scrobbler")

selected_jellyfin_users: Set[str] = set()
jellyfin_selection_initialized = False


def _load_jellyfin_state() -> None:
    """Load persisted Jellyfin user selection for scrobbling."""
    global selected_jellyfin_users, jellyfin_selection_initialized
    if os.path.isdir(JELLYFIN_STATE_PATH):
        logger.warning("Jellyfin: state path %s is a directory; skipping load", JELLYFIN_STATE_PATH)
        selected_jellyfin_users = set()
        jellyfin_selection_initialized = False
        return
    if not os.path.exists(JELLYFIN_STATE_PATH):
        selected_jellyfin_users = set()
        jellyfin_selection_initialized = False
        return
    try:
        with open(JELLYFIN_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        selected = data.get("selected_users") or []
        selected_jellyfin_users = {str(u) for u in selected if str(u)}
        jellyfin_selection_initialized = bool(data.get("initialized", False))
    except Exception:
        logger.warning("Jellyfin: failed to read state file %s", JELLYFIN_STATE_PATH, exc_info=True)
        selected_jellyfin_users = set()
        jellyfin_selection_initialized = False


def _save_jellyfin_state() -> None:
    try:
        os.makedirs(os.path.dirname(JELLYFIN_STATE_PATH) or ".", exist_ok=True)
        with open(JELLYFIN_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {"selected_users": sorted(selected_jellyfin_users), "initialized": jellyfin_selection_initialized},
                f,
                indent=2,
            )
    except Exception:
        logger.warning("Jellyfin: failed to write state file %s", JELLYFIN_STATE_PATH, exc_info=True)


def _ensure_selection_initialized() -> None:
    """On first toggle, treat all current users as selected."""
    global jellyfin_selection_initialized, selected_jellyfin_users
    if jellyfin_selection_initialized:
        return
    selected_jellyfin_users = set(cache.users.keys())
    jellyfin_selection_initialized = True
    _save_jellyfin_state()


def _is_user_selected(user_id: str) -> bool:
    if not user_id:
        return False
    if not jellyfin_selection_initialized:
        return True
    return user_id in selected_jellyfin_users


_load_jellyfin_state()


def _provider_key(provider: str, ident: str) -> str:
    provider = provider.strip().lower()
    ident = ident.strip()
    if not provider or not ident:
        return ""
    return f"{provider}:{ident}"


def _provider_key_from_ids(ids: Dict[str, Any]) -> str:
    # Prefer TMDB, then IMDB, then TVDB
    if not ids:
        return ""
    tmdb = str(ids.get("Tmdb") or ids.get("tmdb") or "").strip()
    if tmdb:
        return _provider_key("tmdb", tmdb)
    imdb = str(ids.get("Imdb") or ids.get("imdb") or "").strip()
    if imdb:
        return _provider_key("imdb", imdb)
    tvdb = str(ids.get("Tvdb") or ids.get("tvdb") or "").strip()
    if tvdb:
        return _provider_key("tvdb", tvdb)
    return ""


def _catalog_entry_for_key(key: str) -> Dict[str, Any]:
    k = (key or "").strip()
    if not k:
        return {}
    if k in cache.catalog:
        return cache.catalog[k]
    for _, meta in cache.catalog.items():
        if k == meta.get("providerKey") or k == meta.get("groupKey"):
            return meta
    return {}


def _rule_key_for_entry(entry: Dict[str, Any]) -> str:
    """Determine the rule key for a catalog entry (mirrors /api/trakt/items/set logic)."""
    if not entry:
        return ""
    typ = str(entry.get("type") or "").lower()
    gk = str(entry.get("groupKey") or "").strip()
    pk = str(entry.get("providerKey") or "").strip()
    key = pk or gk
    if typ == "show" and gk:
        key = gk
    elif not key:
        key = gk or pk
    return key


async def _cache_thumb(url: str, force: bool = False) -> str:
    """Cache a remote thumbnail locally and return the local URL."""
    if not url:
        return url
    try:
        os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
    except Exception:
        return url
    fname = hashlib.sha256(url.encode("utf-8")).hexdigest() + ".jpg"
    fpath = os.path.join(THUMB_CACHE_DIR, fname)
    ttl_seconds = max(THUMB_CACHE_TTL_HOURS, 0) * 3600
    now = time.time()
    if os.path.exists(fpath) and not force:
        try:
            if ttl_seconds <= 0 or (now - os.path.getmtime(fpath)) < ttl_seconds:
                return f"/thumbs/{fname}"
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            with open(fpath, "wb") as dst:
                dst.write(r.content)
            os.utime(fpath, (now, now))
            return f"/thumbs/{fname}"
    except Exception:
        return url


def _backup_sources() -> List[Dict[str, str]]:
    files: List[Dict[str, str]] = []
    candidates = [
        {"path": TRAKT_STATE_PATH, "name": os.path.basename(TRAKT_STATE_PATH) or "trakt_accounts.json"},
        {"path": TRAKT_DB_PATH, "name": os.path.basename(TRAKT_DB_PATH) or "trakt_sync.db"},
        {"path": JELLYFIN_STATE_PATH, "name": os.path.basename(JELLYFIN_STATE_PATH) or "jellyfin_state.json"},
    ]
    seen_names: Set[str] = set()
    for c in candidates:
        path = c["path"]
        name = c["name"]
        if not path or not os.path.exists(path) or os.path.isdir(path):
            continue
        # Avoid duplicate basenames.
        if name in seen_names:
            base, ext = os.path.splitext(name)
            suffix = 1
            alt_name = f"{base}_{suffix}{ext}"
            while alt_name in seen_names:
                suffix += 1
                alt_name = f"{base}_{suffix}{ext}"
            name = alt_name
        seen_names.add(name)
        files.append({"path": path, "name": name})
    return files


def _thumb_cache_status() -> Dict[str, Any]:
    files = 0
    size = 0
    if os.path.isdir(THUMB_CACHE_DIR):
        try:
            for entry in os.scandir(THUMB_CACHE_DIR):
                if entry.is_file():
                    files += 1
                    try:
                        size += entry.stat().st_size
                    except Exception:
                        pass
        except Exception:
            pass
    return {
        "files": files,
        "size": size,
        "lastRefresh": thumb_cache_last_refresh,
        "ttlHours": THUMB_CACHE_TTL_HOURS,
    }


def _ts_from_iso(val: str) -> float:
    if not val:
        return 0.0
    try:
        cleaned = val.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).timestamp()
    except Exception:
        return 0.0


def _jellyfin_thumb(item_id: str, tag: str) -> str:
    if not (item_id and tag):
        return ""
    if PROXY_IMAGES:
        return f"/image/{item_id}?tag={tag}"
    if not (JELLYFIN_URL and JELLYFIN_APIKEY):
        return ""
    return f"{JELLYFIN_URL}/Items/{item_id}/Images/Primary?tag={tag}&X-Emby-Token={JELLYFIN_APIKEY}"


def _record_history(user_id: str, event: Dict[str, Any]) -> None:
    cache.user_history.setdefault(user_id, []).append(event)


def _gather_completed_events() -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for user_id, evs in cache.user_history.items():
        if not _is_user_selected(user_id):
            continue
        for ev in evs:
            if not ev.get("completed") or not ev.get("date"):
                continue
            events.append(ev)
    events.sort(key=lambda e: float(e.get("date") or 0.0))
    return events


def _recent_completed_events(limit: int = 5) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for user_id, evs in cache.user_history.items():
        if not _is_user_selected(user_id):
            continue
        user_name = cache.users.get(user_id, "")
        for ev in evs:
            if not ev.get("completed") or not ev.get("date"):
                continue
            out = dict(ev)
            out["userId"] = user_id
            out["userName"] = user_name
            items.append(out)
    items.sort(key=lambda e: float(e.get("date") or 0.0), reverse=True)
    return items[:limit]


async def refresh_cache(force: bool = False, recache_thumbs: bool = False) -> None:
    """Pull users + history from Jellyfin only."""
    global thumb_cache_last_refresh
    if not force and not cache.is_stale(REFRESH_MINUTES):
        return

    cache.users.clear()
    cache.user_history.clear()
    cache.catalog.clear()

    try:
        jf_users = await jellyfin.get_users()
        for ju in jf_users or []:
            juid = str(ju.get("Id") or ju.get("id") or "").strip()
            jname = (ju.get("Name") or ju.get("Username") or juid).strip()
            if juid:
                cache.users[juid] = jname
    except Exception:
        logger.warning("Jellyfin: failed to fetch users", exc_info=True)
        return

    for juid, _ in cache.users.items():
        try:
            items_resp = await jellyfin.get_user_items(juid)
        except Exception:
            logger.warning("Jellyfin: failed to fetch items for user %s", juid, exc_info=True)
            continue

        items = items_resp.get("Items", []) if isinstance(items_resp, dict) else (items_resp or [])
        for it in items:
            series_thumb_url = ""
            show_id = ""
            season_id = ""
            typ = (it.get("Type") or "").lower()
            if typ not in ("movie", "episode"):
                continue

            item_id = str(it.get("Id") or "").strip()
            if not item_id:
                continue

            primary_tag = it.get("PrimaryImageTag") or (it.get("ImageTags") or {}).get("Primary") or ""
            series_primary_tag = it.get("SeriesPrimaryImageTag") or ""
            thumb_url = ""
            if typ == "episode":
                show_id = str(it.get("SeriesId") or "").strip()
                if series_primary_tag and show_id:
                    # Prefer the show poster for episodes instead of episode stills.
                    series_thumb_url = _jellyfin_thumb(show_id, series_primary_tag)
                    thumb_url = series_thumb_url
                elif primary_tag:
                    thumb_url = _jellyfin_thumb(item_id, primary_tag)
            else:
                if primary_tag:
                    thumb_url = _jellyfin_thumb(item_id, primary_tag)

            ud = it.get("UserData", {}) or {}
            played_pct = 0.0
            try:
                played_pct = float(ud.get("PlayedPercentage", 0.0)) / 100.0
            except Exception:
                played_pct = 0.0
            is_completed = bool(ud.get("Played")) or played_pct >= WATCH_THRESHOLD
            date_ts = _ts_from_iso(ud.get("LastPlayedDate")) if isinstance(ud, dict) else 0.0

            show_id = str(it.get("SeriesId") or "").strip()
            season_id = str(it.get("ParentId") or it.get("SeasonId") or "").strip()
            if series_primary_tag and show_id and not series_thumb_url:
                series_thumb_url = _jellyfin_thumb(show_id, series_primary_tag)

            thumb_url = await _cache_thumb(thumb_url, force=recache_thumbs)
            series_thumb_url = await _cache_thumb(series_thumb_url, force=recache_thumbs)

            provider_key = _provider_key_from_ids(it.get("ProviderIds", {}))
            group_key = item_id if typ == "movie" else show_id
            catalog_key = group_key or provider_key
            if catalog_key:
                cache.catalog.setdefault(catalog_key, {
                    "groupKey": group_key,
                    "providerKey": provider_key,
                    "type": "movie" if typ == "movie" else "show",
                    "title": it.get("Name") if typ == "movie" else (it.get("SeriesName") or ""),
                    "year": str(it.get("ProductionYear") or ""),
                    "thumb": thumb_url or series_thumb_url,
                })

            event = {
                "source": "jellyfin",
                "type": typ,
                "ratingKey": item_id,
                "providerKey": _provider_key_from_ids(it.get("ProviderIds", {})),
                "percent": played_pct,
                "completed": is_completed,
                "date": date_ts,
                "title": it.get("Name") or "",
                "year": it.get("ProductionYear") or "",
                "seriesName": it.get("SeriesName") or "",
                "seasonName": it.get("SeasonName") or "",
                "episodeTitle": it.get("Name") or "",
                "episodeIndex": it.get("IndexNumber"),
                "seasonIndex": it.get("ParentIndexNumber"),
                "seriesId": show_id or "",
                "seasonId": season_id or "",
                "episodeId": item_id,
                "jellyfinId": item_id,
                "seriesThumb": series_thumb_url or thumb_url,
                "thumb": thumb_url,
                "groupKey": group_key,
            }
            _record_history(juid, event)

    cache.last_refresh_ts = time.time()
    thumb_cache_last_refresh = cache.last_refresh_ts

    if jellyfin_selection_initialized:
        removed = {uid for uid in selected_jellyfin_users if uid not in cache.users}
        if removed:
            selected_jellyfin_users.difference_update(removed)
            _save_jellyfin_state()

    # Prune Trakt per-item rules that refer to removed content.
    if trakt_service:
        valid_keys: set[str] = set()
        for v in cache.catalog.values():
            if v.get("providerKey"):
                valid_keys.add(v["providerKey"])
            if v.get("groupKey"):
                valid_keys.add(v["groupKey"])
        trakt_service.prune_rules(valid_keys)


async def sync_trakt(usernames: List[str] | None = None) -> Dict[str, Any]:
    """Push completed Jellyfin events to enabled Trakt accounts (optionally filtered)."""
    if not trakt_service or not trakt_service.ready:
        return {"ok": False, "error": "trakt_not_configured"}
    events = _gather_completed_events()
    return await trakt_service.sync_events(events, usernames=usernames)


@app.on_event("startup")
async def _startup() -> None:
    # Refresh on boot, then keep a background refresh loop.
    await refresh_cache(force=True)

    async def loop():
        while True:
            try:
                await refresh_cache(force=False)
                if trakt_service and trakt_service.ready:
                    await sync_trakt()
            except Exception:
                # Keep the service running even if a refresh fails once.
                pass
            await asyncio.sleep(REFRESH_MINUTES * 60)

    asyncio.create_task(loop())


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/image/{item_id}")
async def image_proxy(item_id: str, tag: str):
    """Proxy Jellyfin images to avoid mixed-content or private-host issues."""
    if not tag:
        return JSONResponse({"error": "tag is required"}, status_code=400)

    url = f"{JELLYFIN_URL}/Items/{item_id}/Images/Primary"
    headers = {"X-Emby-Token": JELLYFIN_APIKEY}
    params = {"tag": tag}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=headers, params=params)
            r.raise_for_status()
    except Exception as exc:
        logger.warning("Image proxy failed for %s (%s): %s", item_id, tag, exc)
        return JSONResponse({"error": "image fetch failed"}, status_code=502)

    media_type = r.headers.get("content-type", "image/jpeg")
    resp = Response(content=r.content, media_type=media_type)
    resp.headers["Cache-Control"] = f"public, max-age={IMAGE_CACHE_SECONDS}"
    return resp


@app.get("/trakt/{username}", response_class=HTMLResponse)
async def trakt_account_page(username: str):
    try:
        with open("static/account.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse(content="Account page missing.", status_code=500)


@app.get("/api/summary")
async def summary():
    await refresh_cache(force=False)
    movies = sum(1 for v in cache.catalog.values() if v.get("type") == "movie")
    shows = sum(1 for v in cache.catalog.values() if v.get("type") == "show")
    selected_count = len([uid for uid in cache.users.keys() if _is_user_selected(uid)])
    total_users = len(cache.users.keys())
    return JSONResponse(
        {
            "users": selected_count,
            "selectedUsers": selected_count,
            "totalUsers": total_users,
            "lastRefresh": cache.last_refresh_ts,
            "traktConfigured": bool(trakt_service and trakt_service.ready),
            "movies": movies,
            "shows": shows,
            "traktAccounts": [
                {"username": u, "enabled": acc.enabled}
                for u, acc in (trakt_service.accounts.items() if trakt_service else [])
            ],
        }
    )


@app.get("/api/trakt/accounts")
async def api_trakt_accounts():
    """List configured Trakt accounts and their enable/disable state."""
    if not trakt_service:
        return JSONResponse({"accounts": [], "configured": False, "error": "missing_client_id"}, status_code=200)
    return JSONResponse({"accounts": trakt_service.list_accounts(), "configured": trakt_service.ready})


@app.get("/api/trakt/accounts/{username}/items")
async def api_trakt_account_items(username: str):
    """Return enabled content for a specific Trakt account."""
    await refresh_cache(force=False)
    if not trakt_service or username not in trakt_service.accounts:
        return JSONResponse({"ok": False, "error": "unknown_account"}, status_code=404)

    rules = trakt_service.enabled_items(username) if trakt_service else {}
    enabled_keys = [k for k, v in rules.items() if v]
    items: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    seen_canonical: Dict[str, Dict[str, Any]] = {}

    for key in enabled_keys:
        meta = _catalog_entry_for_key(key)
        if meta:
            entry = dict(meta)
            entry["ruleKey"] = key
            canonical = ""
            if entry.get("type") == "show" and entry.get("groupKey"):
                canonical = str(entry.get("groupKey"))
            elif entry.get("providerKey"):
                canonical = str(entry.get("providerKey"))
            else:
                canonical = key
            if canonical in seen_canonical:
                # Prefer the groupKey rule for shows if duplicated.
                existing = seen_canonical[canonical]
                if entry.get("type") == "show" and entry.get("groupKey") == key:
                    seen_canonical[canonical] = entry
                continue
            seen_canonical[canonical] = entry
        else:
            missing.append({"ruleKey": key})

    items_sorted = sorted(seen_canonical.values(), key=lambda x: (x.get("type") or "", x.get("title") or ""))
    movies = [i for i in items_sorted if i.get("type") == "movie"]
    shows = [i for i in items_sorted if i.get("type") == "show"]

    # Build blocked list: catalog items not allowed for this account (rule false or no rule).
    enabled_set = {k for k in enabled_keys}
    seen_blocked: set[str] = set()
    for _, meta in cache.catalog.items():
        key = _rule_key_for_entry(meta)
        if not key or key in enabled_set:
            continue
        allowed = bool(rules.get(key)) if key in rules else False
        if allowed:
            continue
        if key in seen_blocked:
            continue
        entry = dict(meta)
        entry["ruleKey"] = key
        blocked.append(entry)
        seen_blocked.add(key)

    return JSONResponse(
        {
            "ok": True,
            "username": username,
            "enabled": trakt_service.accounts.get(username).enabled if trakt_service else False,
            "lastSynced": trakt_service.last_synced.get(username, 0.0),
            "items": items_sorted,
            "counts": {"movies": len(movies), "shows": len(shows)},
            "missing": missing,
            "blocked": sorted(blocked, key=lambda x: (x.get("type") or "", x.get("title") or "")),
            "traktConfigured": bool(trakt_service and trakt_service.ready),
            "catalogCount": len(cache.catalog),
        }
    )


@app.post("/api/trakt/accounts/toggle")
async def api_toggle_trakt_account(payload: Dict[str, Any] = Body(...)):
    if not trakt_service or not trakt_service.ready:
        return JSONResponse({"ok": False, "error": "trakt_not_configured"}, status_code=400)
    username = str(payload.get("username") or "").strip()
    enabled = bool(payload.get("enabled", True))
    if not username:
        return JSONResponse({"ok": False, "error": "missing_username"}, status_code=400)
    ok = trakt_service.set_enabled(username, enabled)
    return JSONResponse({"ok": ok})


@app.post("/api/trakt/accounts/delete")
async def api_delete_trakt_account(payload: Dict[str, Any] = Body(...)):
    if not trakt_service:
        return JSONResponse({"ok": False, "error": "trakt_not_configured"}, status_code=400)
    username = str(payload.get("username") or "").strip()
    if not username:
        return JSONResponse({"ok": False, "error": "missing_username"}, status_code=400)
    ok = trakt_service.remove_account(username)
    return JSONResponse({"ok": ok})


@app.post("/api/trakt/sync")
async def api_trakt_sync():
    await refresh_cache(force=False)
    result = await sync_trakt()
    return JSONResponse(result)


@app.post("/api/trakt/accounts/{username}/sync")
async def api_trakt_sync_account(username: str):
    await refresh_cache(force=False)
    if not trakt_service or username not in (trakt_service.accounts or {}):
        return JSONResponse({"ok": False, "error": "unknown_account"}, status_code=404)
    result = await sync_trakt([username])
    per_account = (result.get("results") or {}).get(username) if isinstance(result, dict) else None
    if per_account is None:
        return JSONResponse({"ok": False, "error": "no_result"}, status_code=400)
    return JSONResponse({"ok": True, "result": per_account})


@app.get("/api/backup")
async def api_backup():
    files = _backup_sources()
    manifest = {
        "timestamp": time.time(),
        "files": [{"name": f["name"], "path": f["path"]} for f in files],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for f in files:
            try:
                zf.write(f["path"], arcname=f["name"])
            except Exception:
                # If one file fails, continue with others.
                pass
    buf.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="trakt-multi-scrobbler-backup.zip"'}
    return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)


@app.post("/api/backup/restore")
async def api_restore(file: UploadFile = File(...)):
    if not file:
        return JSONResponse({"ok": False, "error": "missing_file"}, status_code=400)
    raw = await file.read()
    restored: List[str] = []
    try:
        buf = io.BytesIO(raw)
        with zipfile.ZipFile(buf, "r") as zf:
            for info in zf.infolist():
                name = os.path.basename(info.filename)
                if not name:
                    continue
                dest = None
                for candidate in (TRAKT_STATE_PATH, TRAKT_DB_PATH, JELLYFIN_STATE_PATH):
                    if name == os.path.basename(candidate):
                        dest = candidate
                        break
                if not dest:
                    continue
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                restored.append(dest)
    except Exception:
        return JSONResponse({"ok": False, "error": "restore_failed"}, status_code=400)

    # Reload in-memory state after restore.
    _load_jellyfin_state()
    if trakt_service:
        trakt_service._load_state()
        trakt_service._load_sync_state()

    return JSONResponse({"ok": True, "restored": restored})


@app.get("/api/thumbs/status")
async def api_thumb_status():
    return JSONResponse({"ok": True, **_thumb_cache_status()})


@app.post("/api/thumbs/refresh")
async def api_thumb_refresh():
    await refresh_cache(force=True, recache_thumbs=True)
    return JSONResponse({"ok": True, **_thumb_cache_status()})


@app.post("/api/trakt/device/start")
async def api_trakt_device_start():
    if not trakt_service:
        return JSONResponse({"ok": False, "error": "trakt_not_configured"}, status_code=400)
    ok, data = await trakt_service.start_device_flow()
    return JSONResponse({"ok": ok, **data}, status_code=200 if ok else 400)


@app.post("/api/trakt/device/poll")
async def api_trakt_device_poll(payload: Dict[str, Any] = Body(...)):
    if not trakt_service:
        return JSONResponse({"status": "error", "error": "trakt_not_configured"}, status_code=400)
    device_code = str(payload.get("device_code") or "").strip()
    if not device_code:
        return JSONResponse({"status": "error", "error": "missing_device_code"}, status_code=400)
    status, data = await trakt_service.poll_device_flow(device_code)
    return JSONResponse({"status": status, **data})


@app.get("/api/trakt/items")
async def api_trakt_items():
    """List Jellyfin titles (movie/show) with per-account scrobble flag."""
    await refresh_cache(force=False)
    items = sorted(cache.catalog.values(), key=lambda x: (x.get("type", ""), x.get("title", "")))
    account_pairs = list(trakt_service.accounts.items()) if trakt_service else []
    resp: List[Dict[str, Any]] = []
    for it in items:
        pk = it.get("providerKey") or ""
        gk = it.get("groupKey") or ""
        if not (pk or gk):
            continue
        entry = dict(it)
        entry["accounts"] = [
            {
                "username": u,
                # ruleEnabled = user choice; accountEnabled = current account toggle
                "ruleEnabled": trakt_service.item_allowed(u, pk, gk) if trakt_service else False,
                "enabled": acc.enabled,
                "accountEnabled": acc.enabled,
            }
            for u, acc in account_pairs
        ]
        resp.append(entry)
    return JSONResponse({"items": resp, "accounts": [u for u, _ in account_pairs]})


@app.post("/api/trakt/items/set")
async def api_trakt_items_set(payload: Dict[str, Any] = Body(...)):
    if not trakt_service or not trakt_service.ready:
        return JSONResponse({"ok": False, "error": "trakt_not_configured"}, status_code=400)
    pk = str(payload.get("providerKey") or "").strip()
    gk = str(payload.get("groupKey") or "").strip()
    username = str(payload.get("username") or "").strip()
    typ = str(payload.get("type") or "").lower().strip()
    enabled = bool(payload.get("enabled", True))
    if not (pk or gk) or not username:
        return JSONResponse({"ok": False, "error": "missing_params"}, status_code=400)
    key = pk or gk
    # Prefer grouping by series id for shows so future episodes inherit the rule.
    if typ == "show" and gk:
        key = gk
    elif not key:
        key = gk or pk
    ok = trakt_service.set_item_rule(username, key, enabled)
    return JSONResponse({"ok": ok})


@app.post("/api/trakt/accounts/{username}/items/remove")
async def api_trakt_account_items_remove(username: str, payload: Dict[str, Any] = Body(...)):
    """Remove a specific Trakt rule for an account (by rule key)."""
    if not trakt_service or not trakt_service.ready:
        return JSONResponse({"ok": False, "error": "trakt_not_configured"}, status_code=400)
    rule_key = str(payload.get("ruleKey") or payload.get("rule_key") or "").strip()
    if not rule_key:
        return JSONResponse({"ok": False, "error": "missing_rule_key"}, status_code=400)
    if username not in trakt_service.accounts:
        return JSONResponse({"ok": False, "error": "unknown_account"}, status_code=404)
    ok = trakt_service.remove_item_rule(username, rule_key)
    if not ok:
        return JSONResponse({"ok": False, "error": "rule_not_found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/api/users")
async def api_users():
    """Return all known Jellyfin users."""
    await refresh_cache(force=False)
    return JSONResponse(
        {
            "users": [{"user_id": uid, "name": name, "enabled": _is_user_selected(uid)} for uid, name in cache.users.items()],
            "initialized": jellyfin_selection_initialized,
        }
    )


@app.post("/api/users/toggle")
async def api_toggle_user(payload: Dict[str, Any] = Body(...)):
    """Enable/disable a Jellyfin user as a scrobble source."""
    await refresh_cache(force=False)
    user_id = str(payload.get("user_id") or payload.get("userId") or "").strip()
    enabled = bool(payload.get("enabled", True))
    if not user_id:
        return JSONResponse({"ok": False, "error": "missing_user_id"}, status_code=400)
    if user_id not in cache.users:
        return JSONResponse({"ok": False, "error": "unknown_user"}, status_code=404)
    _ensure_selection_initialized()
    if enabled:
        selected_jellyfin_users.add(user_id)
    else:
        selected_jellyfin_users.discard(user_id)
    _save_jellyfin_state()
    return JSONResponse(
        {
            "ok": True,
            "enabled": enabled,
            "selected": list(selected_jellyfin_users),
            "initialized": jellyfin_selection_initialized,
        }
    )


@app.get("/api/recent")
async def api_recent():
    """Return recent completed items across selected Jellyfin users."""
    await refresh_cache(force=False)
    items = _recent_completed_events(limit=6)
    return JSONResponse({"items": items})


@app.post("/api/refresh")
async def api_force_refresh():
    """Force a cache refresh (useful after library changes)."""
    await refresh_cache(force=True)
    return JSONResponse({"ok": True, "lastRefresh": cache.last_refresh_ts})


@app.get("/api/user/{user_id}/items")
async def user_items(user_id: str):
    """Return movies and show progress for a single user (also non-complete)."""
    await refresh_cache(force=False)

    if user_id not in cache.users:
        return JSONResponse({"error": "user not found"}, status_code=404)

    events = cache.user_history.get(user_id, [])
    # Keep only latest per item id for progress view.
    latest: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        rk = ev.get("ratingKey")
        if not rk:
            continue
        prev = latest.get(rk, {})
        if not prev or float(ev.get("date") or 0) > float(prev.get("date") or 0):
            latest[rk] = ev
    movies_resp = [v for v in latest.values() if v.get("type") == "movie"]
    shows_resp = [v for v in latest.values() if v.get("type") == "episode"]

    return JSONResponse({"movies": movies_resp, "shows": shows_resp})


@app.get("/api/user/{user_id}/history")
async def user_history(user_id: str):
    """Return full watched history (Jellyfin)."""
    await refresh_cache(force=False)

    if user_id not in cache.users:
        return JSONResponse({"error": "user not found"}, status_code=404)

    events = cache.user_history.get(user_id, [])
    enriched: List[Dict[str, Any]] = []

    for ev in events:
        out = dict(ev)
        if not out.get("title") and out.get("episodeTitle"):
            out["title"] = out.get("episodeTitle")
        enriched.append(out)

    def _sort_key(e: Dict[str, Any]):
        try:
            return float(e.get("date") or 0)
        except Exception:
            return 0.0

    enriched_sorted = sorted(enriched, key=_sort_key, reverse=True)
    return JSONResponse({"items": enriched_sorted})
