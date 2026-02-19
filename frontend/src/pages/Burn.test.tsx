import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { BurnPage } from './Burn';
import { useWorkflowStore } from '../stores/workflowStore';

function jsonResponse(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
    text: async () => JSON.stringify(data),
  } as Response;
}

const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  const method = init?.method || 'GET';

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

  if (url.includes('/api/burn/overlay') && method === 'POST') {
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
    activeProject: null,
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

  it('loads project videos and caption sources', async () => {
    useWorkflowStore.setState({
      activeProject: {
        name: 'quick-test',
        path: '/tmp/projects/quick-test',
        video_count: 1,
        caption_count: 1,
        burned_count: 0,
      },
    });

    render(<BurnPage />);

    await waitFor(() => {
      expect(screen.getByText('Burn Captions')).toBeTruthy();
      expect(screen.getByText(/artist1/)).toBeTruthy();
      expect(screen.getAllByText('caption text').length).toBeGreaterThan(0);
    });
  });

  it('submits burn overlay request and shows success result', async () => {
    useWorkflowStore.setState({
      activeProject: {
        name: 'quick-test',
        path: '/tmp/projects/quick-test',
        video_count: 1,
        caption_count: 1,
        burned_count: 0,
      },
    });

    render(<BurnPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Burn Caption' })).toBeTruthy();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Burn Caption' }));

    await waitFor(() => {
      const overlayCall = fetchMock.mock.calls.find(
        ([url, init]) => String(url).includes('/api/burn/overlay') && (init?.method || 'GET') === 'POST',
      );
      expect(overlayCall).toBeTruthy();
      expect(screen.getByText('Success')).toBeTruthy();
    });
  });
});
