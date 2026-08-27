# Разворачивание бота на сервере с гео-блоком OpenRouter/Groq

Файлы этой папки — рабочая обвязка с VPS VDSina (Москва, июль–август 2026,
удалён 2026-08-19). OpenRouter и Groq блокируют IP российских дата-центров
(резидентные домашние IP — нет); обход — Shadowsocks-прокси из динамического
Outline-ключа Paper VPN, только для httpx/yt-dlp (Telegram ходит напрямую).

## Восстановление на новом сервере (Ubuntu 24.04, ~30 минут)

1. Базовое: swap 2 ГБ (`fallocate -l 2G /swapfile && chmod 600 /swapfile &&
   mkswap /swapfile && swapon /swapfile` + строка в /etc/fstab), Docker
   (`curl -fsSL https://get.docker.com | sh`).
2. Прокси (нужен только если IP под гео-блоком — проверить:
   `curl -s -o /dev/null -w "%{http_code}" https://openrouter.ai/api/v1/models`,
   403 = блок):
   - `sslocal` из shadowsocks-rust (GitHub releases) → /usr/local/bin;
   - динамический ключ Paper VPN → `/etc/papervpn-key.url` (chmod 600);
   - `papervpn-refresh` → /usr/local/bin (chmod +x) — перечитывает ключ,
     Paper ротирует серверы (наблюдалось дважды за неделю);
   - юниты `papervpn-proxy.service`, `papervpn-refresh.service`,
     `papervpn-refresh.timer` → /etc/systemd/system;
     `systemctl daemon-reload && systemctl enable --now papervpn-proxy
     papervpn-refresh.timer`;
   - прокси слушает 172.17.0.1:8118 (docker-мост, наружу не торчит) —
     интерфейс существует только при запущенном docker.
3. Бот: rsync репозитория (без .venv/data) → /opt/youtube-summary-bot;
   `.env` и `data/` (bot.db, cookies, telegraph_token, system_prompt) — с
   прежней машины, ПОСЛЕ остановки старого инстанса (конфликт getUpdates).
   При гео-блоке добавить `vps-docker-compose.override.yml` как
   `docker-compose.override.yml` (env-прокси для контейнера).
4. `docker compose up -d --build`; проверить `billing.boot` в логах и живой
   генерацией. Для роликов без субтитров прокси обязателен и для ffmpeg —
   это уже в коде (yt-dlp `proxy` из env, см. app/youtube_service.py).

## Грабли, проверенные кровью

- Полный VPN-туннель НЕ поднимать — отрежешь себе SSH; только локальный
  HTTP-прокси + env-переменные контейнера.
- 429 у free-моделей OpenRouter — норма (цепочка переживает); 403 на всё —
  умер прокси (проверь `systemctl status papervpn-proxy` и таймер).
- SQLite: на Docker Desktop (мак) в data/bot.db нельзя писать при живом
  контейнере (virtiofs теряет WAL-записи второго писателя); на нативном
  Linux ext4 — можно.
- Пересборка контейнера при непустой очереди обрывает активную генерацию —
  всегда гейтить деплой на `SELECT COUNT(*) FROM jobs WHERE status IN
  ('queued','active')` = 0.
