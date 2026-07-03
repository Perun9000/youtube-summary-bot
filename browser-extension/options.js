const api = globalThis.browser ?? globalThis.chrome;
const DEFAULT_HANDLE = "YouTube_Sum_mary_bot";

function load() {
  api.storage.sync.get({ botHandle: DEFAULT_HANDLE }, (items) => {
    document.getElementById("handle").value = items.botHandle;
  });
}

document.getElementById("save").addEventListener("click", () => {
  const value = document.getElementById("handle").value.trim().replace(/^@/, "") || DEFAULT_HANDLE;
  api.storage.sync.set({ botHandle: value }, () => {
    const status = document.getElementById("status");
    status.textContent = "Сохранено";
    setTimeout(() => (status.textContent = ""), 1500);
  });
});

load();
