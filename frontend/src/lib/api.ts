/**
 * URL utilities for split-deploy (Vercel frontend + Railway backend).
 *
 * When VITE_API_URL is set (production), all paths are prefixed with the
 * backend origin. When unset (local dev), paths are returned unchanged
 * so the Vite proxy or same-origin server handles them.
 */

const BASE = import.meta.env.VITE_API_URL?.replace(/\/+$/, '') ?? '';

/** Prefix an API path (e.g. `/api/video/providers`) with the backend URL. */
export function apiUrl(path: string): string {
  return BASE ? `${BASE}${path}` : path;
}

/**
 * Build a WebSocket URL for the given path.
 *
 * Production: converts the VITE_API_URL scheme (https → wss, http → ws).
 * Local dev:  derives from window.location as before.
 */
export function wsUrl(path: string): string {
  if (BASE) {
    const ws = BASE.replace(/^https:/, 'wss:').replace(/^http:/, 'ws:');
    return `${ws}${path}`;
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${path}`;
}

/** Prefix a static-file path (e.g. `/projects/...`, `/fonts/...`) with the backend URL. */
export function staticUrl(path: string): string {
  return BASE ? `${BASE}${path}` : path;
}
