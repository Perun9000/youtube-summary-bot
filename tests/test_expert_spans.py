from app.models import TranscriptSegment, VideoChapter
from app.monitoring_service import _compute_expert_spans, _merge_spans, filter_segments_by_spans


def seg(start, text):
    return TranscriptSegment(start=start, end=start + 5, text=text)


def test_merge_spans_merges_close_and_keeps_far():
    assert _merge_spans([(0, 100), (150, 200), (500, 600)], merge_gap_sec=60) == [(0, 200), (500, 600)]


def test_chapter_priority_over_transcript():
    spans = _compute_expert_spans(
        segments=[seg(3000, "тут Иванов говорит")],
        chapters=(VideoChapter(start=0, title="Интро"), VideoChapter(start=600, title="Иванов о рынке"), VideoChapter(start=1200, title="Финал")),
        expert_names=["Иванов"],
        video_duration_sec=3600,
        window_pre_sec=60,
        window_post_sec=180,
        cluster_gap_sec=300,
    )
    assert spans == [(600.0, 1200.0)]  # глава, а не окно вокруг упоминания


def test_transcript_clusters_with_window():
    spans = _compute_expert_spans(
        segments=[seg(1000, "слово Иванову"), seg(1100, "Иванов продолжает"), seg(3000, "снова Иванов")],
        chapters=(),
        expert_names=["Иванов"],
        video_duration_sec=3600,
        window_pre_sec=60,
        window_post_sec=180,
        cluster_gap_sec=300,
    )
    assert spans == [(940.0, 1280.0), (2940.0, 3180.0)]


def test_no_mentions_returns_empty():
    assert _compute_expert_spans(
        segments=[seg(10, "ни слова про эксперта")], chapters=(), expert_names=["Иванов"],
        video_duration_sec=100, window_pre_sec=60, window_post_sec=180, cluster_gap_sec=300,
    ) == []


def test_filter_segments_by_spans():
    segs = [seg(0, "a"), seg(500, "b"), seg(1000, "c")]
    assert filter_segments_by_spans(segs, [(400, 600)]) == [segs[1]]
    assert filter_segments_by_spans(segs, []) == segs
