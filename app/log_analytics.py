"""Parse rotated bot.log files and aggregate usage / cost / perf metrics.

The bot logs structured events with ``event.name key=value key=value`` bodies
(see ``logger.info`` calls all over the code). This module reads those, extracts
counters and durations, and renders human-readable reports for two surfaces:

- ``scripts/analytics.py`` — CLI markdown output for offline analysis
- ``/stats`` command in the bot — compact HTML for Telegram (4096-char limit)

The parsing is intentionally tolerant: unknown / malformed events are
silently skipped, so a future log-format tweak doesn't break analytics.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+"
    r"(?P<level>INFO|WARNING|ERROR|DEBUG)\s+"
    r"(?P<logger>\S+)\s+"
    r"(?P<message>.*)$"
)

# Распознаёт ``key=value`` пары. Допускает три формата значения:
#   key='quoted with spaces' (repr() от Python)
#   key="double quoted"
#   key=bareword (без пробелов внутри)
KV_RE = re.compile(r"(\w+)=(?:'([^']*)'|\"([^\"]*)\"|(\S+))")


@dataclass
class LogEvent:
    timestamp: dt.datetime
    level: str
    logger: str
    name: str           # первое слово сообщения, например "queue.job.done"
    kv: dict[str, str]  # распарсенные ключ-значение пары


def iter_events(
    logs_dir: Path,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> Iterable[LogEvent]:
    """Yield parsed events from ``bot.log*`` files in chronological order.

    Reads the rotated archives first (older), then current ``bot.log``.
    Skips lines we can't parse — corrupted entries don't break the stream.
    """
    files = sorted(_log_files(logs_dir))
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("log_analytics.read_failed path=%s error=%s", path, exc)
            continue
        for raw in content.splitlines():
            ev = _parse_line(raw)
            if ev is None:
                continue
            if since and ev.timestamp < since:
                continue
            if until and ev.timestamp > until:
                continue
            yield ev


def _log_files(logs_dir: Path) -> list[Path]:
    """Sorted list of bot.log files: archives first (oldest → newest), then current."""
    if not logs_dir.is_dir():
        return []
    archives = sorted(p for p in logs_dir.glob("bot.log.*") if p.is_file())
    current = logs_dir / "bot.log"
    if current.exists():
        archives.append(current)
    return archives


def _parse_line(raw: str) -> LogEvent | None:
    m = LINE_RE.match(raw)
    if not m:
        return None
    msg = m.group("message").strip()
    if not msg:
        return None
    parts = msg.split(maxsplit=1)
    name = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    kv: dict[str, str] = {}
    for kv_match in KV_RE.finditer(body):
        key = kv_match.group(1)
        value = (
            kv_match.group(2)  # 'single quoted'
            or kv_match.group(3)  # "double quoted"
            or kv_match.group(4)  # bareword
            or ""
        )
        kv[key] = value
    try:
        ts = dt.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return LogEvent(
        timestamp=ts,
        level=m.group("level"),
        logger=m.group("logger"),
        name=name,
        kv=kv,
    )


@dataclass
class Stats:
    """Aggregate counters + samples for a time window."""

    period_start: dt.datetime | None = None
    period_end: dt.datetime | None = None

    # Per-user (chat_id → count)
    user_jobs: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    user_cache_hits: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    # LLM cost / volume
    llm_cost_usd: float = 0.0
    llm_calls: int = 0
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_calls_per_provider: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    llm_calls_per_model: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    llm_cost_per_day: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    # Performance — durations of successful jobs in seconds.
    job_durations_sec: list[float] = field(default_factory=list)

    # Source of transcripts
    transcript_sources: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    # Cache effectiveness
    summaries_stored: int = 0
    summaries_expired: int = 0

    # Channel publishing
    channel_posts: int = 0

    # Monitoring
    monitoring_scans: int = 0
    monitoring_skipped: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    monitoring_accepted: int = 0

    # Errors
    errors: int = 0
    # Распределение ERROR-строк по источнику. Ключ — "<logger>/<event.name>"
    # (например, "app.bot_handlers/job.comments.failed"). Это нужно чтобы в
    # /stats показать «откуда идут» 90% ошибок, а не просто общее число —
    # иначе цифра выглядит пугающей и непонятно что делать.
    errors_by_source: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def aggregate(events: Iterable[LogEvent]) -> Stats:
    s = Stats()
    for ev in events:
        if s.period_start is None:
            s.period_start = ev.timestamp
        s.period_end = ev.timestamp
        _ingest(s, ev)
    return s


def _ingest(s: Stats, ev: LogEvent) -> None:
    if ev.level == "ERROR":
        s.errors += 1
        # ``ev.name`` это первое слово сообщения (например "job.comments.failed"
        # или просто "Traceback" если строка — это часть стектрейса). Для
        # стектрейсов получится мусорный ключ — это OK, они и должны попасть
        # в «other», в топе их видно не будет.
        source = f"{ev.logger}/{ev.name}" if ev.name else ev.logger
        s.errors_by_source[source] += 1

    name = ev.name
    kv = ev.kv

    if name == "queue.job.enqueued":
        chat = kv.get("chat_id")
        if chat:
            s.user_jobs[chat] += 1
    elif name == "queue.cache.hit":
        chat = kv.get("chat_id")
        if chat:
            s.user_cache_hits[chat] += 1
    elif name == "job.cache.hit":
        chat = kv.get("chat_id")
        if chat:
            s.user_cache_hits[chat] += 1
    elif name == "llm.generate.done":
        s.llm_calls += 1
        s.llm_prompt_tokens += _int(kv, "prompt_tokens")
        s.llm_completion_tokens += _int(kv, "completion_tokens")
        cost = _float(kv, "cost_usd")
        s.llm_cost_usd += cost
        provider = kv.get("provider", "unknown")
        model = kv.get("model", "unknown")
        s.llm_calls_per_provider[provider] += 1
        s.llm_calls_per_model[model] += 1
        day = ev.timestamp.date().isoformat()
        s.llm_cost_per_day[day] += cost
    elif name == "job.done":
        s.job_durations_sec.append(_float(kv, "duration_sec"))
    elif name == "job.transcript.done":
        source = kv.get("source", "unknown")
        s.transcript_sources[source] += 1
    elif name == "summary_cache.stored":
        s.summaries_stored += 1
    elif name == "summary_cache.expired":
        s.summaries_expired += 1
    elif name == "channel_posts.stored":
        s.channel_posts += 1
    elif name == "monitoring.scan.start":
        s.monitoring_scans += 1
    elif name == "monitoring.entry.accepted":
        s.monitoring_accepted += 1
    elif name == "monitoring.entry.skip":
        reason = kv.get("reason", "unknown")
        s.monitoring_skipped[reason] += 1


def _int(kv: dict[str, str], key: str) -> int:
    try:
        return int(float(kv.get(key, "0")))
    except (TypeError, ValueError):
        return 0


def _float(kv: dict[str, str], key: str) -> float:
    try:
        return float(kv.get(key, "0"))
    except (TypeError, ValueError):
        return 0.0


# ──────────────────────────── рендереры ────────────────────────────

def format_markdown(
    s: Stats,
    *,
    name_resolver: Callable[[str], str | None] | None = None,
    summary_cache=None,
    tags_catalog=None,
) -> str:
    """Render a long markdown report for stdout / scripts/analytics.py."""
    lines: list[str] = []
    lines.append(_section_header(s))
    lines.append("")

    # Активность пользователей
    lines.append("## Активность пользователей")
    if not s.user_jobs and not s.user_cache_hits:
        lines.append("_нет данных_")
    else:
        all_chats = set(s.user_jobs.keys()) | set(s.user_cache_hits.keys())
        rows: list[tuple[str, str, int, int, float]] = []
        for chat in all_chats:
            display = (name_resolver(chat) if name_resolver else None) or chat
            jobs = s.user_jobs.get(chat, 0)
            hits = s.user_cache_hits.get(chat, 0)
            total = jobs + hits
            hit_rate = (hits / total * 100) if total else 0.0
            rows.append((chat, display, jobs, hits, hit_rate))
        rows.sort(key=lambda r: r[2] + r[3], reverse=True)
        lines.append("| chat_id | имя | новые саммари | cache-hit | % cache |")
        lines.append("|---|---|---:|---:|---:|")
        for chat, display, jobs, hits, hit_rate in rows:
            lines.append(f"| {chat} | {display} | {jobs} | {hits} | {hit_rate:.0f}% |")
    lines.append("")

    # LLM costs / volume
    lines.append("## LLM нагрузка")
    if s.llm_calls == 0:
        lines.append("_нет вызовов LLM_")
    else:
        lines.append(f"- Запросов всего: **{s.llm_calls}**")
        lines.append(f"- Затраты: **${s.llm_cost_usd:.4f}**")
        lines.append(f"- Токенов: prompt={s.llm_prompt_tokens:,} / completion={s.llm_completion_tokens:,}")
        if s.llm_cost_usd > 0:
            avg = s.llm_cost_usd / max(1, s.llm_calls)
            lines.append(f"- Среднее $ за запрос: ${avg:.4f}")
        if s.llm_calls_per_provider:
            lines.append("\n**По провайдерам:**")
            for prov, n in sorted(s.llm_calls_per_provider.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"- {prov}: {n}")
        if s.llm_calls_per_model:
            lines.append("\n**По моделям (топ 5):**")
            top5 = sorted(s.llm_calls_per_model.items(), key=lambda x: x[1], reverse=True)[:5]
            for model, n in top5:
                lines.append(f"- {model}: {n}")
        # Cost per day, последние 7 дней
        if s.llm_cost_per_day:
            lines.append("\n**$ за последние 7 дней:**")
            recent = sorted(s.llm_cost_per_day.items(), reverse=True)[:7]
            for day, cost in reversed(recent):
                lines.append(f"- {day}: ${cost:.4f}")
    lines.append("")

    # Производительность
    lines.append("## Производительность саммаризации")
    if not s.job_durations_sec:
        lines.append("_нет завершённых job-ов в окне_")
    else:
        ds = s.job_durations_sec
        lines.append(f"- Завершённых: {len(ds)}")
        lines.append(f"- Среднее: {_fmt_duration(statistics.mean(ds))}")
        lines.append(f"- Медиана: {_fmt_duration(statistics.median(ds))}")
        if len(ds) >= 5:
            p95 = statistics.quantiles(sorted(ds), n=20)[-1]
            lines.append(f"- p95: {_fmt_duration(p95)}")
        lines.append(f"- Самое долгое: {_fmt_duration(max(ds))}")
        lines.append(f"- Самое быстрое: {_fmt_duration(min(ds))}")
    lines.append("")

    # Источник транскриптов
    if s.transcript_sources:
        lines.append("## Источник транскриптов")
        for src, n in sorted(s.transcript_sources.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"- {src}: {n}")
        lines.append("")

    # Cache
    lines.append("## Кэш саммари")
    lines.append(f"- Сохранено новых: {s.summaries_stored}")
    lines.append(f"- Удалено по TTL: {s.summaries_expired}")
    total_jobs = sum(s.user_jobs.values())
    total_hits = sum(s.user_cache_hits.values())
    if total_jobs + total_hits > 0:
        rate = total_hits / (total_jobs + total_hits) * 100
        lines.append(f"- Hit rate: **{rate:.1f}%** ({total_hits} из {total_jobs + total_hits})")
    lines.append("")

    # Channel publishes
    if s.channel_posts:
        lines.append("## Публикации в канал")
        lines.append(f"- Опубликовано постов: {s.channel_posts}")
        lines.append("")

    # Monitoring
    if s.monitoring_scans:
        lines.append("## Мониторинг каналов")
        lines.append(f"- Запусков скана: {s.monitoring_scans}")
        lines.append(f"- Принято роликов: {s.monitoring_accepted}")
        if s.monitoring_skipped:
            lines.append("\n**Пропущено по причинам:**")
            for reason, n in sorted(s.monitoring_skipped.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"- {reason}: {n}")
        lines.append("")

    # Top channels / tags from summary_cache + tags_catalog
    if summary_cache is not None:
        top = _top_channels_from_cache(summary_cache, limit=10)
        if top:
            lines.append("## Самые суммаризируемые каналы (из кэша)")
            for ch, n in top:
                lines.append(f"- {ch}: {n}")
            lines.append("")

    if tags_catalog is not None:
        lines.append("## Каталог тегов")
        for cat in ("topic", "speaker", "host", "format", "channel"):
            tags = tags_catalog.all_tags(cat)
            if tags:
                shown = tags[:15]
                tail = f" (+ ещё {len(tags) - 15})" if len(tags) > 15 else ""
                lines.append(f"- {cat}: {', '.join(shown)}{tail}")
        lines.append("")

    if s.errors:
        lines.append("## Ошибки")
        lines.append(f"- ERROR-строк в логах: {s.errors}")
        if s.errors_by_source:
            lines.append("\n**Топ источников:**")
            top = sorted(s.errors_by_source.items(), key=lambda x: x[1], reverse=True)[:10]
            for source, n in top:
                lines.append(f"- {source}: {n}")
        lines.append("")

    return "\n".join(lines)


def format_telegram(
    s: Stats,
    *,
    name_resolver: Callable[[str], str | None] | None = None,
    summary_cache=None,
) -> str:
    """Compact HTML report for the /stats command (≤ 4096 chars)."""
    parts: list[str] = []
    period_text = _format_period(s)
    parts.append(f"<b>📊 Статистика бота</b> ({period_text})")

    # Активность
    if s.user_jobs or s.user_cache_hits:
        all_chats = set(s.user_jobs.keys()) | set(s.user_cache_hits.keys())
        rows = []
        for chat in all_chats:
            jobs = s.user_jobs.get(chat, 0)
            hits = s.user_cache_hits.get(chat, 0)
            display = (name_resolver(chat) if name_resolver else None) or chat
            rows.append((display, jobs, hits))
        rows.sort(key=lambda r: r[1] + r[2], reverse=True)
        user_lines = ["", "<b>👤 Пользователи:</b>"]
        for display, jobs, hits in rows[:10]:
            user_lines.append(f"  {display}: {jobs} новых / {hits} cache")
        parts.append("\n".join(user_lines))

    # LLM
    if s.llm_calls:
        llm_lines = [
            "",
            "<b>🤖 LLM:</b>",
            f"  Запросов: {s.llm_calls}",
            f"  Затраты: ${s.llm_cost_usd:.4f}",
        ]
        if s.llm_prompt_tokens:
            llm_lines.append(
                f"  Токены: {s.llm_prompt_tokens:,} prompt / {s.llm_completion_tokens:,} ответ"
            )
        # Топ провайдер
        if s.llm_calls_per_provider:
            top_prov = sorted(s.llm_calls_per_provider.items(), key=lambda x: x[1], reverse=True)
            llm_lines.append("  Провайдеры: " + ", ".join(f"{p}={n}" for p, n in top_prov[:3]))
        parts.append("\n".join(llm_lines))

    # Performance
    if s.job_durations_sec:
        ds = s.job_durations_sec
        perf_lines = [
            "",
            "<b>⏱ Производительность:</b>",
            f"  Среднее: {_fmt_duration(statistics.mean(ds))}",
            f"  Медиана: {_fmt_duration(statistics.median(ds))}",
        ]
        if len(ds) >= 5:
            p95 = statistics.quantiles(sorted(ds), n=20)[-1]
            perf_lines.append(f"  p95: {_fmt_duration(p95)}")
        perf_lines.append(f"  Завершено job: {len(ds)}")
        parts.append("\n".join(perf_lines))

    # Cache
    total_jobs = sum(s.user_jobs.values())
    total_hits = sum(s.user_cache_hits.values())
    if total_jobs + total_hits > 0:
        rate = total_hits / (total_jobs + total_hits) * 100
        parts.append(
            f"\n<b>💾 Кэш:</b>\n  Hit rate: {rate:.0f}% "
            f"({total_hits}/{total_jobs + total_hits}), "
            f"сохранено {s.summaries_stored}, истекло {s.summaries_expired}"
        )

    # Channel posts
    if s.channel_posts:
        parts.append(f"\n<b>📢 Публикаций в канал:</b> {s.channel_posts}")

    # Monitoring
    if s.monitoring_scans:
        ms = [f"\n<b>🛰 Мониторинг:</b>"]
        ms.append(f"  Сканов: {s.monitoring_scans} / принято: {s.monitoring_accepted}")
        if s.monitoring_skipped:
            top_skip = sorted(s.monitoring_skipped.items(), key=lambda x: x[1], reverse=True)[:3]
            ms.append("  Пропущено: " + ", ".join(f"{r}={n}" for r, n in top_skip))
        parts.append("\n".join(ms))

    # Top channels from cache
    if summary_cache is not None:
        top = _top_channels_from_cache(summary_cache, limit=5)
        if top:
            ch_lines = ["", "<b>📺 Топ каналов в кэше:</b>"]
            for ch, n in top:
                ch_lines.append(f"  {ch}: {n}")
            parts.append("\n".join(ch_lines))

    if s.errors:
        err_lines = [f"\n<b>⚠ Ошибок:</b> {s.errors}"]
        if s.errors_by_source:
            top = sorted(s.errors_by_source.items(), key=lambda x: x[1], reverse=True)[:3]
            for source, n in top:
                err_lines.append(f"  {source}: {n}")
        parts.append("\n".join(err_lines))

    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:3997] + "..."
    return text


# ──────────────────────────── helpers ────────────────────────────

def _section_header(s: Stats) -> str:
    return f"# Аналитика бота: {_format_period(s)}"


def _format_period(s: Stats) -> str:
    if s.period_start and s.period_end:
        return f"{s.period_start.date()} → {s.period_end.date()}"
    if s.period_start:
        return f"с {s.period_start.date()}"
    return "нет данных"


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m:02d} мин"
    if m:
        return f"{m} мин {s:02d} сек"
    return f"{s} сек"


def _top_channels_from_cache(summary_cache, limit: int = 10) -> list[tuple[str, int]]:
    """Read summary_cache._entries (private but stable) to count by channel_name."""
    counter: dict[str, int] = defaultdict(int)
    try:
        for entry in summary_cache._entries.values():  # noqa: SLF001
            name = (entry.channel_name or "").strip()
            if name:
                counter[name] += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("log_analytics.cache_scan_failed error=%s", exc)
        return []
    return sorted(counter.items(), key=lambda x: x[1], reverse=True)[:limit]
