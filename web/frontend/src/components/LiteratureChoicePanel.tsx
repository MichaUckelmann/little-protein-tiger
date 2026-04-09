/**
 * LiteratureChoicePanel — shown after the literature/molecular-biology-expert stage.
 * Lets the user review the go/no-go recommendation and decide whether to proceed
 * to the design stage or stop here.
 */
import { useState } from "react";
import { api } from "../lib/api";

interface Props {
  runId: number;
  goRecommendation: string | null;
  onResumed: () => void;
}

const GO_COLOR: Record<string, string> = {
  GO: "#16a34a",
  CONDITIONAL_GO: "#d97706",
  NO_GO: "#dc2626",
};

const GO_BG: Record<string, string> = {
  GO: "#f0fdf4",
  CONDITIONAL_GO: "#fffbeb",
  NO_GO: "#fef2f2",
};

const GO_BORDER: Record<string, string> = {
  GO: "#bbf7d0",
  CONDITIONAL_GO: "#fcd34d",
  NO_GO: "#fca5a5",
};

export function LiteratureChoicePanel({ runId, goRecommendation, onResumed }: Props) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const go = goRecommendation?.toUpperCase().replace("-", "_") ?? "";
  const color = GO_COLOR[go] ?? "#374151";
  const bg = GO_BG[go] ?? "#f9fafb";
  const border = GO_BORDER[go] ?? "#e5e7eb";

  async function choose(nextStep: "proceed_to_design" | "stop") {
    setSubmitting(true);
    setError(null);
    try {
      await api.runs.resume(runId, { next_step: nextStep });
      onResumed();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div style={{
      border: "1px solid #38bdf8",
      borderLeft: "4px solid #0369a1",
      borderRadius: 10,
      background: "#f0f9ff",
      padding: "20px 24px",
      marginBottom: 14,
    }}>
      <div style={{ fontWeight: 700, fontSize: 15, color: "#0369a1", marginBottom: 12 }}>
        Literature Review Complete — Ready for Design Stage
      </div>

      {go && (
        <div style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          padding: "8px 16px",
          borderRadius: 8,
          border: `1px solid ${border}`,
          background: bg,
          marginBottom: 16,
        }}>
          <span style={{ fontSize: 13, color: "#374151" }}>Go/No-Go decision:</span>
          <span style={{ fontWeight: 700, fontSize: 14, color }}>
            {go.replace("_", " ")}
          </span>
        </div>
      )}

      <p style={{ fontSize: 13, color: "#374151", margin: "0 0 16px" }}>
        The literature review is complete. Proceed to the protein design stage
        (generates BoltzGen YAML and RFD3 JSON inputs), or stop here to review the results.
      </p>

      <div style={{ display: "flex", gap: 10 }}>
        <button
          onClick={() => choose("proceed_to_design")}
          disabled={submitting}
          style={{
            padding: "9px 18px",
            background: "#18181b",
            color: "#fff",
            border: "none",
            borderRadius: 6,
            cursor: submitting ? "not-allowed" : "pointer",
            fontWeight: 600,
            fontSize: 14,
            opacity: submitting ? 0.7 : 1,
          }}
        >
          {submitting ? "Submitting…" : "Proceed to Design"}
        </button>

        <button
          onClick={() => choose("stop")}
          disabled={submitting}
          style={{
            padding: "9px 18px",
            background: "#f3f4f6",
            color: "#374151",
            border: "1px solid #d1d5db",
            borderRadius: 6,
            cursor: submitting ? "not-allowed" : "pointer",
            fontSize: 14,
            opacity: submitting ? 0.7 : 1,
          }}
        >
          Stop here
        </button>
      </div>

      {error && (
        <p style={{ color: "#dc2626", fontSize: 13, marginTop: 10 }}>{error}</p>
      )}
    </div>
  );
}
