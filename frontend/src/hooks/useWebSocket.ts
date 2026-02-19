import { useCallback, useEffect, useRef, useState } from 'react';

export type WebSocketStatus = 'connecting' | 'connected' | 'reconnecting' | 'disconnected' | 'error';

interface WebSocketOptions {
  onMessage?: (event: MessageEvent) => void;
  onOpen?: (event: Event) => void;
  onClose?: (event: CloseEvent) => void;
  onError?: (event: Event) => void;
  maxReconnectAttempts?: number;
  maxReconnectDelayMs?: number;
  shouldReconnect?: (event: CloseEvent) => boolean;
}

export function useWebSocket(url: string | null, options: WebSocketOptions = {}) {
  const {
    onMessage,
    onOpen,
    onClose,
    onError,
    maxReconnectAttempts = 10,
    maxReconnectDelayMs = 30000,
    shouldReconnect,
  } = options;

  const [status, setStatus] = useState<WebSocketStatus>('disconnected');
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isDisposedRef = useRef(false);
  const queuedMessagesRef = useRef<string[]>([]);
  const lastStartPayloadRef = useRef<string | null>(null);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const connect = useCallback(
    (mode: 'connecting' | 'reconnecting' = 'connecting') => {
      if (!url || isDisposedRef.current) {
        return;
      }

      clearReconnectTimer();
      setStatus(mode);

      try {
        const socket = new WebSocket(url);
        wsRef.current = socket;

        socket.onopen = (event) => {
          if (isDisposedRef.current) {
            socket.close();
            return;
          }

          setStatus('connected');
          setError(null);

          const hadReconnects = reconnectAttemptsRef.current > 0;
          reconnectAttemptsRef.current = 0;

          if (queuedMessagesRef.current.length > 0) {
            for (const payload of queuedMessagesRef.current) {
              socket.send(payload);
            }
            queuedMessagesRef.current = [];
          }

          if (hadReconnects && lastStartPayloadRef.current) {
            socket.send(lastStartPayloadRef.current);
          }

          onOpen?.(event);
        };

        socket.onmessage = (event) => {
          onMessage?.(event);
        };

        socket.onerror = (event) => {
          setError('WebSocket connection error');
          setStatus('error');
          onError?.(event);
        };

        socket.onclose = (event) => {
          wsRef.current = null;
          onClose?.(event);

          if (isDisposedRef.current) {
            setStatus('disconnected');
            return;
          }

          const reconnectAllowed = shouldReconnect ? shouldReconnect(event) : true;
          if (!reconnectAllowed) {
            setStatus('disconnected');
            return;
          }

          if (reconnectAttemptsRef.current >= maxReconnectAttempts) {
            setStatus('error');
            setError('Connection lost. Reconnect attempts exhausted.');
            return;
          }

          const nextAttempt = reconnectAttemptsRef.current + 1;
          reconnectAttemptsRef.current = nextAttempt;
          setStatus('reconnecting');

          const baseDelay = Math.min(1000 * 2 ** (nextAttempt - 1), maxReconnectDelayMs);
          const reconnectDelay = document.hidden ? Math.max(baseDelay, 3000) : baseDelay;

          reconnectTimerRef.current = setTimeout(() => {
            connect('reconnecting');
          }, reconnectDelay);
        };
      } catch {
        setStatus('error');
        setError('Failed to initialize WebSocket');
      }
    },
    [clearReconnectTimer, maxReconnectAttempts, maxReconnectDelayMs, onClose, onError, onMessage, onOpen, shouldReconnect, url],
  );

  useEffect(() => {
    isDisposedRef.current = false;
    reconnectAttemptsRef.current = 0;
    queuedMessagesRef.current = [];

    if (!url) {
      setStatus('disconnected');
      return () => {
        isDisposedRef.current = true;
      };
    }

    connect('connecting');

    return () => {
      isDisposedRef.current = true;
      clearReconnectTimer();
      if (wsRef.current) {
        wsRef.current.close(1000, 'cleanup');
      }
      wsRef.current = null;
      setStatus('disconnected');
    };
  }, [clearReconnectTimer, connect, url]);

  const sendMessage = useCallback((data: unknown) => {
    const payload = JSON.stringify(data);

    if (
      typeof data === 'object' &&
      data !== null &&
      'action' in data &&
      typeof (data as { action?: unknown }).action === 'string' &&
      (data as { action: string }).action === 'start'
    ) {
      lastStartPayloadRef.current = payload;
    }

    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(payload);
      return;
    }

    queuedMessagesRef.current.push(payload);
  }, []);

  const reconnect = useCallback(() => {
    clearReconnectTimer();
    reconnectAttemptsRef.current = 0;
    if (wsRef.current) {
      wsRef.current.close();
    }
    connect('reconnecting');
  }, [clearReconnectTimer, connect]);

  return {
    ws: wsRef.current,
    error,
    status,
    isConnected: status === 'connected',
    reconnectAttempts: reconnectAttemptsRef.current,
    sendMessage,
    reconnect,
  };
}
