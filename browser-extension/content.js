// YouTube Summary (Telegram bot) — content script
//
// Что делает: на странице /watch?... добавляет кнопку «📚 Summary» рядом
// с Like/Share/Save. По клику открывает Telegram через deep-link с
// payload'ом /start <video_id>. Бот видит payload, валидирует как
// 11-символьный YouTube ID и кладёт ролик в очередь саммари.
//
// Бот: @YouTube_Sum_mary_bot. Чтобы поменять — отредактируй BOT_HANDLE
// ниже и перезагрузи extension. Никаких popup-настроек нет — этот
// extension личный, не для распространения.

(() => {
  'use strict';

  const api = globalThis.browser ?? globalThis.chrome;
  const DEFAULT_BOT_HANDLE = 'YouTube_Sum_mary_bot';
  const BUTTON_ID = 'yt-summary-bot-btn';
  const TOAST_ID = 'yt-summary-bot-toast';
  // YouTube video_id: ровно 11 символов из base64url-алфавита.
  const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;

  function getBotHandle() {
    return new Promise((resolve) => {
      try {
        api.storage.sync.get({ botHandle: DEFAULT_BOT_HANDLE }, (items) =>
          resolve(items.botHandle || DEFAULT_BOT_HANDLE)
        );
      } catch {
        resolve(DEFAULT_BOT_HANDLE);
      }
    });
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
    btn.textContent = '📚 Summary';
    btn.addEventListener('click', async () => {
      // Видео могло смениться между injection'ом и кликом (SPA), поэтому
      // ID добываем заново в момент нажатия.
      const id = extractVideoId();
      if (!id) {
        showToast('Открой ролик YouTube и нажми снова — это не страница с видео.');
        return;
      }
      const handle = await getBotHandle();
      const url = `https://t.me/${handle}?start=${encodeURIComponent(id)}`;
      window.open(url, '_blank', 'noopener,noreferrer');
    });
    return btn;
  }

  // ───────────────────────────── injection ─────────────────────────────

  function findInjectionHost() {
    // YouTube переписывает разметку чаще, чем хотелось бы. Перебираем
    // несколько селекторов от самого нового к более старым. Берём
    // **первый существующий** — туда и кладём кнопку.
    const selectors = [
      // 2024–2026: top-level buttons row внутри ytd-watch-metadata.
      'ytd-watch-metadata #top-level-buttons-computed',
      'ytd-menu-renderer #top-level-buttons-computed',
      // Старые варианты — на всякий случай.
      'ytd-watch-metadata #actions',
      '#actions-inner #top-level-buttons-computed',
    ];
    for (const sel of selectors) {
      const node = document.querySelector(sel);
      if (node) return node;
    }
    return null;
  }

  function injectButton() {
    if (document.getElementById(BUTTON_ID)) return; // уже на месте
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

  function onRouteChange() {
    // YouTube — SPA, DOM собирается асинхронно. На каждом событии делаем
    // 3 попытки injection с разными задержками — обычно достаточно.
    if (!extractVideoId()) {
      uninjectButton();
      return;
    }
    setTimeout(injectButton, 200);
    setTimeout(injectButton, 800);
    setTimeout(injectButton, 2000);
  }

  // ───────────────────────────── SPA events ─────────────────────────────

  // YouTube эмитит "yt-navigate-finish" по окончанию SPA-перехода.
  window.addEventListener('yt-navigate-finish', onRouteChange);

  // Подстраховка: если событие не прилетит — следим за изменением
  // location.href через MutationObserver на body.
  let lastUrl = location.href;
  const urlObserver = new MutationObserver(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      onRouteChange();
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
