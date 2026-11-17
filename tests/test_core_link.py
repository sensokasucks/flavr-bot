"""The Stream Core side door (flavrbot/core_link.py): password, message handling, heartbeat watch."""

from aiohttp.test_utils import TestClient, TestServer

from flavrbot.core_link import CoreLink
from flavrbot.store import Store


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make():
    clock = Clock()
    link = CoreLink(Store(":memory:"), "pw", late_sec=90, clock=clock)
    got = {"stream": [], "alert": [], "lost": [], "back": []}

    async def rec(kind, x):
        got[kind].append(x)

    link.on_stream = lambda c: rec("stream", c)
    link.on_alert = lambda a: rec("alert", a)
    link.on_core_lost = lambda i: rec("lost", i)
    link.on_core_back = lambda s: rec("back", s)
    return link, clock, got


async def test_password_and_messages():
    link, clock, got = make()
    async with TestClient(TestServer(link.app())) as c:
        r = await c.post("/core/push", json={"type": "hello"}, headers={"Authorization": "Bearer nope"})
        assert r.status == 401
        assert link.refused == 1
        h = {"Authorization": "Bearer pw"}
        r = await c.post("/core/push", json={"type": "heartbeat", "data": {"stream": {"state": "live", "live": True}}},
                         headers=h)
        assert r.status == 200 and link.core_online() and link.stream_live()
        await c.post("/core/push", json={"type": "snapshot", "data": {"points": [{"name": "Amy", "points": 3}]}}, headers=h)
        await c.post("/core/push", json={"type": "snapshot", "data": {"queue": {"pending": 2}}}, headers=h)
        assert link.snapshot == {"points": [{"name": "Amy", "points": 3}], "queue": {"pending": 2}}
        await c.post("/core/push", json={"type": "alert", "data": {"kind": "raid"}}, headers=h)
        await c.post("/core/push", json={"type": "stream", "data": {"event": "ended", "live": False}}, headers=h)
        await c.post("/core/push", json={"type": "from-the-future", "data": {}}, headers=h)
        assert got["alert"] == [{"kind": "raid"}]
        assert got["stream"][0]["event"] == "ended"
        assert link.stream()["state"] == "offline"
        assert (await (await c.get("/health")).json()) == {"ok": True}


async def test_empty_key_refuses_everything():
    link = CoreLink(Store(":memory:"), "")
    async with TestClient(TestServer(link.app())) as c:
        r = await c.post("/core/push", json={"type": "hello"}, headers={"Authorization": "Bearer "})
        assert r.status == 401


async def test_core_lost_only_during_a_stream_then_back():
    link, clock, got = make()
    await link.handle("heartbeat", {"stream": {"state": "offline", "live": False}})
    clock.t += 500
    await link.check_late()
    assert got["lost"] == []                 # PC simply off between streams: no ping
    await link.handle("heartbeat", {"stream": {"state": "live", "live": True}})
    clock.t += 89
    await link.check_late()
    assert got["lost"] == []
    clock.t += 1
    await link.check_late()
    await link.check_late()
    assert len(got["lost"]) == 1 and got["lost"][0]["silent_sec"] == 90
    await link.handle("heartbeat", {"stream": {"state": "live", "live": True}})
    assert len(got["back"]) == 1


async def test_snapshot_survives_a_restart():
    store = Store(":memory:")
    link = CoreLink(store, "pw")
    await link.handle("snapshot", {"points": [{"name": "Bo", "points": 1}]})
    again = CoreLink(store, "pw")
    assert again.snapshot["points"][0]["name"] == "Bo"
