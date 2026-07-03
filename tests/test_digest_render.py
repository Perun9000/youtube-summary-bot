from app.digest_service import DigestEntry, render_digest_html, MAX_DIGEST_CHARS


def entry(i):
    return DigestEntry(video_id=f"video{i:06d}", title=f"Заголовок {i}", telegraph_url=f"https://telegra.ph/x-{i}", channel_name="Канал")


def test_empty_digest():
    assert "Пока пусто" in render_digest_html([])


def test_render_contains_links_and_fits_budget():
    html = render_digest_html([entry(i) for i in range(20)])
    assert len(html) <= MAX_DIGEST_CHARS
    assert 'href="https://telegra.ph/x-0"' in html  # newest (первый в списке) всегда внутри


def test_html_escaping():
    e = DigestEntry(video_id="v", title="A <b> & B", telegraph_url="https://telegra.ph/x", channel_name="")
    html = render_digest_html([e])
    assert "&lt;b&gt;" in html and "&amp;" in html
