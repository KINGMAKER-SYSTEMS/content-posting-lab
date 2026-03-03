import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { BurnPage } from './Burn';
import { useWorkflowStore } from '../stores/workflowStore';

// Mock html2canvas
vi.mock('html2canvas', () => ({
  default: vi.fn(async () => {
    const canvas = document.createElement('canvas');
    canvas.width = 100;
    canvas.height = 100;
    return canvas;
  }),
}));

function jsonResponse(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
    text: async () => JSON.stringify(data),
  } as Response;
}

const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
  const url = String(input);

  if (url.includes('/api/burn/videos')) {
    return jsonResponse({
      videos: [
        {
          path: 'provider/clip.mp4',
          name: 'clip.mp4',
          folder: 'provider',
        },
      ],
    });
  }

  if (url.includes('/api/burn/captions')) {
    return jsonResponse({
      sources: [
        {
          username: 'artist1',
          csv_path: 'projects/quick-test/captions/artist1/captions.csv',
          count: 1,
          captions: [
            {
              text: 'caption text',
              video_id: '1',
              video_url: 'u',
            },
          ],
        },
      ],
    });
  }

  if (url.includes('/api/burn/fonts')) {
    return jsonResponse({
      fonts: [
        { file: 'TikTokSans16pt-Bold.ttf', name: 'TikTok Sans Bold' },
      ],
    });
  }

  if (url.includes('/api/burn/batches')) {
    return jsonResponse({ batches: [] });
  }

  if (url.includes('/api/burn/overlay')) {
    return jsonResponse({ index: 0, ok: true, file: 'batch-1/burned_000.mp4' });
  }

  return jsonResponse({});
});

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockClear();
  Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
    configurable: true,
    value: vi.fn(() => ({
      clearRect: vi.fn(),
      fillRect: vi.fn(),
      fillText: vi.fn(),
      strokeText: vi.fn(),
      measureText: vi.fn(() => ({ width: 120 })),
    })),
  });
  Object.defineProperty(HTMLCanvasElement.prototype, 'toDataURL', {
    configurable: true,
    value: vi.fn(() => 'data:image/png;base64,AAA='),
  });

  useWorkflowStore.setState({
    activeProjectName: null,
    jobs: {},
    notifications: [],
    recentlyGeneratedVideos: [],
    recentlyScrapedCaptions: [],
    videoRunningCount: 0,
    captionJobActive: false,
    burnReadyCount: 0,
    burnSelection: { videoPaths: ['provider/clip.mp4'], captionSource: 'artist1' },
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('BurnPage', () => {
  it('shows empty-state when no project is selected', () => {
    render(<BurnPage />);
    expect(screen.getByText('No Project Selected')).toBeTruthy();
  });

  it('loads project data and shows sidebar controls', async () => {
    useWorkflowStore.setState({
      activeProjectName: 'quick-test',
    });

    render(<BurnPage />);

    await waitFor(() => {
      expect(screen.getByText('Caption Burner')).toBeTruthy();
      // Caption sources are in a Radix Select (options not in DOM when closed).
      // Verify data loaded by checking the captions fetch was made.
      const urls = fetchMock.mock.calls.map(([u]) => String(u));
      expect(urls.some((u) => u.includes('/api/burn/captions'))).toBe(true);
    });
  });

  it('fetches videos, captions, fonts, and batches on mount', async () => {
    useWorkflowStore.setState({
      activeProjectName: 'quick-test',
    });

    render(<BurnPage />);

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([u]) => String(u));
      expect(urls.some((u) => u.includes('/api/burn/videos'))).toBe(true);
      expect(urls.some((u) => u.includes('/api/burn/captions'))).toBe(true);
      expect(urls.some((u) => u.includes('/api/burn/fonts'))).toBe(true);
      expect(urls.some((u) => u.includes('/api/burn/batches'))).toBe(true);
    });
  });

  it('shows color correction sliders', async () => {
    useWorkflowStore.setState({
      activeProjectName: 'quick-test',
    });

    render(<BurnPage />);

    await waitFor(() => {
      expect(screen.getByText('Brightness')).toBeTruthy();
      expect(screen.getByText('Contrast')).toBeTruthy();
      expect(screen.getByText('Saturation')).toBeTruthy();
      expect(screen.getByText('Temperature')).toBeTruthy();
    });
  });
});
