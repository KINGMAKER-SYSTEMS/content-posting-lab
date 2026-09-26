import { afterEach, describe, expect, it, vi } from 'vitest';

async function loadApi(key: string | undefined) {
  vi.resetModules();
  vi.stubEnv('VITE_APP_API_KEY', key ?? '');
  return import('./api');
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('withApiKey', () => {
  it('adds x-api-key and keeps the rest of the request', async () => {
    const { withApiKey } = await loadApi('k-test');
    const init = withApiKey({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"a":1}',
    });
    const headers = new Headers(init.headers);
    expect(headers.get('x-api-key')).toBe('k-test');
    expect(headers.get('content-type')).toBe('application/json');
    expect(init.method).toBe('POST');
    expect(init.body).toBe('{"a":1}');
  });

  it('works with no init', async () => {
    const { withApiKey } = await loadApi('k-test');
    expect(new Headers(withApiKey().headers).get('x-api-key')).toBe('k-test');
  });

  it('does not override an explicit x-api-key', async () => {
    const { withApiKey } = await loadApi('k-test');
    const init = withApiKey({ headers: { 'x-api-key': 'explicit' } });
    expect(new Headers(init.headers).get('x-api-key')).toBe('explicit');
  });

  it('sends no header when no key is configured', async () => {
    const { withApiKey } = await loadApi(undefined);
    expect(new Headers(withApiKey({ method: 'GET' }).headers).has('x-api-key')).toBe(false);
  });
});
