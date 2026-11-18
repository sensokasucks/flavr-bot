"""/said chat search (flavrbot/said.py) and the chat copy Core sends (store.py, core_link.py)."""

from flavrbot.core_link import CoreLink
from flavrbot.said import format_rows
from flavrbot.store import Store


def line(i, msg, who="amy", plat="twitch", at=None):
    return {"id": i, "platform": plat, "username": who, "display_name": who.title(), "message": msg,
            "at": at if at is not None else 1000.0 + i}


async def test_chat_copy_fills_in_and_clears():
    store = Store(":memory:")
    link = CoreLink(store, "k")
    assert store.chat_since() == 0
    await link.handle("chat", {"lines": [line(1, "pizza night!"), line(2, "hello", "bob", "youtube")]})
    await link.handle("chat", {"lines": [line(2, "hello", "bob", "youtube"), line(3, "night of PIZZA")]})
    assert store.chat_since() == 3 and store.chat_count() == 3            # the repeat was skipped
    await link.handle("chat_cleared", {"before": 1002.5})                 # keep-days trim: older lines go
    assert store.chat_count() == 1
    await link.handle("chat_cleared", {})                                 # a clear by hand: all of it
    assert store.chat_count() == 0 and store.chat_since() == 0


def test_search_words_who_platform_and_days():
    store = Store(":memory:")
    store.add_chat([line(1, "pizza night!"), line(2, "Pizza? at NIGHT", "bob", "youtube"),
                    line(3, "no food tonight", "bob"), line(4, "night night", "cat", "kick", at=50.0)])
    hits = store.search_chat("night pizza")
    assert [r["core_id"] for r in hits["rows"]] == [2, 1] and hits["total"] == 2   # newest first, any order
    assert store.search_chat(who="@BOB")["total"] == 2                    # name, any case, @ optional
    assert store.search_chat(who="Bob", platform="youtube")["total"] == 1
    assert store.search_chat("night", since=100)["total"] == 2            # cat's old line left out
    page2 = store.search_chat("night", limit=2, offset=2)
    assert page2["total"] == 3 and [r["core_id"] for r in page2["rows"]] == [4]


def test_search_without_fts_uses_like():
    store = Store(":memory:")
    store.fts = False
    store.add_chat([line(1, "pizza night!"), line(2, "nothing")])
    assert store.search_chat("PIZZA")["total"] == 1


def test_answer_text_is_escaped_and_paged():
    store = Store(":memory:")
    store.add_chat([line(i, f"*hi* @everyone {i}") for i in range(1, 13)])
    text = format_rows(store.search_chat("hi", limit=10), 1)
    assert "\\*hi\\*" in text and "<t:1012:f> **Amy** (Twitch)" in text
    assert "12 lines · page 1 of 2 · next: same search with page:2" in text
    assert format_rows(store.search_chat("zzz"), 1) == "No chat lines match."
