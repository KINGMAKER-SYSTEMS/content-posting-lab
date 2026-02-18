import { useState, useEffect, useRef, useCallback } from 'react';

interface WebSocketOptions {
  onMessage?: (event: MessageEvent) => void;
  onOpen?: (event: Event) => void;
  onClose?: (event: CloseEvent) => void;
  onError?: (event: Event) => void;
  reconnectInterval?: number;
  maxReconnectAttempts?: number;
}

export function useWebSocket(url: string | null, options: WebSocketOptions = {}) {
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<Event | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectCountRef = useRef(0);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | undefined>(undefined);

  const {
    onMessage,
    onOpen,
    onClose,
    onError,
    reconnectInterval = 3000,
    maxReconnectAttempts = 5
  } = options || {};

  const connect = useCallback(() => {
    if (!url) return;

    try {
      const ws = new WebSocket(url);

      ws.onopen = (event) => {
        setIsConnected(true);
        setError(null);
        reconnectCountRef.current = 0;
        onOpen?.(event);
      };

      ws.onmessage = (event) => {
        onMessage?.(event);
      };

      ws.onclose = (event) => {
        setIsConnected(false);
        wsRef.current = null;
        onClose?.(event);

        // Attempt reconnect if not closed cleanly and haven't exceeded max attempts
        if (!event.wasClean && reconnectCountRef.current < maxReconnectAttempts) {
          reconnectTimeoutRef.current = setTimeout(() => {
            reconnectCountRef.current += 1;
            connect();
          }, reconnectInterval);
        }
      };

      ws.onerror = (event) => {
        setError(event);
        onError?.(event);
      };

      wsRef.current = ws;
    } catch (err) {
      console.error('WebSocket connection failed:', err);
    }
  }, [url, onMessage, onOpen, onClose, onError, reconnectInterval, maxReconnectAttempts]);

  useEffect(() => {
    if (url) {
      connect();
    }

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [url, connect]);

  const sendMessage = useCallback((data: any) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    } else {
      console.warn('WebSocket is not connected');
    }
  }, []);

  return {
    isConnected,
    error,
    sendMessage,
    ws: wsRef.current
  };
}
