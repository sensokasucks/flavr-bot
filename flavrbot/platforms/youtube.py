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

Past weeks (``past_streams``, for Watcher.backfill): the channel's uploads list
(playlistItems.list, 1 unit per 50 videos, newest first, stops at the week) and one videos.list for
their real start and end times. Finished livestreams are in the uploads list as long as the
streamer keeps the VOD public.

Videos already known to be over are remembered (``_done``) and never asked about again.

Waiting rooms made long ahead (Sen 2026-11-20: Amanda's Nov 22 stream, made Oct 22, never showed):
the RSS feed lists only the 15 newest videos, so a waiting room drops out of it once the channel
posts 15 more. Two things catch those:
  * every upcoming stream found is remembered (``_upcoming_ids``) and asked about directly until it
    goes live, ends or is gone, even after it leaves the feed;
  * once every SEARCH_EVERY per channel, one Data API ``search.list`` (eventType=upcoming, 100 quota
    units) asks YouTube for that channel's scheduled streams, wherever they are in its list. About
    15 channels x 100 = 1,500 of the 10,000 free daily units. It runs at start-up too (memory only).
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
# Only feed videos this new are asked about. Waiting rooms are often made weeks ahead, so this
# is long; every video is asked about once and remembered (_done) once it's over or an upload.
FRESH_DAYS = 45
SEARCH_EVERY = 24 * 3600.0      # per channel: one search.list for scheduled streams (100 quota units)


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
        self._upcoming_ids: Dict[str, str] = {}   # video id → channel id: waiting rooms seen, asked until they start
        self._searched: Dict[str, float] = {}     # channel id → last search.list for its upcoming streams

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

    async def search_upcoming(self, channel_id: str) -> List[str]:
        """The channel's scheduled streams straight from YouTube's search (100 quota units): video ids."""
        data = await self._api("/search", {"part": "id", "channelId": channel_id, "eventType": "upcoming",
                                           "type": "video", "maxResults": "25"})
        return [str((i.get("id") or {}).get("videoId") or "") for i in data.get("items") or []
                if (i.get("id") or {}).get("videoId")]

    async def live(self, accounts: List[Dict[str, Any]]) -> Dict[str, Live]:
        """RSS for every channel (plus a daily search, and the waiting rooms already found), then one
        videos.list per 50 of the fresh, not-yet-finished videos."""
        if not self.api_key:
            return {}
        owner_of: Dict[str, str] = {}
        now = time.time()
        cutoff = now - FRESH_DAYS * 86400
        followed = {a.get("platform_id") or "" for a in accounts} - {""}
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
            if now - self._searched.get(cid, 0.0) >= SEARCH_EVERY:
                self._searched[cid] = now           # also on failure: a quota problem shouldn't retry every check
                try:
                    for vid in await self.search_upcoming(cid):
                        if vid not in self._done:
                            owner_of.setdefault(vid, cid)
                except (aiohttp.ClientError, RuntimeError) as e:
                    log.info("YouTube search for %s's scheduled streams failed: %s", cid, e)
        for vid, cid in list(self._upcoming_ids.items()):
            if cid in followed:
                owner_of.setdefault(vid, cid)       # a waiting room found earlier, maybe gone from the feed
            else:
                del self._upcoming_ids[vid]         # nobody follows that channel any more
        out: Dict[str, Live] = {}
        upcoming: Dict[str, List[Dict[str, Any]]] = {}
        for part in chunks(list(owner_of), 50):
            data = await self._api("/videos", {"part": "snippet,liveStreamingDetails", "id": ",".join(part)})
            answered = {str(v.get("id") or "") for v in data.get("items") or []}
            for vid in part:
                if vid not in answered:
                    self._upcoming_ids.pop(vid, None)   # deleted or made private
            for v in data.get("items") or []:
                vid = str(v.get("id") or "")
                sn = v.get("snippet") or {}
                ld = v.get("liveStreamingDetails") or {}
                state = sn.get("liveBroadcastContent")
                cid = owner_of.get(vid, "")
                if state != "upcoming":
                    self._upcoming_ids.pop(vid, None)
                if state == "live":
                    out[cid] = {"title": str(sn.get("title") or ""), "game": "",
                                "viewers": int(ld.get("concurrentViewers") or 0),
                                "started_at": iso_ts(str(ld.get("actualStartTime") or "")),
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault_live.jpg?t={int(time.time() // 300)}",
                                "video_id": vid}
                elif state == "upcoming":
                    self._upcoming_ids[vid] = cid
                    upcoming.setdefault(cid, []).append({
                        "title": str(sn.get("title") or ""), "at": iso_ts(str(ld.get("scheduledStartTime") or "")),
                        "url": f"https://www.youtube.com/watch?v={vid}", "platform": "youtube", "video_id": vid,
                        "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "game": ""})
                else:
                    self._done.add(vid)       # an upload or a finished stream: never ask again
        self.upcoming = upcoming
        return out

    async def past_streams(self, channel_id: str, since: float, until: float) -> List[Dict[str, Any]]:
        """Finished livestreams that started in [since, until). A channel's uploads playlist is its id
        with UC → UU. About 2-4 quota units per channel. → [{started_at, ended_at, title, url, thumb}]"""
        if not self.api_key or not CHANNEL_ID.match(channel_id or ""):
            return []
        uploads = "UU" + channel_id[2:]
        ids: List[str] = []
        token = ""
        for _ in range(4):                    # at most 200 newest videos; plenty for a week or two
            params = {"part": "contentDetails", "playlistId": uploads, "maxResults": "50"}
            if token:
                params["pageToken"] = token
            data = await self._api("/playlistItems", params)
            oldest = None
            for it in data.get("items") or []:
                cd = it.get("contentDetails") or {}
                if cd.get("videoId"):
                    ids.append(str(cd["videoId"]))
                    t = iso_ts(str(cd.get("videoPublishedAt") or ""))
                    oldest = t if oldest is None or (t is not None and t < oldest) else oldest
            token = str(data.get("nextPageToken") or "")
            # a stream's video is published when it starts, so once the list is older than the week, stop
            if not token or (oldest is not None and oldest < since - 86400):
                break
        out = []
        for part in chunks(ids, 50):
            data = await self._api("/videos", {"part": "snippet,liveStreamingDetails", "id": ",".join(part)})
            for v in data.get("items") or []:
                ld = v.get("liveStreamingDetails") or {}
                start = iso_ts(str(ld.get("actualStartTime") or ""))
                end = iso_ts(str(ld.get("actualEndTime") or ""))
                if start is None or end is None or not (since <= start < until):
                    continue                  # an upload, a stream still live, or outside the week
                vid = str(v.get("id") or "")
                self._done.add(vid)
                out.append({"started_at": start, "ended_at": end, "title": str((v.get("snippet") or {}).get("title") or ""),
                            "url": f"https://www.youtube.com/watch?v={vid}",
                            "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
        return out
