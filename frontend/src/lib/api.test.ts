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
  it('keeps same-origin ZIP downloads in the browser streaming path', async () => {
    vi.stubEnv('VITE_API_URL', '');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const { downloadApiFile } = await loadApi(undefined);

    await downloadApiFile('/api/burn/zip/batch?project=one', 'burned_batch.zip');

    expect(click).toHaveBeenCalledOnce();
    expect((click.mock.contexts[0] as unknown as HTMLAnchorElement).download).toBe('burned_batch.zip');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('streams split-origin ZIP bytes to disk with the Access cookie and browser Origin', async () => {
    vi.stubEnv('VITE_API_URL', 'https://lab.example.invalid');
    const chunks: Uint8Array[] = [];
    const writable = new WritableStream<Uint8Array>({ write: (chunk) => { chunks.push(chunk); } });
    const picker = vi.fn().mockResolvedValue({ createWritable: async () => writable });
    vi.stubGlobal('showSaveFilePicker', picker);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('zip-bytes'));
          controller.close();
        },
      }),
      blob: vi.fn(() => { throw new Error('ZIP must never buffer into a Blob'); }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const { downloadApiFile } = await loadApi(undefined);
    await downloadApiFile('/api/burn/zip/batch?project=one', 'burned_batch.zip');

    expect(picker).toHaveBeenCalledWith({ suggestedName: 'burned_batch.zip' });
    expect(picker.mock.invocationCallOrder[0]).toBeLessThan(fetchMock.mock.invocationCallOrder[0]);
    expect(fetchMock).toHaveBeenCalledWith(
      'https://lab.example.invalid/api/burn/zip/batch?project=one',
      { credentials: 'include' },
    );
    expect(new TextDecoder().decode(chunks[0])).toBe('zip-bytes');
  });

  it('refuses split-origin ZIPs when stream-to-disk is unavailable', async () => {
    vi.stubEnv('VITE_API_URL', 'https://lab.example.invalid');
    vi.stubGlobal('showSaveFilePicker', undefined);
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const { downloadApiFile } = await loadApi(undefined);

    await expect(downloadApiFile('/api/burn/zip/batch', 'batch.zip'))
      .rejects.toThrow('cannot stream cross-origin downloads to disk');
    expect(fetchMock).not.toHaveBeenCalled();
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
