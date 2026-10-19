import { afterEach, describe, expect, it, vi } from 'vitest';

async function loadApi(key: string | undefined) {
  vi.resetModules();
  vi.stubEnv('VITE_APP_API_KEY', key ?? '');
  return import('./api');
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('downloadApiFile', () => {
  it('fetches split-origin ZIP bytes with cookies before starting a local download', async () => {
    vi.stubEnv('VITE_API_URL', 'https://lab.example.invalid');
    const blob = new Blob(['zip-bytes'], { type: 'application/zip' });
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: async () => blob });
    vi.stubGlobal('fetch', fetchMock);
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:zip-test') });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    const { downloadApiFile } = await loadApi(undefined);
    vi.useFakeTimers();
    await downloadApiFile('/api/burn/zip/batch?project=one', 'burned_batch.zip');

    expect(fetchMock).toHaveBeenCalledWith(
      'https://lab.example.invalid/api/burn/zip/batch?project=one',
      { credentials: 'include' },
    );
    expect(click).toHaveBeenCalledOnce();
    expect((click.mock.contexts[0] as unknown as HTMLAnchorElement).download).toBe('burned_batch.zip');
    vi.runAllTimers();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:zip-test');
  });
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
