"""
YouTube for the go-live watcher.

  * Each channel's public RSS feed (www.youtube.com/feeds/videos.xml?channel_id=...): its newest
    15 videos, including scheduled and live streams. Free, no key.
  * One Data API ``videos.list`` call (YOUTUBE_API_KEY, a free key from console.cloud.google.com;
    1 quota unit per call of up to 50 videos, 10,000 units a day) says which of those videos are
    live now or scheduled, with the real start time. The RSS "published" date is NOT the stream
    start (waiting rooms are made days ahead), so without a key YouTube streams aren't watched.
  * A handle (@name) is turned into a channel id with ``channels.list?forHandle=`` (1 unit, once).
    Without a key, add people by channel id (UC...) instead.

Videos already known to be over are remembered (``_done``) and never asked about again.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import aiohttp

from . import UA, Live
from .twitch import chunks, iso_ts

log = logging.getLogger("flavrbot.youtube")
API = "https://www.googleapis.com/youtube/v3"
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id="
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
FRESH_DAYS = 3          # only videos the feed says are this new can be live / upcoming soon


def parse_feed(xml_text: str) -> List[Dict[str, Any]]:
    """RSS → [{video_id, title, published (unix s), thumb}] newest first."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", "", NS)
        if not vid:
            continue
        thumb = e.find("media:group/media:thumbnail", NS)
        out.append({"video_id": vid, "title": e.findtext("a:title", "", NS),
                    "published": iso_ts(e.findtext("a:published", "", NS)) or 0.0,
                    "thumb": thumb.get("url") if thumb is not None else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
    return out


class YouTube:
    platform = "youtube"

    def __init__(self, http: aiohttp.ClientSession, api_key: str = ""):
        self.http = http
        self.api_key = api_key
        self._done: set[str] = set()          # videos known to be over or plain uploads
        self.upcoming: Dict[str, List[Dict[str, Any]]] = {}   # channel id → scheduled streams (for /schedule)

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    async def _api(self, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        async with self.http.get(API + path, params={**params, "key": self.api_key},
                                 headers={"User-Agent": UA}) as r:
            if r.status != 200:
                raise RuntimeError(f"YouTube {path} answered HTTP {r.status} (check YOUTUBE_API_KEY and its quota)")
            return await r.json(content_type=None)

    async def resolve(self, handles: List[str]) -> Dict[str, Optional[Dict[str, str]]]:
        out: Dict[str, Optional[Dict[str, str]]] = {}
        for h in handles:
            out[h] = None
            if CHANNEL_ID.match(h):
                out[h] = {"platform_id": h, "display": "", "avatar": ""}
                if not self.api_key:
                    continue
                params = {"part": "snippet", "id": h}
            elif self.api_key:
                params = {"part": "snippet", "forHandle": h if h.startswith("@") else "@" + h}
            else:
                continue
            try:
                items = (await self._api("/channels", params)).get("items") or []
            except RuntimeError as e:
                log.warning("%s", e)
                continue
            if items:
                sn = items[0].get("snippet") or {}
                out[h] = {"platform_id": str(items[0].get("id") or ""), "display": str(sn.get("title") or ""),
                          "avatar": str(((sn.get("thumbnails") or {}).get("default") or {}).get("url") or "")}
        return out

    async def feed(self, channel_id: str) -> List[Dict[str, Any]]:
        async with self.http.get(FEED + channel_id, headers={"User-Agent": UA}) as r:
            if r.status != 200:
                return []
            return parse_feed(await r.text())

    async def live(self, accounts: List[Dict[str, Any]]) -> Dict[str, Live]:
        """RSS for every channel, then one videos.list for the fresh, not-yet-finished videos."""
        if not self.api_key:
            return {}
        owner_of: Dict[str, str] = {}
        cutoff = time.time() - FRESH_DAYS * 86400
        for a in accounts:
            cid = a.get("platform_id") or ""
            if not cid:
                continue
            try:
                for v in await self.feed(cid):
                    if v["video_id"] not in self._done and v["published"] >= cutoff:
                        owner_of[v["video_id"]] = cid
            except aiohttp.ClientError as e:
                log.info("YouTube feed for %s failed: %s", cid, e)
        out: Dict[str, Live] = {}
        upcoming: Dict[str, List[Dict[str, Any]]] = {}
        for part in chunks(list(owner_of), 50):
            data = await self._api("/videos", {"part": "snippet,liveStreamingDetails", "id": ",".join(part)})
            for v in data.get("items") or []:
                vid = str(v.get("id") or "")
                sn = v.get("snippet") or {}
                ld = v.get("liveStreamingDetails") or {}
                state = sn.get("liveBroadcastContent")
                cid = owner_of.get(vid, "")
                if state == "live":
                    out[cid] = {"title": str(sn.get("title") or ""), "game": "",
                                "viewers": int(ld.get("concurrentViewers") or 0),
                                "started_at": iso_ts(str(ld.get("actualStartTime") or "")),
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault_live.jpg?t={int(time.time() // 300)}",
                                "video_id": vid}
                elif state == "upcoming":
                    upcoming.setdefault(cid, []).append({
                        "title": str(sn.get("title") or ""), "at": iso_ts(str(ld.get("scheduledStartTime") or "")),
                        "url": f"https://www.youtube.com/watch?v={vid}", "platform": "youtube"})
                else:
                    self._done.add(vid)       # an upload or a finished stream: never ask again
        self.upcoming = upcoming
        return out
