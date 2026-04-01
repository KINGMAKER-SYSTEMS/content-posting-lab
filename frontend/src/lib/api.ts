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

/**
 * Fetch wrapper with standardized error handling.
 *
 * - Prepends apiUrl() to the path
 * - Parses JSON responses
 * - Extracts error messages from {error: "..."} or {detail: "..."} shapes
 * - Throws an Error with the extracted message on non-2xx responses
 */
export async function fetchApi<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(apiUrl(path), init);
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      msg = body.error || body.detail || body.message || msg;
    } catch {
      // response wasn't JSON
    }
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

/**
 * Same as fetchApi but for POST with JSON body.
 */
export async function postApi<T = unknown>(
  path: string,
  body: unknown,
  init?: RequestInit,
): Promise<T> {
  return fetchApi<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    body: JSON.stringify(body),
    ...init,
  });
}
