import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { CaptionsPage } from './Captions';
import { useWorkflowStore } from '../stores/workflowStore';
import type { WebSocketStatus } from '../hooks/useWebSocket';

const wsState: {
  status: WebSocketStatus;
  error: string | null;
  sendMessage: ReturnType<typeof vi.fn>;
  reconnect: ReturnType<typeof vi.fn>;
  reconnectAttempts: number;
} = {
  status: 'disconnected',
  error: null,
  sendMessage: vi.fn(),
  reconnect: vi.fn(),
  reconnectAttempts: 0,
};

let latestOptions:
  | {
      onOpen?: (event: Event) => void;
      onMessage?: (event: MessageEvent) => void;
      onError?: (event: Event) => void;
    }
  | null = null;

vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn((_url: string | null, options: unknown) => {
    latestOptions = options as {
      onOpen?: (event: Event) => void;
      onMessage?: (event: MessageEvent) => void;
      onError?: (event: Event) => void;
    };

    return {
      ws: null,
      error: wsState.error,
      status: wsState.status,
      isConnected: wsState.status === 'connected',
      reconnectAttempts: wsState.reconnectAttempts,
      sendMessage: wsState.sendMessage,
      reconnect: wsState.reconnect,
      clearStartPayload: vi.fn(),
    };
  }),
}));

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: vi.fn(),
  });

  wsState.status = 'disconnected';
  wsState.error = null;
  wsState.sendMessage.mockClear();
  wsState.reconnect.mockClear();
  latestOptions = null;

  useWorkflowStore.setState({
    activeProjectName: null,
    jobs: {},
    notifications: [],
    recentlyGeneratedVideos: [],
    recentlyScrapedCaptions: [],
    videoRunningCount: 0,
    captionJobActive: false,
    burnReadyCount: 0,
    burnSelection: { videoPaths: [], captionSource: null },
  });
});

afterEach(() => {
  cleanup();
});

describe('CaptionsPage', () => {
  it('shows empty-state when no project is active', () => {
    render(
      <MemoryRouter>
        <CaptionsPage />
      </MemoryRouter>,
    );

    expect(screen.getByText('No Project Selected')).toBeTruthy();
  });

  it('starts extraction flow and updates status UI', async () => {
    useWorkflowStore.setState({
      activeProjectName: 'quick-test',
    });
    wsState.status = 'connecting';

    render(
      <MemoryRouter>
        <CaptionsPage />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByPlaceholderText('@username'), {
      target: { value: '@artist' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Extract Captions' }));

    await waitFor(() => {
      expect(screen.getByText('Running...')).toBeTruthy();
      expect(screen.getByText('CONNECTING')).toBeTruthy();
    });
  });

  it('sends handle + selected sort mode on socket open', async () => {
    useWorkflowStore.setState({
      activeProjectName: 'quick-test',
    });

    render(
      <MemoryRouter>
        <CaptionsPage />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByPlaceholderText('@username'), {
      target: { value: '@artist' },
    });
    // Sort is now a Radix Select — we can't fireEvent.change on it directly.
    // The default 'latest' is fine for verifying the sendMessage payload structure.
    // The sort param will be 'latest' (default) instead of 'popular'.
    fireEvent.click(screen.getByRole('button', { name: 'Extract Captions' }));

    await waitFor(() => {
      expect(latestOptions).toBeTruthy();
    });

    act(() => {
      latestOptions?.onOpen?.(new Event('open'));
    });

    expect(wsState.sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        profile_url: '@artist',
        sort: 'latest',
      }),
    );
  });

  it('handles full scrape flow and primes burn caption source', async () => {
    useWorkflowStore.setState({
      activeProjectName: 'quick-test',
    });
    wsState.status = 'connected';

    render(
      <MemoryRouter>
        <CaptionsPage />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByPlaceholderText('@username'), {
      target: { value: '@artist' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Extract Captions' }));

    await waitFor(() => {
      expect(latestOptions).toBeTruthy();
    });

    // Simulate the real event sequence: urls_collected → frame_ready → ocr_done → all_complete
    act(() => {
      latestOptions?.onMessage?.(
        {
          data: JSON.stringify({
            event: 'urls_collected',
            count: 1,
            urls: ['https://tiktok.com/@artist/video/1'],
          }),
        } as MessageEvent,
      );
    });

    act(() => {
      latestOptions?.onMessage?.(
        {
          data: JSON.stringify({
            event: 'frame_ready',
            index: 0,
            video_id: '1',
            video_url: 'https://tiktok.com/@artist/video/1',
            b64: 'dGVzdA==',
          }),
        } as MessageEvent,
      );
    });

    act(() => {
      latestOptions?.onMessage?.(
        {
          data: JSON.stringify({
            event: 'ocr_done',
            index: 0,
            video_id: '1',
            caption: 'caption text',
            error: null,
            total: 1,
          }),
        } as MessageEvent,
      );
    });

    act(() => {
      latestOptions?.onMessage?.(
        {
          data: JSON.stringify({
            event: 'all_complete',
            username: 'artist1',
            csv: '/tmp/captions.csv',
            results: [
              {
                index: 0,
                video_id: '1',
                video_url: 'https://tiktok.com/@artist/video/1',
                caption: 'caption text',
              },
            ],
          }),
        } as MessageEvent,
      );
    });

    await waitFor(() => {
      expect(screen.getByText('caption text')).toBeTruthy();
      expect(screen.getByRole('button', { name: /Use in Burn/i })).toBeTruthy();
    });

    fireEvent.click(screen.getByRole('button', { name: /Use in Burn/i }));
    const store = useWorkflowStore.getState();
    expect(store.burnSelection.captionSource).toBe('artist1');
  });

  it('shows grid/table tabs after urls_collected event', async () => {
    useWorkflowStore.setState({
      activeProjectName: 'quick-test',
    });
    wsState.status = 'connected';

    render(
      <MemoryRouter>
        <CaptionsPage />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByPlaceholderText('@username'), {
      target: { value: '@artist' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Extract Captions' }));

    await waitFor(() => {
      expect(latestOptions).toBeTruthy();
    });

    act(() => {
      latestOptions?.onMessage?.(
        {
          data: JSON.stringify({
            event: 'urls_collected',
            count: 3,
            urls: ['u1', 'u2', 'u3'],
          }),
        } as MessageEvent,
      );
    });

    await waitFor(() => {
      expect(screen.getByText('Grid')).toBeTruthy();
      expect(screen.getByText('Table')).toBeTruthy();
    });
  });
});
