// YouTube Summary (Telegram bot) — background service worker
//
// Единственная задача: по сообщению от content.js выполнить fetch к
// локальному HTTP API бота (127.0.0.1:8799/enqueue). Запрос делается
// именно отсюда, а не из content-script'а: страница youtube.com — публичный
// origin, и Chrome блокирует её обращения к loopback-адресам (Private
// Network Access). У background service worker'а с host_permissions
// такого ограничения нет.
//
// Токен читается из storage.sync (ключ localApiToken, задаётся на
// options-странице). Пустой токен — сигнал, что фича выключена: сразу
// отвечаем {ok: false}, никакого fetch не делаем.

const API_URL = 'http://127.0.0.1:8799/enqueue';
const api = typeof browser !== 'undefined' ? browser : chrome;

function getToken() {
  // Тот же dual-form паттерн, что в content.js/options.js: Firefox
  // поддерживает только Promise-форму storage.sync.get, Chrome — обе.
  return new Promise((resolve) => {
    try {
      const maybe = api.storage.sync.get({ localApiToken: '' });
      if (maybe && typeof maybe.then === 'function') {
        maybe.then((items) => resolve((items && items.localApiToken) || ''));
        return;
      }
      api.storage.sync.get({ localApiToken: '' }, (items) => resolve((items && items.localApiToken) || ''));
    } catch (_) {
      resolve('');
    }
  });
}

api.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== 'yt-summary-enqueue') return false;
  (async () => {
    const token = await getToken();
    if (!token) {
      sendResponse({ ok: false });
      return;
    }
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 1500);
      const resp = await fetch(API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': token },
        body: JSON.stringify({ video_id: msg.videoId }),
        signal: controller.signal,
      });
      clearTimeout(timer);
      if (resp.ok) {
        const data = await resp.json().catch(() => ({}));
        sendResponse({ ok: true, status: data.status || 'queued' });
        return;
      }
    } catch (_) {
      // Бот выключен / контейнер не поднят / таймаут — упадём на fallback
      // в content.js (deep-link), это ожидаемый путь, не ошибка.
    }
    sendResponse({ ok: false });
  })();
  return true; // sendResponse асинхронный — держим канал открытым
});
