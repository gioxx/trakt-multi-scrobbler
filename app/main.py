from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Set

from fastapi import FastAPI, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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


def _default_jellyfin_state_path() -> str:
    env_path = os.environ.get("JELLYFIN_STATE_PATH", "").strip()
    if env_path:
        return env_path
    base_dir = os.path.dirname(TRAKT_STATE_PATH) or "."
    return os.path.join(base_dir, "jellyfin_state.json")


JELLYFIN_STATE_PATH = _default_jellyfin_state_path()

if not (JELLYFIN_URL and JELLYFIN_APIKEY):
    raise RuntimeError("Missing required env vars: JELLYFIN_URL, JELLYFIN_APIKEY")

jellyfin = JellyfinClient(JELLYFIN_URL, JELLYFIN_APIKEY)
trakt_service = TraktService(TRAKT_CLIENT_ID, TRAKT_CLIENT_SECRET, TRAKT_STATE_PATH)

cache = Cache()
app = FastAPI(title="Trakt Multi-Scrobbler")

app.mount("/static", StaticFiles(directory="static"), name="static")

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


def _ts_from_iso(val: str) -> float:
    if not val:
        return 0.0
    try:
        cleaned = val.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).timestamp()
    except Exception:
        return 0.0


def _jellyfin_thumb(item_id: str, tag: str) -> str:
    if not (JELLYFIN_URL and JELLYFIN_APIKEY and item_id and tag):
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


async def refresh_cache(force: bool = False) -> None:
    """Pull users + history from Jellyfin only."""
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


async def sync_trakt() -> Dict[str, Any]:
    """Push completed Jellyfin events to enabled Trakt accounts."""
    if not trakt_service or not trakt_service.ready:
        return {"ok": False, "error": "trakt_not_configured"}
    events = _gather_completed_events()
    return await trakt_service.sync_events(events)


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
        }
    )


@app.get("/api/trakt/accounts")
async def api_trakt_accounts():
    """List configured Trakt accounts and their enable/disable state."""
    if not trakt_service:
        return JSONResponse({"accounts": [], "configured": False, "error": "missing_client_id"}, status_code=200)
    return JSONResponse({"accounts": trakt_service.list_accounts(), "configured": trakt_service.ready})


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
