(() => {
  "use strict";

  const root = document.documentElement;
  const sessionKey = "hydra-admin-session";
  const themeKey = "hydra-admin-theme";
  const fragmentPattern = /^#token=([A-Za-z0-9_-]{43})$/;
  const routePattern = /^#(?:overview|tasks|compare|health|evidence)$/;
  let credential = null;

  const restoreSession = () => {
    try {
      const candidate = sessionStorage.getItem(sessionKey);
      return candidate && /^[A-Za-z0-9_-]{43}$/.test(candidate) ? candidate : null;
    } catch (_) {
      return null;
    }
  };

  const fragment = window.location.hash;
  const match = fragmentPattern.exec(fragment);
  if (match) {
    credential = match[1];
    try {
      sessionStorage.setItem(sessionKey, credential);
    } catch (_) {
      // The current page keeps the handoff in memory.
    }
    history.replaceState(null, "", window.location.pathname + window.location.search);
  } else if (routePattern.test(fragment) || !fragment) {
    credential = restoreSession();
  } else if (fragment) {
    credential = null;
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }

  let storedTheme = null;
  try {
    const candidate = localStorage.getItem(themeKey);
    if (candidate === "light" || candidate === "dark") storedTheme = candidate;
  } catch (_) {
    storedTheme = null;
  }
  if (storedTheme) root.dataset.theme = storedTheme;

  const clearCredential = () => {
    credential = null;
    try { sessionStorage.removeItem(sessionKey); } catch (_) { /* memory is cleared */ }
  };
  const takeCredential = () => credential;

  const initializeTheme = () => {
    const button = document.getElementById("theme-button");
    if (!button || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const current = () => root.dataset.theme || (media.matches ? "dark" : "light");
    const sync = () => {
      const next = current() === "dark" ? "light" : "dark";
      button.textContent = next === "dark" ? "Dark theme" : "Light theme";
      button.setAttribute("aria-label", `Switch to ${next} theme`);
    };
    button.addEventListener("click", () => {
      const next = current() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try { localStorage.setItem(themeKey, next); } catch (_) { /* memory only */ }
      sync();
    });
    const followSystem = () => { if (!root.dataset.theme) sync(); };
    if (typeof media.addEventListener === "function") media.addEventListener("change", followSystem);
    sync();
  };

  document.addEventListener("DOMContentLoaded", () => {
    initializeTheme();
    window.dispatchEvent(new CustomEvent("hydra-dashboard-ready", {
      detail: Object.freeze({takeCredential, clearCredential}),
    }));
  }, {once: true});
})();
