"""After-stream recap and chat moments (flavrbot/recap.py), fed the way flavrbot/core_link.py does."""

import time

from flavrbot.core_link import CoreLink
from flavrbot.recap import Recapper, ai_summary, clean_mentions, hms, recap_text, support, vod_at
from flavrbot.store import Store

T = time.time() - 7200          # a stream two hours ago (alerts / moments older than 120 days are pruned)


def test_vod_links_and_times():
    assert vod_at("https://www.twitch.tv/videos/123?foo=1", 3723) == "https://www.twitch.tv/videos/123?t=1h2m3s"
    assert vod_at("https://www.youtube.com/watch?v=abc", 95) == "https://www.youtube.com/watch?v=abc&t=95s"
    assert vod_at("https://kick.com/sen", 95) == "https://kick.com/sen"     # no time links: unchanged
    assert vod_at("", 5) == ""
    assert hms(65) == "1:05" and hms(3723) == "1:02:03"


def test_support_lines():
    lines = support([{"kind": "raid", "display_name": "Amy", "viewers": 40},
                     {"kind": "subscribe"}, {"kind": "resub"}, {"kind": "gift", "qty": 5},
                     {"kind": "superchat", "amount": 5, "currency": "USD"},
                     {"kind": "donation", "amount": 2.5, "currency": "USD"},
                     {"kind": "cheer", "amount": 300, "currency": "bits"}])
    assert lines[0] == "🚀 Raid from Amy with 40"
    assert "2 subs" in lines[1] and "5 gifted" in lines[1] and "7.50 USD" in lines[1] and "300 bits" in lines[1]


def test_recap_text_has_the_parts_and_fits():
    chat = [{"platform": "twitch", "username": "amy", "display_name": "Amy", "message": "hi"}] * 3 + \
           [{"platform": "kick", "username": "bob", "display_name": "Bob", "message": "yo"}]
    moments = [{"at": T + 600, "cpm": 90, "baseline": 12, "clip": "https://clips.twitch.tv/Abc"}]
    text = recap_text({"started_at": T, "ended_at": T + 3600, "platforms": ["twitch", "kick"], "peak": 33},
                      chat, [{"kind": "raid", "display_name": "Cat", "viewers": 9}], moments,
                      {"url": "https://www.twitch.tv/videos/9", "started_at": T + 30}, "A fun stream.", "Tuesday Nov 18")
    assert text.startswith("📼 **Stream recap** · Tuesday Nov 18")
    assert "Twitch, Kick" in text and "33 viewers" in text and "4 chat lines from 2 chatters" in text
    assert "Amy (3)" in text and "Raid from Cat" in text and "📝 A fun stream." in text
    assert "[9:30](<https://www.twitch.tv/videos/9?t=0h9m30s>)" in text       # VOD time, not stream time
    assert "[clip](<https://clips.twitch.tv/Abc>)" in text
    many = [{"at": T + i, "cpm": 50, "baseline": 5, "clip": "https://clips.twitch.tv/" + "x" * 150} for i in range(12)]
    assert len(recap_text({"started_at": T, "ended_at": T + 60}, [], [], many, {}, "y" * 700)) <= 1990


def test_ai_words_cant_ping():
    assert "<@" not in clean_mentions("hi <@123> and <@&5>") and "@everyone" not in clean_mentions("@everyone wow")


async def test_ai_summary_uses_ollama_and_fails_quietly():
    seen = {}

    async def post(url, body):
        seen.update(url=url, body=body)
        return {"response": " Chat loved the boss fight. @here "}

    chat = [{"display_name": "Amy", "message": "boss!"}]
    assert await ai_summary("http://ai:11434", "llama3.2:3b", chat, post) == "Chat loved the boss fight. @​here"
    assert seen["url"] == "http://ai:11434/api/generate" and seen["body"]["model"] == "llama3.2:3b"
    assert "Amy: boss!" in seen["body"]["prompt"] and seen["body"]["options"]["num_ctx"] >= 8192

    async def broken(url, body):
        raise OSError("no route")

    assert await ai_summary("http://ai:11434", "m", chat, broken) == ""
    assert await ai_summary("", "m", chat, post) == ""                        # OLLAMA_URL not set


async def test_stream_to_recap_through_core_link():
    store = Store(":memory:")
    link = CoreLink(store, "k")
    now = [T]
    pings, posts = [], []

    async def ping(kind, text, urgent=False):
        pings.append((kind, text))

    async def post_text(channel, text):
        posts.append((channel, text))

    hb = {"viewers": 0}
    r = Recapper(store, post_text, ping, lambda: hb, delay_min=3, clock=lambda: now[0])
    link.on_alert, link.on_moment, link.on_stream = r.on_alert, r.on_moment, r.on_stream
    store.put("recap_channel", "55")
    owner = store.add_person("Sen", is_owner=True)
    store.start_session(owner, T + 20, platforms=["twitch"], url="https://www.twitch.tv/sen",
                        vod_url="https://www.twitch.tv/videos/77")

    await link.handle("stream", {"event": "started", "started_at": T, "platforms": ["twitch"]})
    hb["viewers"] = 41
    await r.tick()
    hb["viewers"] = 12
    await r.tick()                                            # the peak stays 41
    store.add_chat([{"id": 1, "platform": "twitch", "username": "amy", "display_name": "Amy",
                     "message": "LOL", "at": T + 900}])
    await link.handle("alert", {"kind": "raid", "display_name": "Cat", "viewers": 9, "platform": "twitch"})
    await link.handle("moment", {"at": T + 900, "cpm": 80, "baseline": 10, "clip": "https://clips.twitch.tv/Z"})
    assert pings[-1][0] == "chat_moments" and "clips.twitch.tv/Z" in pings[-1][1]

    now[0] = T + 3600
    await link.handle("stream", {"event": "ended", "started_at": T, "ended_at": T + 3600, "platforms": ["twitch"]})
    assert await r.tick() is None                             # waits RECAP_DELAY_MIN for the last chat lines
    now[0] = T + 3600 + 181
    text = await r.tick()
    assert "41 viewers" in text and "Raid from Cat" in text and "Amy (1)" in text
    assert "https://www.twitch.tv/videos/77?t=0h14m40s" in text                  # 900 s in, VOD began 20 s late
    assert posts == [("55", text)] and pings[-1] == ("recap", text)
    assert await r.tick() is None and store.get("recap_pending") is None        # once only


async def test_short_streams_get_no_recap():
    store = Store(":memory:")
    sent = []

    async def ping(kind, text, urgent=False):
        sent.append(kind)

    async def post_text(channel, text):
        sent.append("post")

    now = [T]
    r = Recapper(store, post_text, ping, dict, clock=lambda: now[0])
    await r.on_stream({"event": "started", "started_at": T})
    await r.on_stream({"event": "ended", "started_at": T, "ended_at": T + 120})
    now[0] = T + 1000
    assert await r.tick() is None and sent == []
