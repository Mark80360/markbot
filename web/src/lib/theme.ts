export type Theme = "dark" | "light";

export const THEME_STORAGE_KEY = "markbot-theme";

const THEME_COLORS: Record<Theme, string> = {
  dark: "#041c1c",
  light: "#f7f8fa",
};

export function isTheme(value: unknown): value is Theme {
  return value === "dark" || value === "light";
}

export function readStoredTheme(): Theme | null {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(stored) ? stored : null;
  } catch {
    return null;
  }
}

/** Stored preference wins; otherwise follow the OS setting. */
export function resolveInitialTheme(): Theme {
  const stored = readStoredTheme();
  if (stored) return stored;
  if (typeof window === "undefined") return "dark";
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/**
 * Paint the palette onto <html>. Called from the module entry point (before the
 * first React render) because the backend CSP forbids inline scripts, so an
 * in-document bootstrap snippet would be blocked.
 */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  root.setAttribute("data-theme", theme);
  root.style.colorScheme = theme;

  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (meta) meta.content = THEME_COLORS[theme];

  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    /* private mode / storage disabled — the in-memory value still applies */
  }
}
