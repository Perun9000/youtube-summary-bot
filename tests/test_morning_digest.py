from app.db import Database
from app.morning_digest import (
    MorningDigestItem,
    MorningDigestStore,
    build_rank_prompt,
    parse_rank_response,
    render_morning_digest,
)


def item(i, vid=None):
    return MorningDigestItem(
        video_id=vid or f"vid{i:08d}", title=f"Видео {i}", channel_name="Канал",
        telegraph_url=f"https://telegra.ph/v{i}", overview=f"Обзор {i}",
        tags_line="#тема", duration_sec=600, created_at_unix=1000 + i,
    )


def test_store_roundtrip(tmp_path):
    store = MorningDigestStore(Database(tmp_path / "bot.db"))
    store.add(item(1))
    store.add(item(2))
    assert [i.title for i in store.unsent()] == ["Видео 1", "Видео 2"]
    store.mark_sent([i.video_id for i in store.unsent()])
    assert store.unsent() == []


def test_parse_rank_response_valid():
    raw = '[{"video_id": "vid00000001", "score": 8, "reason": "по вашей теме"}, {"video_id": "unknown", "score": 5, "reason": "x"}]'
    ranks = parse_rank_response(raw, valid_ids={"vid00000001"})
    assert ranks == {"vid00000001": (8, "по вашей теме")}


def test_parse_rank_response_garbage_returns_empty():
    assert parse_rank_response("не json", valid_ids={"a"}) == {}
    assert parse_rank_response('{"not": "a list"}', valid_ids={"a"}) == {}


def test_parse_rank_response_clamps_score():
    raw = '[{"video_id": "a", "score": 99, "reason": "r"}, {"video_id": "b", "score": -3, "reason": "r"}]'
    ranks = parse_rank_response(raw, valid_ids={"a", "b"})
    assert ranks["a"][0] == 10 and ranks["b"][0] == 0


def test_render_sorted_and_fits():
    items = [item(1), item(2), item(3)]
    ranks = {items[0].video_id: (3, "так себе"), items[2].video_id: (9, "огонь")}
    html = render_morning_digest(items, ranks)
    assert len(html) <= 4000
    # Ранжированные выше, внутри — по убыванию score; без оценки — в конце.
    assert html.index("Видео 3") < html.index("Видео 1") < html.index("Видео 2")
    assert "огонь" in html and 'href="https://telegra.ph/v3"' in html


def test_build_rank_prompt_mentions_interests_and_items():
    prompt = build_rank_prompt([item(1)], interests=["инвестиции", "AI"])
    assert "инвестиции" in prompt and "vid00000001" in prompt and "Обзор 1" in prompt
