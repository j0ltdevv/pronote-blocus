/* Page hors ligne : rechargement manuel et enregistrement du service worker. */
document.querySelector("#retry").addEventListener("click", () => {
  window.location.reload();
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
