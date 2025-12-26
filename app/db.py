from __future__ import annotations

import os
import sqlite3
from typing import Dict, Iterable


class SyncStore:
    """SQLite-backed storage for Trakt sync state."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_settings (
                    username TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    last_synced REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_items (
                    username TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    PRIMARY KEY (username, item_key),
                    FOREIGN KEY (username) REFERENCES account_settings(username) ON DELETE CASCADE
                )
                """
            )
            conn.commit()

    def load_account_settings(self) -> Dict[str, Dict[str, float | bool]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT username, enabled, last_synced FROM account_settings")
            return {
                row["username"]: {"enabled": bool(row["enabled"]), "last_synced": float(row["last_synced"] or 0.0)}
                for row in cur.fetchall()
            }

    def ensure_account(self, username: str, enabled: bool = False, last_synced: float = 0.0) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account_settings (username, enabled, last_synced)
                VALUES (?, ?, ?)
                ON CONFLICT(username) DO NOTHING
                """,
                (username, 1 if enabled else 0, float(last_synced or 0.0)),
            )
            conn.commit()

    def set_enabled(self, username: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account_settings (username, enabled, last_synced)
                VALUES (?, ?, 0)
                ON CONFLICT(username) DO UPDATE SET enabled=excluded.enabled
                """,
                (username, 1 if enabled else 0),
            )
            conn.commit()

    def set_last_synced(self, username: str, ts: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account_settings (username, enabled, last_synced)
                VALUES (?, 0, ?)
                ON CONFLICT(username) DO UPDATE SET last_synced=excluded.last_synced
                """,
                (username, float(ts or 0.0)),
            )
            conn.commit()

    def remove_account(self, username: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM account_items WHERE username=?", (username,))
            conn.execute("DELETE FROM account_settings WHERE username=?", (username,))
            conn.commit()

    def load_account_items(self) -> Dict[str, Dict[str, bool]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT username, item_key, enabled FROM account_items")
            items: Dict[str, Dict[str, bool]] = {}
            for row in cur.fetchall():
                user = row["username"]
                items.setdefault(user, {})[row["item_key"]] = bool(row["enabled"])
            return items

    def set_item_rule(self, username: str, key: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account_settings (username, enabled, last_synced)
                VALUES (?, 0, 0)
                ON CONFLICT(username) DO NOTHING
                """,
                (username,),
            )
            conn.execute(
                """
                INSERT INTO account_items (username, item_key, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(username, item_key) DO UPDATE SET enabled=excluded.enabled
                    """,
                    (username, key, 1 if enabled else 0),
                )
            conn.commit()

    def remove_item_rule(self, username: str, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM account_items WHERE username=? AND item_key=?", (username, key))
            conn.commit()

    def import_account_items(self, items: Dict[str, Dict[str, bool]]) -> None:
        if not items:
            return
        with self._connect() as conn:
            for username, rules in items.items():
                conn.execute(
                    """
                    INSERT INTO account_settings (username, enabled, last_synced)
                    VALUES (?, 0, 0)
                    ON CONFLICT(username) DO NOTHING
                    """,
                    (username,),
                )
                for key, enabled in (rules or {}).items():
                    conn.execute(
                        """
                        INSERT INTO account_items (username, item_key, enabled)
                        VALUES (?, ?, ?)
                        ON CONFLICT(username, item_key) DO UPDATE SET enabled=excluded.enabled
                        """,
                        (username, key, 1 if enabled else 0),
                    )
            conn.commit()

    def prune_rules(self, valid_keys: Iterable[str]) -> None:
        keys = list(valid_keys or [])
        with self._connect() as conn:
            if not keys:
                conn.execute("DELETE FROM account_items")
                conn.commit()
                return
            placeholders = ",".join("?" * len(keys))
            conn.execute(f"DELETE FROM account_items WHERE item_key NOT IN ({placeholders})", keys)
            conn.commit()
