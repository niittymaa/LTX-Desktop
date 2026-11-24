import { useCallback, useEffect, useState } from "react";

import { ApiClient, type ApiSuccessOf } from "../lib/api-client";

export type McpStatus = ApiSuccessOf<"getMcpStatus">;
export type McpClientConfig = ApiSuccessOf<"getMcpClientConfig">;

const MCP_POLL_ACTIVE_MS = 1000;
const MCP_POLL_SETTLED_MS = 5000;

export function useMcpStatus(enabled: boolean): {
  status: McpStatus | null;
  clientConfig: McpClientConfig | null;
  rotating: boolean;
  error: string | null;
  rotateToken: () => Promise<void>;
  refreshConfig: () => Promise<void>;
} {
  const [status, setStatus] = useState<McpStatus | null>(null);
  const [clientConfig, setClientConfig] = useState<McpClientConfig | null>(null);
  const [rotating, setRotating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshConfig = useCallback(async () => {
    const result = await ApiClient.getMcpClientConfig();
    if (result.ok) {
      setClientConfig(result.data as McpClientConfig);
      setError(null);
    } else {
      setError(result.error.message || "Failed to load MCP client config");
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const result = await ApiClient.getMcpStatus();
        if (cancelled) return;
        if (result.ok) {
          const next = result.data as McpStatus;
          setStatus(next);
          setError(null);
          const delay = enabled && !next.serving ? MCP_POLL_ACTIVE_MS : MCP_POLL_SETTLED_MS;
          timer = window.setTimeout(() => {
            void poll();
          }, delay);
          return;
        }
      } catch {
        if (cancelled) return;
      }
      timer = window.setTimeout(() => {
        void poll();
      }, MCP_POLL_SETTLED_MS);
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [enabled]);

  useEffect(() => {
    if (!enabled) {
      setClientConfig(null);
      return;
    }
    void refreshConfig();
  }, [enabled, status?.serving, refreshConfig]);

  const rotateToken = useCallback(async () => {
    setRotating(true);
    setError(null);
    try {
      const result = await ApiClient.rotateMcpToken();
      if (!result.ok) {
        setError(result.error.message || "Failed to rotate MCP token");
        return;
      }
      await refreshConfig();
    } finally {
      setRotating(false);
    }
  }, [refreshConfig]);

  return { status, clientConfig, rotating, error, rotateToken, refreshConfig };
}
