const api = globalThis.browser ?? globalThis.chrome;
const DEFAULT_HANDLE = "YouTube_Sum_mary_bot";

// Firefox: browser.storage.sync.* — Promise-only, callback-форму молча
// игнорирует. Chrome: поддерживает обе. Поэтому сначала зовём без
// callback'а; если вернулся thenable — работаем как с Promise, иначе
// повторяем вызов в callback-стиле.
function storageSyncGet(defaults) {
  return new Promise((resolve, reject) => {
    try {
      const maybe = api.storage.sync.get(defaults);
      if (maybe && typeof maybe.then === "function") {
        maybe.then(resolve, reject);
      } else {
        api.storage.sync.get(defaults, resolve);
      }
    } catch (e) {
      reject(e);
    }
  });
}

function storageSyncSet(items) {
  return new Promise((resolve, reject) => {
    try {
      const maybe = api.storage.sync.set(items);
      if (maybe && typeof maybe.then === "function") {
        maybe.then(resolve, reject);
      } else {
        api.storage.sync.set(items, resolve);
      }
    } catch (e) {
      reject(e);
    }
  });
}

function load() {
  storageSyncGet({ botHandle: DEFAULT_HANDLE })
    .then((items) => {
      document.getElementById("handle").value =
        (items && items.botHandle) || DEFAULT_HANDLE;
    })
    .catch(() => {
      document.getElementById("handle").value = DEFAULT_HANDLE;
    });
}

document.getElementById("save").addEventListener("click", () => {
  const value = document.getElementById("handle").value.trim().replace(/^@/, "") || DEFAULT_HANDLE;
  const status = document.getElementById("status");
  storageSyncSet({ botHandle: value })
    .then(() => {
      status.textContent = "Сохранено";
      setTimeout(() => (status.textContent = ""), 1500);
    })
    .catch(() => {
      status.textContent = "Ошибка сохранения";
      setTimeout(() => (status.textContent = ""), 3000);
    });
});

load();
