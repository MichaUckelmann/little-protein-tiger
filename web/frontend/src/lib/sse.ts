/**
 * SSE hook — streams run status updates from GET /runs/{id}/status.
 * Closes automatically on terminal status or component unmount.
 */
import { useEffect, useState } from "react";
import { getToken } from "./api";

export interface RunStatus {
  status: string;
  stage_current: string | null;
  pdb_id: string | null;
  target_complex: string | null;
  go_recommendation: string | null;
  error: string | null;
  completed_at: string | null;
}

const TERMINAL = new Set(["COMPLETE", "FAILED", "BLOCKED"]);
const BASE = import.meta.env.VITE_API_URL ?? "";

export function useRunStatus(runId: number | null, initialStatus?: string) {
  const [status, setStatus] = useState<RunStatus | null>(null);
  const done = status ? TERMINAL.has(status.status) : TERMINAL.has(initialStatus ?? "");

  useEffect(() => {
    if (!runId || done) return;

    // SSE doesn't support custom headers, so pass token as query param.
    // Backend reads it via a dedicated SSE token query param (added in auth.py).
    // For dev simplicity we poll instead if EventSource auth is needed.
    // Current approach: use fetch + ReadableStream (supports headers).
    const token = getToken();
    let cancelled = false;
    const controller = new AbortController();

    (async () => {
      try {
        const res = await fetch(`${BASE}/runs/${runId}/status`, {
          headers: { Authorization: `Bearer ${token}` },
          signal: controller.signal,
        });
        if (!res.body) return;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (!cancelled) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const parts = buf.split("\n\n");
          buf = parts.pop() ?? "";
          for (const part of parts) {
            const dataLine = part.split("\n").find((l) => l.startsWith("data:"));
            if (!dataLine) continue;
            try {
              const parsed: RunStatus = JSON.parse(dataLine.slice(5).trim());
              if (!cancelled) setStatus(parsed);
              if (TERMINAL.has(parsed.status)) return;
            } catch {}
          }
        }
      } catch (e) {
        if (!cancelled) console.error("SSE error", e);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [runId, done]);

  return status;
}
