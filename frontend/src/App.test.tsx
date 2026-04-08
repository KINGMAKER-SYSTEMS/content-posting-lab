import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import App from './App';

let projectsPayload = [
  {
    name: 'quick-test',
    path: '/tmp/projects/quick-test',
    video_count: 0,
    caption_count: 0,
    burned_count: 0,
  },
];

const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
  const url = String(input);

  if (url.includes('/api/projects')) {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        projects: projectsPayload,
      }),
      text: async () => '',
    } as Response;
  }

  if (url.includes('/api/health')) {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        status: 'ok',
        ffmpeg: true,
        ytdlp: true,
        providers: { fal: true },
      }),
      text: async () => '',
    } as Response;
  }

  if (url.includes('/api/video/providers')) {
    return {
      ok: true,
      status: 200,
      json: async () => [],
      text: async () => '[]',
    } as Response;
  }

  if (url.includes('/api/video/prompts') || url.includes('/api/burn/')) {
    return {
      ok: true,
      status: 200,
      json: async () => [],
      text: async () => '[]',
    } as Response;
  }

  return {
    ok: true,
    status: 200,
    json: async () => ({}),
    text: async () => '',
  } as Response;
});

beforeEach(() => {
  projectsPayload = [
    {
      name: 'quick-test',
      path: '/tmp/projects/quick-test',
      video_count: 0,
      caption_count: 0,
      burned_count: 0,
    },
  ];
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockClear();
  window.localStorage.clear();
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('App', () => {
  it('renders shell with pipeline nav tabs', async () => {
    render(<App />);

    expect(screen.getByText('Content Posting Lab')).toBeTruthy();

    // Pipeline nav tabs
    await waitFor(() => {
      expect(screen.getByRole('link', { name: /Home/i })).toBeTruthy();
      expect(screen.getByRole('link', { name: /Create/i })).toBeTruthy();
      expect(screen.getByRole('link', { name: /Captions/i })).toBeTruthy();
      expect(screen.getByRole('link', { name: /Distribute/i })).toBeTruthy();
    });

    // Home page is always mounted and visible
    const main = document.querySelector('main');
    expect(main).toBeTruthy();
    const tabPanels = main!.querySelectorAll(':scope > div');
    expect(tabPanels.length).toBeGreaterThanOrEqual(1);
    expect((tabPanels[0] as HTMLElement).style.display).toBe('block');
  });

  it('loads health and projects on startup', async () => {
    render(<App />);

    await waitFor(() => {
      const requested = fetchMock.mock.calls.map((call) => String(call[0]));
      expect(requested.some((url) => url.includes('/api/projects'))).toBeTruthy();
      expect(requested.some((url) => url.includes('/api/health'))).toBeTruthy();
    });
  });

  it('hydrates and preserves active project from localStorage when present', async () => {
    projectsPayload = [
      {
        name: 'persisted-project',
        path: '/tmp/projects/persisted-project',
        video_count: 3,
        caption_count: 2,
        burned_count: 1,
      },
    ];
    window.localStorage.setItem('activeProjectName', projectsPayload[0].name);

    render(<App />);

    await waitFor(() => {
      expect(screen.getAllByText('persisted-project').length).toBeGreaterThan(0);
    });
  });
});
