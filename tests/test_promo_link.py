"""flavrbot/promo_link.py: reading FlaVR Promo's /live.json (a local test server, no network)."""

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from flavrbot.promo_link import STALE, PromoLink


async def test_owner_live_game_streams_and_live_mode():
    answer = {"owner_live": False, "live": [], "owner_streams": []}

    async def live(request):
        return web.json_response(answer)

    app = web.Application()
    app.router.add_get("/live.json", live)
    clock = {"t": 1000.0}
    calls = []
    async with TestServer(app) as server, ClientSession() as http:
        link = PromoLink(http, str(server.make_url("/")), clock=lambda: clock["t"])

        async def on_owner(is_live, game):
            calls.append((is_live, game))
        link.on_owner = on_owner
        assert link.owner_live() is None                    # nothing read yet
        assert await link.poll() and link.owner_live() is False
        assert calls == []                                  # the first answer only sets the baseline
        # the owner goes live, then changes game, then ends: live mode hears each change once
        answer.update(owner_live=True, live=[{"name": "Sen", "owner": True, "game": "Valheim"},
                                             {"name": "Lulu", "owner": False, "game": "Art"}])
        await link.poll()
        await link.poll()
        answer["live"][0]["game"] = "Schedule I"
        await link.poll()
        answer.update(owner_live=False, live=[],
                      owner_streams=[{"started_at": 1.0, "ended_at": 2.0, "url": "", "vod_url": "https://v/1"}])
        await link.poll()
        assert calls == [(True, "Valheim"), (True, "Schedule I"), (False, "")]
        assert link.owner_streams[0]["vod_url"] == "https://v/1"
        # Promo stops answering: after STALE the owner's state is unknown again (the Core alarm works)
        await server.close()
        clock["t"] += STALE + 1
        assert not await link.poll() and link.error
        assert link.owner_live() is None
        assert link.owner_streams                           # a finished stream's VOD stays usable


async def test_not_set():
    link = PromoLink(None, "")
    assert not link.ready and not await link.poll() and link.owner_live() is None
    await link.run()                                        # returns at once without PROMO_URL
