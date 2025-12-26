import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from app.db import SyncStore


logger = logging.getLogger("trakt")


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(tz=timezone.utc).isoformat()


def _trakt_ids(provider_key: str) -> Dict[str, str]:
    """Convert a providerKey string like 'tmdb:123' to a Trakt ids dict."""
    if not provider_key or ":" not in provider_key:
        return {}
    provider, ident = provider_key.split(":", 1)
    provider = provider.strip().lower()
    ident = ident.strip()
    if not provider or not ident:
        return {}
    if provider in ("tmdb", "imdb", "tvdb"):
        return {provider: ident}
    return {}


@dataclass
class TraktAccount:
    username: str
    access_token: str
    refresh_token: str
    expires_at: float
    enabled: bool = True

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "TraktAccount":
        return cls(
            username=str(data.get("username") or "").strip(),
            access_token=str(data.get("access_token") or "").strip(),
            refresh_token=str(data.get("refresh_token") or "").strip(),
            expires_at=float(data.get("expires_at") or 0.0),
            enabled=bool(data.get("enabled", True)),
        )

    def is_expired(self) -> bool:
        # Refresh slightly before expiration to be safe.
        return _now() > (self.expires_at - 60)


class TraktService:
    """Thin wrapper around Trakt's API for history sync."""

    def __init__(self, client_id: str, client_secret: str, state_path: str = "trakt_accounts.json", db_path: Optional[str] = None) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.state_path = state_path
        self.db_path = db_path or self._derive_db_path(state_path)
        self.accounts: Dict[str, TraktAccount] = {}
        self.last_synced: Dict[str, float] = {}
        # account_items: username -> key -> enabled(bool). Missing means allowed by default.
        self.account_items: Dict[str, Dict[str, bool]] = {}
        self._json_last_synced: Dict[str, float] = {}
        self._json_account_items: Dict[str, Dict[str, bool]] = {}
        self.store = SyncStore(self.db_path)
        self._load_state()
        self._load_sync_state()
        self._ensure_state_file()

    @property
    def ready(self) -> bool:
        return bool(self.client_id and self.client_secret and self.accounts)

    def _derive_db_path(self, state_path: str) -> str:
        base_dir = os.path.dirname(state_path) or "."
        return os.path.join(base_dir, "trakt_sync.db")

    def _load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        if os.path.isdir(self.state_path):
            logger.warning("Trakt: state path %s is a directory; skipping load", self.state_path)
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            logger.warning("Trakt: failed to read state file %s", self.state_path, exc_info=True)
            return
        accs = data.get("accounts") or []
        for a in accs:
            acc = TraktAccount.from_dict(a or {})
            if acc.username:
                self.accounts[acc.username] = acc
        self._json_last_synced = {str(k): float(v) for k, v in (data.get("last_synced") or {}).items()}
        self._json_account_items = {
            str(k): {str(pk): bool(vv) for pk, vv in (v or {}).items()} for k, v in (data.get("account_items") or {}).items()
        }

    def _load_sync_state(self) -> None:
        settings = self.store.load_account_settings()
        items = self.store.load_account_items()

        # Initial migration from JSON to SQLite.
        if not settings and (self._json_last_synced or self._json_account_items):
            for username, acc in self.accounts.items():
                self.store.ensure_account(username, enabled=acc.enabled, last_synced=self._json_last_synced.get(username, 0.0))
            self.store.import_account_items(self._json_account_items)
            settings = self.store.load_account_settings()
            items = self.store.load_account_items()

        # Ensure every account has a row in SQLite.
        for username, acc in self.accounts.items():
            if username not in settings:
                self.store.ensure_account(username, enabled=acc.enabled, last_synced=self._json_last_synced.get(username, 0.0))
        settings = self.store.load_account_settings()

        for username, acc in self.accounts.items():
            st = settings.get(username, {"enabled": False, "last_synced": 0.0})
            acc.enabled = bool(st.get("enabled", False))
            self.last_synced[username] = float(st.get("last_synced") or 0.0)

        self.account_items = items
        self._save_state()

    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        data = {
            "accounts": [
                {
                    "username": acc.username,
                    "access_token": acc.access_token,
                    "refresh_token": acc.refresh_token,
                    "expires_at": acc.expires_at,
                }
                for acc in self.accounts.values()
            ],
            "last_synced": self.last_synced,
        }
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            logger.warning("Trakt: failed to write state file %s", self.state_path, exc_info=True)

    def _ensure_state_file(self) -> None:
        if os.path.isdir(self.state_path):
            return
        if not os.path.exists(self.state_path):
            try:
                self._save_state()
            except Exception:
                logger.warning("Trakt: cannot create state file %s", self.state_path, exc_info=True)

    def list_accounts(self) -> List[Dict[str, object]]:
        return [
            {
                "username": acc.username,
                "enabled": acc.enabled,
                "expires_at": acc.expires_at,
                "last_synced_at": self.last_synced.get(acc.username, 0.0),
            }
            for acc in self.accounts.values()
        ]

    def set_enabled(self, username: str, enabled: bool) -> bool:
        acc = self.accounts.get(username)
        if not acc:
            return False
        acc.enabled = bool(enabled)
        self.store.set_enabled(username, acc.enabled)
        self._save_state()
        return True

    def remove_account(self, username: str) -> bool:
        if username not in self.accounts:
            return False
        self.accounts.pop(username, None)
        self.last_synced.pop(username, None)
        self.account_items.pop(username, None)
        self.store.remove_account(username)
        self._save_state()
        logger.info("Trakt: removed account %s", username)
        return True

    def set_item_rule(self, username: str, key: str, enabled: bool) -> bool:
        if not username or not key:
            return False
        if username not in self.accounts:
            return False
        rules = self.account_items.setdefault(username, {})
        rules[key] = bool(enabled)
        self.store.set_item_rule(username, key, enabled)
        self._save_state()
        return True

    def item_allowed(self, username: str, provider_key: str, group_key: str = "") -> bool:
        if not provider_key and not group_key:
            return False
        rules = self.account_items.get(username) or {}
        if provider_key and provider_key in rules:
            return bool(rules[provider_key])
        if group_key and group_key in rules:
            return bool(rules[group_key])
        return False  # default: new content not selected

    def prune_rules(self, valid_keys: set[str]) -> None:
        if not valid_keys:
            return
        changed = False
        for user, rules in list(self.account_items.items()):
            to_delete = [k for k in rules.keys() if k not in valid_keys]
            if to_delete:
                for k in to_delete:
                    rules.pop(k, None)
                changed = True
        if changed:
            self.store.prune_rules(valid_keys)
            self._save_state()

    async def _refresh_token(self, acc: TraktAccount) -> bool:
        if not acc.refresh_token or not self.client_id or not self.client_secret:
            return False
        payload = {
            "refresh_token": acc.refresh_token,
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post("https://api.trakt.tv/oauth/token", json=payload)
                r.raise_for_status()
                data = r.json()
        except Exception:
            logger.warning("Trakt: failed to refresh token for %s", acc.username, exc_info=True)
            return False

        acc.access_token = data.get("access_token") or acc.access_token
        acc.refresh_token = data.get("refresh_token") or acc.refresh_token
        expires_in = float(data.get("expires_in") or 0)
        acc.expires_at = _now() + expires_in
        self._save_state()
        logger.info("Trakt: refreshed token for %s", acc.username)
        return True

    async def _authorized_client(self, acc: TraktAccount) -> Optional[httpx.AsyncClient]:
        if acc.is_expired():
            ok = await self._refresh_token(acc)
            if not ok:
                return None
        headers = {
            "Authorization": f"Bearer {acc.access_token}",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
            "Content-Type": "application/json",
        }
        return httpx.AsyncClient(timeout=30.0, headers=headers)

    async def _post_history(self, acc: TraktAccount, payload: Dict[str, object]) -> Tuple[bool, Dict[str, object]]:
        if not payload:
            return True, {"added": {}}
        client = await self._authorized_client(acc)
        if client is None:
            return False, {"error": "auth_failed"}
        try:
            async with client as c:
                r = await c.post("https://api.trakt.tv/sync/history", json=payload)
                if r.status_code == 401:
                    # Try once more after refresh.
                    refreshed = await self._refresh_token(acc)
                    if not refreshed:
                        return False, {"error": "unauthorized"}
                    return await self._post_history(acc, payload)
                r.raise_for_status()
                return True, r.json()
        except Exception:
            logger.warning("Trakt: failed to sync history for %s", acc.username, exc_info=True)
            return False, {"error": "exception"}

    async def sync_events(self, events: List[Dict[str, object]]) -> Dict[str, object]:
        """Sync completed Jellyfin events to all enabled Trakt accounts.

        Events must include:
        - type: movie|episode
        - providerKey: provider:id (tmdb/imdb/tvdb)
        - date: unix timestamp
        """
        if not self.ready:
            return {"ok": False, "error": "trakt_not_configured"}

        events_sorted = sorted(events or [], key=lambda e: float(e.get("date") or 0.0))
        results: Dict[str, object] = {}

        for username, acc in self.accounts.items():
            if not acc.enabled:
                results[username] = {"skipped": True, "reason": "disabled"}
                continue

            last = self.last_synced.get(username, 0.0)
            movies: List[Dict[str, object]] = []
            episodes: List[Dict[str, object]] = []
            max_ts_sent = 0.0
            skipped_missing_ids = 0
            skipped_disallowed = 0
            sample_missing: List[Dict[str, str]] = []
            sample_disallowed: List[Dict[str, str]] = []
            for ev in events_sorted:
                ts = float(ev.get("date") or 0.0)
                if ts <= last or not ev.get("completed"):
                    continue
                ids = _trakt_ids(str(ev.get("providerKey") or ""))
                if not ids:
                    skipped_missing_ids += 1
                    if len(sample_missing) < 5:
                        sample_missing.append(
                            {
                                "title": str(ev.get("title") or ev.get("seriesName") or ""),
                                "providerKey": str(ev.get("providerKey") or ""),
                                "groupKey": str(ev.get("groupKey") or ""),
                            }
                        )
                    continue
                if not self.item_allowed(username, str(ev.get("providerKey") or ""), str(ev.get("groupKey") or "")):
                    skipped_disallowed += 1
                    if len(sample_disallowed) < 5:
                        sample_disallowed.append(
                            {
                                "title": str(ev.get("title") or ev.get("seriesName") or ""),
                                "providerKey": str(ev.get("providerKey") or ""),
                                "groupKey": str(ev.get("groupKey") or ""),
                            }
                        )
                    continue
                record = {"ids": ids, "watched_at": _iso(ts)}
                typ = str(ev.get("type") or "").lower()
                if typ == "movie":
                    movies.append(record)
                    max_ts_sent = max(max_ts_sent, ts)
                elif typ == "episode":
                    episodes.append(record)
                    max_ts_sent = max(max_ts_sent, ts)
            payload: Dict[str, object] = {}
            if movies:
                payload["movies"] = movies
            if episodes:
                payload["episodes"] = episodes
            ok, body = await self._post_history(acc, payload)
            if ok:
                if movies or episodes:
                    self.last_synced[username] = max_ts_sent or last
                    self.store.set_last_synced(username, self.last_synced[username])
                    self._save_state()
                results[username] = {
                    "ok": True,
                    "sent": {"movies": len(movies), "episodes": len(episodes)},
                    "skipped_missing_ids": skipped_missing_ids,
                    "skipped_disallowed": skipped_disallowed,
                    "samples_missing_ids": sample_missing,
                    "samples_disallowed": sample_disallowed,
                    "response": body,
                }
            else:
                results[username] = {"ok": False, "payload": payload, "response": body}
        return {"ok": True, "results": results}

    async def start_device_flow(self) -> Tuple[bool, Dict[str, object]]:
        """Kick off Trakt device flow."""
        if not self.client_id:
            return False, {"error": "missing_client_id"}
        payload = {"client_id": self.client_id}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post("https://api.trakt.tv/oauth/device/code", json=payload)
                r.raise_for_status()
                data = r.json()
                return True, data
        except Exception:
            logger.warning("Trakt: failed to start device flow", exc_info=True)
            return False, {"error": "device_flow_start_failed"}

    async def poll_device_flow(self, device_code: str) -> Tuple[str, Dict[str, object]]:
        """Poll Trakt device token endpoint.

        Returns status: "pending", "approved", "rejected", "error"
        """
        if not (self.client_id and self.client_secret and device_code):
            return "error", {"error": "missing_params"}
        payload = {
            "code": device_code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post("https://api.trakt.tv/oauth/device/token", json=payload)
                if r.status_code in (400, 404):
                    try:
                        data = r.json()
                    except Exception:
                        logger.warning("Trakt: device poll returned non-JSON (status %s)", r.status_code)
                        # Treat as pending if still within code validity window.
                        return "pending", {"error": "poll_not_json"}
                    if data.get("error") in ("authorization_pending", "slow_down"):
                        return "pending", data
                    if data.get("error") == "expired_token":
                        return "rejected", data
                r.raise_for_status()
                try:
                    data = r.json()
                except Exception:
                    logger.warning("Trakt: device poll returned non-JSON 200")
                    return "error", {"error": "poll_not_json"}
        except Exception:
            logger.warning("Trakt: device flow poll failed", exc_info=True)
            return "error", {"error": "poll_failed"}

        added, info = await self._add_account_from_tokens(data)
        if added:
            return "approved", info
        return "error", {"error": "add_account_failed"}

    async def _profile_username(self, access_token: str) -> Optional[str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
                r = await client.get("https://api.trakt.tv/users/me")
                r.raise_for_status()
                data = r.json()
                return str(data.get("username") or data.get("ids", {}).get("slug") or "").strip()
        except Exception:
            logger.warning("Trakt: failed to fetch profile username", exc_info=True)
            return None

    async def _add_account_from_tokens(self, data: Dict[str, object]) -> Tuple[bool, Dict[str, object]]:
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        expires_in = float(data.get("expires_in") or 0.0)
        if not access_token or not refresh_token or not expires_in:
            return False, {"error": "missing_tokens"}

        username = await self._profile_username(access_token)
        if not username:
            return False, {"error": "missing_username"}

        expires_at = _now() + expires_in
        acc = TraktAccount(
            username=username,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            enabled=False,  # start disabled until user enables + sets filters
        )
        self.accounts[username] = acc
        self.last_synced.setdefault(username, 0.0)
        self.store.ensure_account(username, enabled=acc.enabled, last_synced=self.last_synced.get(username, 0.0))
        self._save_state()
        logger.info("Trakt: added/updated account %s via device flow", username)
        return True, {"username": username, "expires_at": expires_at}

    def enabled_items(self, username: str) -> Dict[str, bool]:
        return dict(self.account_items.get(username, {}))

    def remove_item_rule(self, username: str, key: str) -> bool:
        if not username or not key:
            return False
        rules = self.account_items.get(username)
        if not rules or key not in rules:
            return False
        rules.pop(key, None)
        self.store.remove_item_rule(username, key)
        self._save_state()
        return True
