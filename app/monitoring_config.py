from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)


DEFAULT_SCAN_TIME = "22:00"
DEFAULT_SCAN_TZ = "Europe/Moscow"
DEFAULT_MIN_DURATION_SEC = 300
DEFAULT_EXPERT_SEGMENT_THRESHOLD_SEC = 2400
DEFAULT_EXPERT_WINDOW_PRE_SEC = 60
DEFAULT_EXPERT_WINDOW_POST_SEC = 180
DEFAULT_EXPERT_MENTION_CLUSTER_GAP_SEC = 300


@dataclass
class MonitoredChannel:
    channel_id: str
    channel_url: str
    channel_name: str = ""

    def to_dict(self) -> dict[str, str]:
        data: dict[str, str] = {"channel_id": self.channel_id, "channel_url": self.channel_url}
        if self.channel_name:
            data["channel_name"] = self.channel_name
        return data


@dataclass
class MonitoringRules:
    channels: list[MonitoredChannel] = field(default_factory=list)
    shows_whitelist: list[str] = field(default_factory=list)
    shows_blacklist: list[str] = field(default_factory=list)
    experts_whitelist: list[str] = field(default_factory=list)
    experts_blacklist: list[str] = field(default_factory=list)
    min_duration_sec: int = DEFAULT_MIN_DURATION_SEC
    scan_time: str = DEFAULT_SCAN_TIME
    scan_tz: str = DEFAULT_SCAN_TZ
    expert_segment_threshold_sec: int = DEFAULT_EXPERT_SEGMENT_THRESHOLD_SEC
    expert_window_pre_sec: int = DEFAULT_EXPERT_WINDOW_PRE_SEC
    expert_window_post_sec: int = DEFAULT_EXPERT_WINDOW_POST_SEC
    expert_mention_cluster_gap_sec: int = DEFAULT_EXPERT_MENTION_CLUSTER_GAP_SEC

    def to_dict(self) -> dict[str, Any]:
        return {
            "channels": [channel.to_dict() for channel in self.channels],
            "shows": {
                "whitelist": list(self.shows_whitelist),
                "blacklist": list(self.shows_blacklist),
            },
            "experts": {
                "whitelist": list(self.experts_whitelist),
                "blacklist": list(self.experts_blacklist),
            },
            "min_duration_sec": self.min_duration_sec,
            "scan_time": self.scan_time,
            "scan_tz": self.scan_tz,
            "expert_segment_threshold_sec": self.expert_segment_threshold_sec,
            "expert_window_pre_sec": self.expert_window_pre_sec,
            "expert_window_post_sec": self.expert_window_post_sec,
            "expert_mention_cluster_gap_sec": self.expert_mention_cluster_gap_sec,
        }

    def has_channel(self, channel_id: str) -> bool:
        return any(channel.channel_id == channel_id for channel in self.channels)


SEED_YAML = """\
# Мониторинг YouTube-каналов. Бот перечитывает этот файл при каждом суточном скане.
# Можно редактировать руками или просто слать в бот ссылки на каналы — он сам их добавит.

channels: []
# Пример:
# - channel_id: UCxxxxxxxxxxxxxxxxxxxxxx
#   channel_url: https://www.youtube.com/@kanal
#   channel_name: Kanal

shows:
  whitelist: []    # подстроки, которые должны встречаться в title ИЛИ description нового видео
  blacklist: []    # подстроки, при совпадении которых видео пропускается

experts:
  whitelist: []    # имена/фамилии гостей, которых хотим ловить (можно разные склонения и оригиналы)
  blacklist: []    # имена, при совпадении которых видео пропускается

# Минимальная длительность ролика в секундах. Шортсы и короткие новости отсекаются.
min_duration_sec: 300

# Время и таймзона суточного скана.
scan_time: "22:00"
scan_tz: Europe/Moscow

# Если ролик длиннее этого порога и в нём найден эксперт из whitelist,
# бот саммаризирует только фрагмент с этим экспертом, а не весь ролик.
expert_segment_threshold_sec: 2400

# Сколько секунд захватывать до первого и после последнего упоминания эксперта.
expert_window_pre_sec: 60
expert_window_post_sec: 180

# Максимальный зазор между упоминаниями имени, при котором они считаются одним кластером.
expert_mention_cluster_gap_sec: 300
"""


class MonitoringConfig:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._rules = MonitoringRules()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def rules(self) -> MonitoringRules:
        return self._rules

    def load(self) -> MonitoringRules:
        if not self._path.exists():
            logger.info("monitoring.config.seed path=%s", self._path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(SEED_YAML, encoding="utf-8")
            self._rules = MonitoringRules()
            return self._rules

        try:
            raw = self._path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw) or {}
        except Exception as exc:
            logger.warning("monitoring.config.load_failed path=%s error=%s", self._path, exc)
            self._rules = MonitoringRules()
            return self._rules

        self._rules = _parse_rules(data)
        logger.info(
            "monitoring.config.loaded path=%s channels=%s shows_wl=%s experts_wl=%s",
            self._path,
            len(self._rules.channels),
            len(self._rules.shows_whitelist),
            len(self._rules.experts_whitelist),
        )
        return self._rules

    def save(self) -> None:
        payload = self._rules.to_dict()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(
                payload,
                fh,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
        logger.info("monitoring.config.saved path=%s channels=%s", self._path, len(self._rules.channels))

    def add_channel(self, channel: MonitoredChannel) -> bool:
        if self._rules.has_channel(channel.channel_id):
            return False
        self._rules.channels.append(channel)
        self.save()
        return True


def _parse_rules(data: dict[str, Any]) -> MonitoringRules:
    channels_raw = data.get("channels") or []
    channels: list[MonitoredChannel] = []
    for item in channels_raw:
        if not isinstance(item, dict):
            continue
        channel_id = str(item.get("channel_id", "")).strip()
        channel_url = str(item.get("channel_url", "")).strip()
        channel_name = str(item.get("channel_name", "")).strip()
        if not channel_id or not channel_url:
            logger.warning("monitoring.config.skip_channel entry=%r", item)
            continue
        channels.append(
            MonitoredChannel(
                channel_id=channel_id, channel_url=channel_url, channel_name=channel_name
            )
        )

    shows = data.get("shows") or {}
    experts = data.get("experts") or {}

    return MonitoringRules(
        channels=channels,
        shows_whitelist=_string_list(shows.get("whitelist")),
        shows_blacklist=_string_list(shows.get("blacklist")),
        experts_whitelist=_string_list(experts.get("whitelist")),
        experts_blacklist=_string_list(experts.get("blacklist")),
        min_duration_sec=int(data.get("min_duration_sec", DEFAULT_MIN_DURATION_SEC)),
        scan_time=str(data.get("scan_time", DEFAULT_SCAN_TIME)).strip() or DEFAULT_SCAN_TIME,
        scan_tz=str(data.get("scan_tz", DEFAULT_SCAN_TZ)).strip() or DEFAULT_SCAN_TZ,
        expert_segment_threshold_sec=int(
            data.get("expert_segment_threshold_sec", DEFAULT_EXPERT_SEGMENT_THRESHOLD_SEC)
        ),
        expert_window_pre_sec=int(data.get("expert_window_pre_sec", DEFAULT_EXPERT_WINDOW_PRE_SEC)),
        expert_window_post_sec=int(data.get("expert_window_post_sec", DEFAULT_EXPERT_WINDOW_POST_SEC)),
        expert_mention_cluster_gap_sec=int(
            data.get("expert_mention_cluster_gap_sec", DEFAULT_EXPERT_MENTION_CLUSTER_GAP_SEC)
        ),
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
