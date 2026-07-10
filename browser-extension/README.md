# YouTube Summary — browser extension

Расширение под Firefox и Chromium (Chrome / Edge / Brave / Opera) на
Manifest V3. На страницах YouTube `/watch?...` добавляет кнопку
**🔮 Summary** рядом с Like / Share / Save. По клику открывает Telegram
с deep-link'ом `t.me/YouTube_Sum_mary_bot?start=<video_id>` — бот видит
`/start <video_id>`, валидирует, ставит ролик в очередь саммари и потом
шлёт результат туда же, в твой личный чат с ботом.

Дополнительно на странице открытого видео у каждого превью в сайдбаре
related-видео при наведении появляется маленькая оверлей-кнопка **🔮**
(слева сверху): клик отправляет боту именно этот ролик, не открывая его.

Никаких popup'ов — один `content_script`, небольшой background service
worker (нужен только для «тихой» генерации, см. ниже) и немного CSS.
Бот-handle сохраняется в `chrome.storage.sync` с дефолтом
`YouTube_Sum_mary_bot`. Менять через **Options** (в контекстном меню
расширения).

## Настройка Telegram-хэндла

По умолчанию расширение использует бота `@YouTube_Sum_mary_bot`. Чтобы поменять:

1. **Chrome / Edge / Brave / Opera:** правый клик на иконку расширения → **Options**
2. **Firefox:** `about:addons` → иконка расширения → **Preferences**

В поле ввода укажи хэндл бота (с `@` или без — нормализуется автоматически).
Нажми **Сохранить** — настройка запишется в `chrome.storage.sync` и будет
действовать на всех вкладках.

## Тихая генерация (Local API)

По умолчанию клик по кнопке открывает Telegram deep-link'ом (см. выше) —
браузер уходит на страницу-прослойку t.me, затем открывается desktop-клиент
и забирает фокус. Если бот работает в Docker на этом же маке, можно
настроить «тихий» путь: клик ставит ролик в очередь через локальный HTTP
API бота, фокус остаётся на YouTube, а результат всё так же приходит в
Telegram.

1. На маке, где крутится бот, сгенерируй токен:
   ```sh
   openssl rand -hex 24
   ```
2. Пропиши его в `.env` бота: `LOCAL_API_TOKEN=<токен>` (и убедись, что
   задан `OWNER_USER_ID` — без него локальный API не стартует), перезапусти
   контейнер.
3. Открой **Options** расширения (см. «Настройка Telegram-хэндла» выше) и
   вставь тот же токен в поле **Local API token** → **Сохранить**.
4. Перезагрузи расширение (`chrome://extensions` → кнопка обновить, или
   Firefox — заново Load Temporary Add-on), чтобы подхватился обновлённый
   `manifest.json`/`background.js`.

Поведение по клику:

- Токен задан и бот отвечает → кнопка на ~2 сек показывает **✅**, ролик
  уходит в очередь (или отдаётся мгновенно, если саммари уже в кэше),
  фокус остаётся на YouTube.
- Токен пуст → поведение как раньше: сразу открывается deep-link
  `t.me/...?start=<id>`, никаких fetch-запросов не делается.
- Токен задан, но бот недоступен (контейнер остановлен, сеть, таймаут
  1.5 сек) → расширение молча откатывается на тот же deep-link.

Запрос к `http://127.0.0.1:8799/enqueue` выполняет background service
worker (`background.js`), а не сам content-script на странице youtube.com:
Chrome блокирует обращения публичных страниц к loopback-адресам
(Private Network Access), у background-контекста с `host_permissions`
такого ограничения нет.

## Что делает кнопка

- **Только на `/watch?v=<id>`** — на `/playlist`, `/channel`, `/shorts`,
  главной странице и в результатах поиска кнопка не появляется вообще
  (не на чём её рисовать — `video_id` нет).
- На `/watch?v=<id>&list=<playlist>` (ролик внутри плейлиста) — кнопка
  работает обычным образом, summary будет ровно для текущего ролика.
