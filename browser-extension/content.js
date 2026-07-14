// YouTube Summary (Telegram bot) — content script
//
// Что делает: на странице /watch?... добавляет кнопку «🔮 Summary» рядом
// с Like/Share/Save, а на превью видео по всему YouTube (главная, подписки,
// поиск, каналы, related-сайдбар) — маленькую оверлей-кнопку «🔮».
// По клику открывает Telegram через deep-link с
// payload'ом /start <video_id>. Бот видит payload, валидирует как
// 11-символьный YouTube ID и кладёт ролик в очередь саммари.
//
// Хэндл бота настраивается на options-странице расширения и хранится
// в storage.sync; если ничего не сохранено — используется дефолт
// DEFAULT_BOT_HANDLE (@YouTube_Sum_mary_bot).

(() => {
  'use strict';

  // Маркер версии content script'а: виден со страницы, позволяет отличить
  // «код не перезагрузился» от «в новом коде баг» при отладке.
  const SCRIPT_VERSION = '0.2.6';
  document.documentElement.dataset.ytSummaryExt = SCRIPT_VERSION;

  const api = globalThis.browser ?? globalThis.chrome;
  const DEFAULT_BOT_HANDLE = 'YouTube_Sum_mary_bot';
  const BUTTON_ID = 'yt-summary-bot-btn';
  const TOAST_ID = 'yt-summary-bot-toast';
  // YouTube video_id: ровно 11 символов из base64url-алфавита.
  const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;

  function getBotHandle() {
    // Firefox: browser.storage.sync.get — Promise-only, callback-форму
    // молча игнорирует. Chrome: поддерживает обе. Поэтому сначала зовём
    // без callback'а; если вернулся thenable — работаем как с Promise,
    // иначе повторяем вызов в callback-стиле.
    return new Promise((resolve) => {
      const done = (items) =>
        resolve((items && items.botHandle) || DEFAULT_BOT_HANDLE);
      try {
        const maybe = api.storage.sync.get({ botHandle: DEFAULT_BOT_HANDLE });
        if (maybe && typeof maybe.then === 'function') {
          maybe.then(done, () => resolve(DEFAULT_BOT_HANDLE));
        } else {
          api.storage.sync.get({ botHandle: DEFAULT_BOT_HANDLE }, done);
        }
      } catch {
        resolve(DEFAULT_BOT_HANDLE);
      }
    });
  }

  // Пытается поставить ролик через локальный API бота (background SW
  // делает fetch — обходит CORS/Private-Network-Access-ограничения на
  // запросы страницы youtube.com к 127.0.0.1). true — успех, кнопка
  // остаётся на YouTube; false — нужен deep-link fallback (токен не
  // задан в options, бот выключен/недоступен или таймаут).
  function tryLocalEnqueue(videoId) {
    return new Promise((resolve) => {
      try {
        api.runtime.sendMessage({ type: 'yt-summary-enqueue', videoId }, (resp) => {
          if (api.runtime.lastError || !resp) { resolve(false); return; }
          resolve(!!resp.ok);
        });
      } catch {
        resolve(false);
      }
    });
  }

  // Показывает «✅» на кнопке ~2 сек, затем возвращает исходный текст.
  function flashButton(btn, originalText) {
    btn.textContent = '✅';
    setTimeout(() => { btn.textContent = originalText; }, 2000);
  }

  // ───────────────────────────── helpers ─────────────────────────────

  function extractVideoId() {
    // Только формат /watch?v=...; youtu.be для UI YouTube не релевантен.
    // Если v= отсутствует или невалиден — return null (например, юзер
    // на /playlist?list=..., /channel/..., /shorts/... — там кнопку
    // вообще не показываем).
    try {
      const u = new URL(window.location.href);
      if (u.pathname !== '/watch') return null;
      const v = u.searchParams.get('v');
      if (v && VIDEO_ID_RE.test(v)) return v;
    } catch (e) {
      // Битый URL — игнорим.
    }
    return null;
  }

  function showToast(text) {
    const old = document.getElementById(TOAST_ID);
    if (old) old.remove();
    const t = document.createElement('div');
    t.id = TOAST_ID;
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(() => {
      if (t.parentNode) t.parentNode.removeChild(t);
    }, 3500);
  }

  function buildButton() {
    const btn = document.createElement('button');
    btn.id = BUTTON_ID;
    btn.type = 'button';
    btn.title = 'Получить саммари ролика в Telegram';
    btn.textContent = '🔮 Summary';
    btn.addEventListener('click', async () => {
      // Видео могло смениться между injection'ом и кликом (SPA), поэтому
      // ID добываем заново в момент нажатия.
      const id = extractVideoId();
      if (!id) {
        showToast('Открой ролик YouTube и нажми снова — это не страница с видео.');
        return;
      }
      const okLocal = await tryLocalEnqueue(id);
      if (okLocal) {
        flashButton(btn, '🔮 Summary');
        return;
      }
      const handle = await getBotHandle();
      const url = `https://telegram.me/${handle}?start=${encodeURIComponent(id)}`;
      window.open(url, '_blank', 'noopener,noreferrer');
    });
    return btn;
  }

  // ───────────────────────────── injection ─────────────────────────────

  function findInjectionHost() {
    // YouTube переписывает разметку чаще, чем хотелось бы. Перебираем
    // несколько селекторов от самого нового к более старым. Селектор может
    // совпасть с десятками узлов (например, menu-renderer есть у каждой
    // карточки сайдбара) и с узлами в скрытых деревьях SPA — берём
    // **первый ВИДИМЫЙ** узел (у display:none поддеревьев нет client rects).
    const selectors = [
      // 2024–2026: top-level buttons row внутри ytd-watch-metadata.
      'ytd-watch-metadata #top-level-buttons-computed',
      'ytd-menu-renderer #top-level-buttons-computed',
      // Старые варианты — на всякий случай.
      'ytd-watch-metadata #actions',
      '#actions-inner #top-level-buttons-computed',
    ];
    for (const sel of selectors) {
      for (const node of document.querySelectorAll(sel)) {
        if (node.getClientRects().length > 0) return node;
      }
    }
    return null;
  }

  function injectButton() {
    const existing = document.getElementById(BUTTON_ID);
    if (existing) {
      if (existing.getClientRects().length > 0) return; // на месте и видима
      // Кнопка застряла в скрытом дереве SPA (YouTube держит в DOM
      // несколько layout-деревьев) — убираем и инжектим заново в видимое.
      existing.remove();
    }
    const id = extractVideoId();
    if (!id) return; // не /watch — нечего делать
    const host = findInjectionHost();
    if (!host) return; // DOM ещё не готов, повторим позже
    host.appendChild(buildButton());
  }

  function uninjectButton() {
    const btn = document.getElementById(BUTTON_ID);
    if (btn && btn.parentNode) btn.parentNode.removeChild(btn);
  }

  // ─────────────── preview buttons (карточки видео на любой странице) ───────────────
  //
  // На каждое превью видео вешаем оверлей-кнопку «🔮»: клик отправляет
  // ЭТОТ ролик боту, не открывая его. YouTube часто переименовывает свои
  // рендереры (ytd-compact-video-renderer → yt-lockup-view-model, ...),
  // поэтому к именам не привязываемся: берём все ссылки на /watch?v=...,
  // содержащие картинку-превью.

  const PREVIEW_BTN_CLASS = 'yt-summary-preview-btn';
  const PREVIEW_HOST_CLASS = 'yt-summary-preview-host';
  const PREVIEW_VISIBLE_CLASS = 'yt-summary-preview-visible';

  // Показ кнопки по наведению делаем из JS, а не чистым CSS :hover: на
  // главной YouTube поверх ссылки-превью монтируется inline-плеер
  // (<video class="html5-main-video">), который НЕ потомок ссылки — :hover
  // на неё не срабатывает, и кнопка навсегда остаётся с opacity:0.
  // elementsFromPoint() возвращает весь стек под курсором, включая
  // перекрытый хост, поэтому работает и сквозь плеер.
  let visiblePreviewHost = null;
  let lastPointerX = -1;
  let lastPointerY = -1;

  function updateHoveredPreviewHost(x, y) {
    let host = null;
    if (x >= 0 && y >= 0) {
      for (const el of document.elementsFromPoint(x, y)) {
        if (el.classList && el.classList.contains(PREVIEW_HOST_CLASS)) {
          host = el;
          break;
        }
      }
    }
    if (host === visiblePreviewHost) return;
    if (visiblePreviewHost) visiblePreviewHost.classList.remove(PREVIEW_VISIBLE_CLASS);
    visiblePreviewHost = host;
    if (host) host.classList.add(PREVIEW_VISIBLE_CLASS);
  }

  // mouseover срабатывает при каждой смене элемента под курсором — этого
  // достаточно (вход на карточку, монтирование/размонтирование плеера).
  document.addEventListener('mouseover', (ev) => {
    lastPointerX = ev.clientX;
    lastPointerY = ev.clientY;
    updateHoveredPreviewHost(lastPointerX, lastPointerY);
  }, true);
  // Курсор ушёл из окна — прячем.
  document.documentElement.addEventListener('mouseleave', () => {
    updateHoveredPreviewHost(-1, -1);
  });
  // При скролле элемент под неподвижным курсором меняется без mouse-событий;
  // пересчитываем по последним координатам, не чаще кадра.
  let hoverScanQueued = false;
  document.addEventListener('scroll', () => {
    if (hoverScanQueued) return;
    hoverScanQueued = true;
    requestAnimationFrame(() => {
      hoverScanQueued = false;
      updateHoveredPreviewHost(lastPointerX, lastPointerY);
    });
  }, {capture: true, passive: true});

  function extractIdFromHref(href) {
    try {
      const u = new URL(href, location.origin);
      if (u.pathname === '/watch') {
        const v = u.searchParams.get('v');
        if (v && VIDEO_ID_RE.test(v)) return v;
      }
    } catch (e) {
      // Битый href — пропускаем.
    }
    return null;
  }

  function buildPreviewButton(anchor) {
    const btn = document.createElement('button');
    btn.className = PREVIEW_BTN_CLASS;
    btn.type = 'button';
    btn.title = 'Получить саммари этого ролика в Telegram';
    btn.textContent = '🔮';
    btn.addEventListener('click', async (ev) => {
      // Не даём клику провалиться в ссылку превью и открыть сам ролик.
      ev.preventDefault();
      ev.stopPropagation();
      // href читаем в момент клика: YouTube переиспользует DOM-ноды
      // сайдбара при SPA-переходах, и ссылка могла смениться.
      const id = extractIdFromHref(anchor.href);
      if (!id) return;
      const originalText = btn.textContent;
      const okLocal = await tryLocalEnqueue(id);
      if (okLocal) {
        flashButton(btn, originalText);
        return;
      }
      const handle = await getBotHandle();
      const url = `https://telegram.me/${handle}?start=${encodeURIComponent(id)}`;
      window.open(url, '_blank', 'noopener,noreferrer');
    });
    return btn;
  }

  function injectPreviewButtons() {
    // YouTube-SPA держит в DOM несколько деревьев разметки одновременно
    // (старый/новый layout, кэш предыдущих страниц), а id вроде #secondary
    // дублируются — привязываться к контейнеру ненадёжно (первый #secondary
    // может оказаться пустым деревом неактивного layout'а). Поэтому берём
    // все ВИДИМЫЕ превью-ссылки на /watch?v= по всему документу: скрытые
    // деревья отсекаются по getClientRects() (у display:none поддеревьев
    // их нет), сам плеер — по closest().
    // Самолечение: убираем кнопки, оказавшиеся на анкорах-обёртках (внешний
    // a#wc-endpoint в очереди/плейлистах оборачивает внутренний a#thumbnail).
    // Такие кнопки могли остаться от прежних версий скрипта или появиться
    // после пере-вложения разметки при SPA-переходах.
    for (const btn of document.querySelectorAll(`.${PREVIEW_BTN_CLASS}`)) {
      const host = btn.closest('a');
      if (host && host.querySelector('a[href*="/watch?v="]')) {
        btn.remove();
        host.classList.remove(PREVIEW_HOST_CLASS);
      }
    }

    const anchors = document.querySelectorAll('a[href*="/watch?v="]');
    for (const anchor of anchors) {
      // Нужны именно превью (ссылки с картинкой), а не текстовые заголовки.
      if (!anchor.querySelector('img, yt-image')) continue;
      // В некоторых панелях (очередь/плейлист) анкоры ВЛОЖЕНЫ: внешний
      // a#wc-endpoint оборачивает весь элемент, внутри — свой a#thumbnail.
      // Оба матчат селектор и оба содержат ту же картинку — кнопка выходила
      // дважды. Правило: кнопку вешаем только на самый внутренний анкор,
      // внешние обёртки пропускаем.
      if (anchor.querySelector('a[href*="/watch?v="]')) continue;
      // Мимо: сам плеер, мелкие миниатюры в дропдауне уведомлений,
      // hover-плеер карточки (кнопка перекрывалась бы его контролами).
      if (anchor.closest('#player, ytd-player, ytd-notification-renderer, ytd-video-preview')) continue;
      if (anchor.getClientRects().length === 0) continue;
      if (anchor.querySelector(`.${PREVIEW_BTN_CLASS}`)) continue; // уже есть
      if (!extractIdFromHref(anchor.href)) continue;
      anchor.classList.add(PREVIEW_HOST_CLASS);
      anchor.appendChild(buildPreviewButton(anchor));
    }
  }

  function onRouteChange() {
    // YouTube — SPA, DOM собирается асинхронно. На каждом событии делаем
    // несколько попыток injection с разными задержками — обычно достаточно.
    // Главная кнопка живёт только на /watch; превью-кнопки — на любой странице.
    if (extractVideoId()) {
      setTimeout(injectButton, 200);
      setTimeout(injectButton, 800);
      setTimeout(injectButton, 2000);
    } else {
      uninjectButton();
    }
    // Сетка карточек/сайдбар собираются позже основной разметки.
    setTimeout(injectPreviewButtons, 800);
    setTimeout(injectPreviewButtons, 2500);
  }

  // ───────────────────────────── SPA events ─────────────────────────────

  // YouTube эмитит "yt-navigate-finish" по окончанию SPA-перехода.
  window.addEventListener('yt-navigate-finish', onRouteChange);

  // Подстраховка: если событие не прилетит — следим за изменением
  // location.href через MutationObserver на body.
  let lastUrl = location.href;
  // Related-список подгружается лениво при скролле — дозакидываем кнопки
  // на новые превью по мутациям DOM, но не чаще раза в секунду.
  let previewScanQueued = false;
  const urlObserver = new MutationObserver(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      onRouteChange();
    }
    if (!previewScanQueued) {
      previewScanQueued = true;
      setTimeout(() => {
        previewScanQueued = false;
        injectPreviewButtons();
        // Главную кнопку тоже перепроверяем: YouTube может подменить
        // активное layout-дерево без смены URL, и кнопка «уедет» в скрытое.
        injectButton();
      }, 1000);
    }
  });
  urlObserver.observe(document.body, {childList: true, subtree: true});

  // First-load: страница могла открыться сразу на /watch.
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    onRouteChange();
  } else {
    document.addEventListener('DOMContentLoaded', onRouteChange);
  }
})();
