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
    activeProject: null,
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

  it('starts scraping flow and updates status UI', async () => {
    useWorkflowStore.setState({
      activeProject: {
        name: 'quick-test',
        path: '/tmp/projects/quick-test',
        video_count: 0,
        caption_count: 0,
        burned_count: 0,
      },
    });
    wsState.status = 'connecting';

    render(
      <MemoryRouter>
        <CaptionsPage />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByPlaceholderText('https://www.tiktok.com/@username'), {
      target: { value: 'https://www.tiktok.com/@artist' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Start Scraping' }));

    await waitFor(() => {
      expect(screen.getByText('Scraping...')).toBeTruthy();
      expect(screen.getByText('CONNECTING')).toBeTruthy();
    });
  });

  it('handles all_complete message and primes burn caption source', async () => {
    useWorkflowStore.setState({
      activeProject: {
        name: 'quick-test',
        path: '/tmp/projects/quick-test',
        video_count: 0,
        caption_count: 0,
        burned_count: 0,
      },
    });
    wsState.status = 'connected';

    render(
      <MemoryRouter>
        <CaptionsPage />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByPlaceholderText('https://www.tiktok.com/@username'), {
      target: { value: 'https://www.tiktok.com/@artist' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Start Scraping' }));

    await waitFor(() => {
      expect(latestOptions).toBeTruthy();
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
                video_url: 'u',
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
});
