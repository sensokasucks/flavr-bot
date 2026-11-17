"""
Twitch for the go-live watcher: Helix with an app sign-in (TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
from dev.twitch.tv → Your Console → Register Your Application; no user sign-in needed).

Calls (all documented Helix endpoints):
  POST id.twitch.tv/oauth2/token            client_credentials → app token (about 60 days; renewed on 401)
  GET  helix/users?login=a&login=b          up to 100 per call: id, display name, picture
  GET  helix/streams?user_id=..&type=live   up to 100 per call: who is live, title, game, viewers, start
  GET  helix/schedule?broadcaster_id=       one person's upcoming segments (/schedule, the calendar
                                            every 6 h: flavrbot/watcher.py refresh_schedules)
  GET  helix/games?id=a&id=b                box art of the scheduled games (calendar tiles)
  GET  helix/videos?user_id=&type=archive   the newest VOD after a stream ends

Rate: Helix allows 800 points a minute for an app; the watcher uses one streams call per 100
people every TWITCH_EVERY_SEC (default 2 minutes).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp

from . import UA, Live

log = logging.getLogger("flavrbot.twitch")
HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


def iso_ts(s: str) -> Optional[float]:
    """"2026-11-17T19:00:00Z" → unix seconds (None when empty or unreadable)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def chunks(items: List[Any], n: int) -> List[List[Any]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


class Twitch:
    platform = "twitch"

    def __init__(self, http: aiohttp.ClientSession, client_id: str, client_secret: str):
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = ""
        self._token_until = 0.0

    @property
    def ready(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _app_token(self) -> str:
        if self._token and time.time() < self._token_until - 300:
            return self._token
        async with self.http.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                                   "grant_type": "client_credentials"}) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"Twitch sign-in refused (HTTP {r.status}): check TWITCH_CLIENT_ID / SECRET")
        self._token = str(data.get("access_token") or "")
        self._token_until = time.time() + float(data.get("expires_in") or 3600)
        return self._token

    async def _get(self, path: str, params: List[tuple]) -> Dict[str, Any]:
        for attempt in (1, 2):
            token = await self._app_token()
            async with self.http.get(HELIX + path, params=params, headers={
                    "Client-Id": self.client_id, "Authorization": f"Bearer {token}", "User-Agent": UA}) as r:
                if r.status == 401 and attempt == 1:
                    self._token = ""
                    continue
                if r.status != 200:
                    raise RuntimeError(f"Twitch {path} answered HTTP {r.status}")
                return await r.json(content_type=None)
        return {}

    async def resolve(self, handles: List[str]) -> Dict[str, Optional[Dict[str, str]]]:
        out: Dict[str, Optional[Dict[str, str]]] = {h: None for h in handles}
        for part in chunks([h.lower().lstrip("@") for h in handles], 100):
            data = await self._get("/users", [("login", h) for h in part])
            for u in data.get("data") or []:
                for h in handles:
                    if h.lower().lstrip("@") == str(u.get("login") or "").lower():
                        out[h] = {"platform_id": str(u.get("id") or ""), "display": str(u.get("display_name") or ""),
                                  "avatar": str(u.get("profile_image_url") or "")}
        return out

    async def live(self, accounts: List[Dict[str, Any]]) -> Dict[str, Live]:
        ids = [a["platform_id"] for a in accounts if a.get("platform_id")]
        out: Dict[str, Live] = {}
        for part in chunks(ids, 100):
            data = await self._get("/streams", [("type", "live"), ("first", "100")] + [("user_id", i) for i in part])
            for s in data.get("data") or []:
                login = str(s.get("user_login") or "")
                out[str(s.get("user_id"))] = {
                    "title": str(s.get("title") or ""), "game": str(s.get("game_name") or ""),
                    "viewers": int(s.get("viewer_count") or 0), "started_at": iso_ts(str(s.get("started_at") or "")),
                    "url": f"https://twitch.tv/{login}",
                    "thumb": str(s.get("thumbnail_url") or "").replace("{width}", "640").replace("{height}", "360")
                    + f"?t={int(time.time() // 300)}",      # Twitch caches the picture; a new one every 5 min
                }
        return out

    async def schedule(self, platform_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Upcoming schedule segments (Twitch answers 404 when the channel has no schedule).

        Each: {key (segment id; a repeating slot's id differs per week), at, title, game,
        thumb (the game's box art, "" when no category), platform}."""
        try:
            data = await self._get("/schedule", [("broadcaster_id", platform_id), ("first", str(limit))])
        except RuntimeError:
            return []
        segs = [s for s in ((data.get("data") or {}).get("segments")) or [] if not s.get("canceled_until")]
        art = await self.box_art([str((s.get("category") or {}).get("id") or "") for s in segs])
        return [{"key": str(s.get("id") or ""), "title": str(s.get("title") or ""),
                 "at": iso_ts(str(s.get("start_time") or "")),
                 "game": str((s.get("category") or {}).get("name") or ""),
                 "thumb": art.get(str((s.get("category") or {}).get("id") or ""), ""), "platform": "twitch"}
                for s in segs]

    async def box_art(self, game_ids: List[str]) -> Dict[str, str]:
        """{game id: box art URL at 285 x 380} for the calendar's scheduled tiles."""
        ids = sorted({i for i in game_ids if i})
        out: Dict[str, str] = {}
        for part in chunks(ids, 100):
            try:
                data = await self._get("/games", [("id", i) for i in part])
            except RuntimeError:
                continue
            for g in data.get("data") or []:
                out[str(g.get("id"))] = str(g.get("box_art_url") or "").replace("{width}", "285").replace("{height}", "380")
        return out

    async def last_vod(self, platform_id: str) -> Optional[Dict[str, Any]]:
        try:
            data = await self._get("/videos", [("user_id", platform_id), ("type", "archive"), ("first", "1")])
        except RuntimeError:
            return None
        rows = data.get("data") or []
        if not rows:
            return None
        v = rows[0]
        return {"url": str(v.get("url") or ""), "title": str(v.get("title") or ""),
                "created_at": iso_ts(str(v.get("created_at") or "")),
                "thumb": str(v.get("thumbnail_url") or "").replace("%{width}", "640").replace("%{height}", "360")}
