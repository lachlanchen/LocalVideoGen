"use strict";

(() => {
  const storageKey = "h3-studio-theme";
  const allowedThemes = new Set(["system", "light", "dark"]);
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";

  try {
    const stored = window.localStorage.getItem(storageKey);
    if (allowedThemes.has(stored)) preference = stored;
  } catch {
    // Storage can be unavailable in hardened private browsing contexts.
  }

  function resolvedTheme() {
    return preference === "system" ? (systemDark.matches ? "dark" : "light") : preference;
  }

  function applyTheme(nextPreference, { persist = false } = {}) {
    preference = allowedThemes.has(nextPreference) ? nextPreference : "system";
    if (persist) {
      try {
        window.localStorage.setItem(storageKey, preference);
      } catch {
        // The selected theme still applies for this page when storage is blocked.
      }
    }

    const resolved = resolvedTheme();
    const root = document.documentElement;
    root.dataset.theme = resolved;
    root.dataset.themePreference = preference;
    root.style.colorScheme = resolved;
    document.querySelector("#themeColor")?.setAttribute("content", resolved === "dark" ? "#0b0d11" : "#f6f4ef");
    const detail = { preference, resolved };
    root.dispatchEvent(new CustomEvent("h3-theme-change", { detail }));
    return detail;
  }

  const handleSystemThemeChange = () => {
    if (preference === "system") applyTheme(preference);
  };
  if (typeof systemDark.addEventListener === "function") {
    systemDark.addEventListener("change", handleSystemThemeChange);
  } else if (typeof systemDark.addListener === "function") {
    systemDark.addListener(handleSystemThemeChange);
  }

  window.H3Theme = Object.freeze({
    getState: () => ({ preference, resolved: resolvedTheme() }),
    setPreference: (nextPreference) => applyTheme(nextPreference, { persist: true }),
  });
  applyTheme(preference);
})();
