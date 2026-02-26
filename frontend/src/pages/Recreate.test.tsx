import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { RecreatePage } from './Recreate';
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
    recreateJobActive: false,
  });

  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ jobs: [] }),
  });

  // Mock crypto.randomUUID used by handleStart
  vi.stubGlobal('crypto', {
    ...globalThis.crypto,
    randomUUID: () => '00000000-0000-0000-0000-000000000000',
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('RecreatePage', () => {
  it('shows empty state when no project is active', () => {
    render(
      <MemoryRouter>
        <RecreatePage />
      </MemoryRouter>,
    );

    expect(screen.getByText('No Project Selected')).toBeTruthy();
  });

  it('renders input and button when project is active', () => {
    useWorkflowStore.setState({ activeProjectName: 'quick-test' });
    render(
      <MemoryRouter>
        <RecreatePage />
      </MemoryRouter>,
    );

    expect(screen.getByPlaceholderText('https://www.tiktok.com/@user/video/...')).toBeTruthy();
    expect(screen.getByRole('button', { name: /Extract & Clean/i })).toBeTruthy();
  });

  it('disables button when URL is empty', () => {
    useWorkflowStore.setState({ activeProjectName: 'quick-test' });
    render(
      <MemoryRouter>
        <RecreatePage />
      </MemoryRouter>,
    );

    const btn = screen.getByRole('button', { name: /Extract & Clean/i });
    expect(btn).toHaveProperty('disabled', true);
  });

  it('shows frame previews after frames_ready event', async () => {
    useWorkflowStore.setState({ activeProjectName: 'quick-test' });
    wsState.status = 'connected';

    render(
      <MemoryRouter>
        <RecreatePage />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByPlaceholderText('https://www.tiktok.com/@user/video/...'), {
      target: { value: 'https://www.tiktok.com/@artist/video/123' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Extract & Clean/i }));

    await waitFor(() => {
      expect(latestOptions).toBeTruthy();
    });

    // Send downloaded event first to set duration
    act(() => {
      latestOptions?.onMessage?.({
        data: JSON.stringify({
          event: 'downloaded',
          text: 'Video downloaded',
          duration: 15.2,
        }),
      } as MessageEvent);
    });

    // Send frames_ready event
    act(() => {
      latestOptions?.onMessage?.({
        data: JSON.stringify({
          event: 'frames_ready',
          text: 'Frames extracted',
          first_frame: 'data:image/png;base64,dGVzdA==',
          last_frame: 'data:image/png;base64,dGVzdA==',
        }),
      } as MessageEvent);
    });

    await waitFor(() => {
      // Duration displayed as "15.2s"
      expect(screen.getByText('15.2s')).toBeTruthy();
      // Frame images rendered with proper alt text
      expect(screen.getByAltText('First frame original')).toBeTruthy();
      expect(screen.getByAltText('Last frame original')).toBeTruthy();
    });
  });
});