- На клик: открывает `https://t.me/YouTube_Sum_mary_bot?start=<id>` в новой
  вкладке. Если Telegram установлен в системе — открывается desktop-клиент,
  иначе — Telegram Web.

Если в момент клика URL уже не `/watch?v=<id>` (SPA-переход или сломанный
URL) — внизу страницы появится тост «Открой ролик YouTube и нажми снова».

## Установка в Firefox (временно)

1. Открой `about:debugging#/runtime/this-firefox`.
2. **Load Temporary Add-on** → выбери файл `manifest.json` в этой папке.
3. Открой любой ролик на youtube.com — кнопка появится рядом с
   Like / Dislike / Share / Download / Save.

⚠️ Временные расширения в Firefox **сбрасываются после перезапуска
браузера**. Чтобы установить навсегда — нужно подписать XPI через
[AMO Self-Distribution](https://addons.mozilla.org/developers/addon/new)
или Firefox Developer Edition с отключённой проверкой подписи
(`xpinstall.signatures.required = false` в `about:config`).

## Установка в Chrome / Edge / Brave / Opera

1. Открой `chrome://extensions` (в Edge — `edge://extensions`).
2. Включи тумблер **Developer mode** в правом верхнем углу.
3. Нажми **Load unpacked** → выбери папку `browser-extension/`.
4. Готово, обновлять страницу YouTube не надо — `content_script` подхватится
   на следующий `yt-navigate-finish`.

## Установка как `.zip` (для удобства)

```sh
cd browser-extension
zip -r ../youtube-summary-bot-extension.zip .
# Firefox: about:debugging → Load Temporary Add-on → ZIP
# Chrome:  chrome://extensions → drag-and-drop ZIP
```

## Структура

```
browser-extension/
├── manifest.json       — MV3 декларация (FF + Chrome compatible)
├── content.js          — injection, click handler, SPA-navigation watcher
├── background.js       — service worker: fetch к локальному API бота (127.0.0.1:8799)
├── content.css         — стили кнопки и тоста (light/dark themed via YT vars)
├── options.html         — страница настроек (хэндл бота, local API token)
├── options.js           — load/save настроек в storage.sync
├── icons/
│   ├── icon-16.png     — toolbar
│   ├── icon-48.png     — addons page
│   └── icon-128.png    — store
└── README.md
```

## Где он живёт в DOM

`content.js` ищет первый существующий из этих селекторов:

```
ytd-watch-metadata #top-level-buttons-computed   ← основной таргет
ytd-menu-renderer  #top-level-buttons-computed
ytd-watch-metadata #actions                       ← старая разметка
#actions-inner     #top-level-buttons-computed
```

Если YouTube перепишет разметку — добавь новый селектор в массив `selectors`
внутри `findInjectionHost()`.

## SPA-навигация

YouTube — single-page app, URL меняется без полной перезагрузки. Скрипт
слушает:

- `yt-navigate-finish` (нативное событие YouTube'а) — основной триггер.
- `MutationObserver` на `<body>`, сравнение `location.href` — подстраховка.

После каждой смены URL — три попытки `injectButton()` с задержками
200/800/2000 мс. На второй обычно DOM уже готов.

## Совместимость с ботом

Боту на стороне `app/bot_handlers.py` нужен патч в `@router.message(Command("start"))`:
если payload (`/start <X>`) — это 11 символов `[A-Za-z0-9_-]`, бот строит URL
`https://www.youtube.com/watch?v=<X>` и зовёт `_enqueue_summary_job`. Этот
патч лежит в том же коммите, что и сам extension.

Логирование: каждый успешный заход через extension оставляет в логе строку

```
deep_link.start chat_id=... video_id=... source=browser_button
```

Через неё в `/stats` можно отдельно посчитать «сколько саммари приехало через
кнопку vs через ручное копирование URL'а в чат».
