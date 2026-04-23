import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';

// ═══════════════════════════════════════════════════════════════════════
// Theme Provider — light/dark toggle
//
// Storage: localStorage['rt-theme'] = 'light' | 'dark'
// Default: 'dark' on first visit (ignores prefers-color-scheme by design —
//          this is a dashboard, dark is the intended brand experience).
// Mechanism: toggles `.dark` class on <html>. All token CSS lives in
//            index.css (:root = light, .dark = dark).
// ═══════════════════════════════════════════════════════════════════════

export type Theme = 'light' | 'dark';

const STORAGE_KEY = 'rt-theme';
const DEFAULT_THEME: Theme = 'dark';

interface ThemeContextValue {
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function readStoredTheme(): Theme {
  if (typeof window === 'undefined') return DEFAULT_THEME;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === 'light' || raw === 'dark') return raw;
  } catch {
    // localStorage may be disabled (private mode, SSR, etc.)
  }
  return DEFAULT_THEME;
}

function applyThemeToDocument(theme: Theme) {
  if (typeof document === 'undefined') return;
  const html = document.documentElement;
  if (theme === 'dark') {
    html.classList.add('dark');
  } else {
    html.classList.remove('dark');
  }
  html.setAttribute('data-theme', theme);
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() => readStoredTheme());

  // Apply theme class on mount + whenever it changes.
  useEffect(() => {
    applyThemeToDocument(theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // localStorage unavailable — in-memory state still works
    }
  }, []);

  const toggleTheme = useCallback(() => {
    setThemeState((prev) => {
      const next: Theme = prev === 'dark' ? 'light' : 'dark';
      try {
        window.localStorage.setItem(STORAGE_KEY, next);
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    throw new Error('useTheme must be used within ThemeProvider');
  }
  return ctx;
}
