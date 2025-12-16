from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Any
import time


@dataclass
class Cache:
    """
    Minimal in-memory cache for Jellyfin users and watch history.
    """
    last_refresh_ts: float = 0.0
    users: Dict[str, str] = field(default_factory=dict)  # user_id -> friendly_name
    user_history: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)  # user_id -> events

    def is_stale(self, refresh_minutes: int) -> bool:
        if self.last_refresh_ts <= 0:
            return True
        return (time.time() - self.last_refresh_ts) > (refresh_minutes * 60)
