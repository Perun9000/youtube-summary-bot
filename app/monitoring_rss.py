from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx


logger = logging.getLogger(__name__)

FEED_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"


@dataclass(frozen=True)
class FeedEntry:
    video_id: str
    title: str
    url: str
    published_at: datetime | None
    description: str


async def fetch_channel_feed(client: httpx.AsyncClient, channel_id: str) -> list[FeedEntry]:
    url = FEED_URL_TEMPLATE.format(channel_id=channel_id)
    response = await client.get(url, timeout=30)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    entries: list[FeedEntry] = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        video_id_el = entry.find(f"{YT_NS}videoId")
        if video_id_el is None or not (video_id_el.text or "").strip():
            continue
        video_id = video_id_el.text.strip()

        title_el = entry.find(f"{ATOM_NS}title")
        title = (title_el.text or "").strip() if title_el is not None else f"YouTube video {video_id}"

        link_el = entry.find(f"{ATOM_NS}link")
        link = ""
        if link_el is not None and link_el.get("href"):
            link = link_el.get("href", "").strip()
        if not link:
            link = f"https://www.youtube.com/watch?v={video_id}"

        published_el = entry.find(f"{ATOM_NS}published")
        published_at: datetime | None = None
        if published_el is not None and (published_el.text or "").strip():
            published_at = _parse_iso8601(published_el.text.strip())

        description = ""
        media_group = entry.find(f"{MEDIA_NS}group")
        if media_group is not None:
            desc_el = media_group.find(f"{MEDIA_NS}description")
            if desc_el is not None and desc_el.text:
                description = desc_el.text.strip()

        entries.append(
            FeedEntry(
                video_id=video_id,
                title=title,
                url=link,
                published_at=published_at,
                description=description,
            )
        )
    logger.info("monitoring.rss.fetched channel_id=%s entries=%s", channel_id, len(entries))
    return entries


def _parse_iso8601(value: str) -> datetime | None:
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        logger.warning("monitoring.rss.bad_published value=%r", value)
        return None
