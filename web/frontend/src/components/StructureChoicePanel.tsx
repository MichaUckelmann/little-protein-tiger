/**
 * StructureChoicePanel — shown after the structure stage when the run is paused
 * at "structure_choice". Summarises tractability and lets the user choose the
 * next step before the pipeline continues.
 */
import { useState } from "react";
import { api } from "../lib/api";

interface Props {
  runId: number;
  tractability: string | null;
  modality: string | null;
  bsaA2: string | null;
  targetComplex: string | null;
  onResumed: () => void;
}

export function StructureChoicePanel({
  runId,
  tractability,
  modality,
  bsaA2,
  targetComplex,
  onResumed,
}: Props) {
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function choose(next_step: string) {
    setLoading(next_step);
    setError(null);
    try {
      await api.runs.resume(runId, { next_step });
      onResumed();
    } catch (e: any) {
      setError(e.message ?? "Failed to resume run");
      setLoading(null);
    }
  }

  const tractabilityColor: Record<string, string> = {
    Excellent: "#166534",
    Good: "#1d4ed8",
    Marginal: "#92400e",
    Poor: "#991b1b",
  };
  const color = tractabilityColor[tractability ?? ""] ?? "#374151";

  return (
    <div style={{
      margin: "16px 0",
      padding: "20px 24px",
      background: "#f0f9ff",
      border: "1px solid #38bdf8",
      borderRadius: "8px",
    }}>
      <div style={{ marginBottom: "16px" }}>
        <h3 style={{ margin: "0 0 4px", fontSize: "15px", fontWeight: 600, color: "#0369a1" }}>
          Structure analysis complete — choose next step
        </h3>
        {targetComplex && (
          <p style={{ margin: "0 0 12px", fontSize: "13px", color: "#0c4a6e" }}>
            Target: <strong>{targetComplex}</strong>
          </p>
        )}
        <div style={{ display: "flex", gap: "20px", flexWrap: "wrap" }}>
          {tractability && (
            <div>
              <div style={{ fontSize: "11px", color: "#64748b", marginBottom: "2px" }}>Tractability</div>
              <div style={{ fontSize: "14px", fontWeight: 600, color }}>{tractability}</div>
            </div>
          )}
          {modality && (
            <div>
              <div style={{ fontSize: "11px", color: "#64748b", marginBottom: "2px" }}>Design modality</div>
              <div style={{ fontSize: "14px", fontWeight: 600, color: "#1e3a5f" }}>
                {modality.replace(/_/g, " ")}
              </div>
            </div>
          )}
          {bsaA2 && (
            <div>
              <div style={{ fontSize: "11px", color: "#64748b", marginBottom: "2px" }}>Interface BSA</div>
              <div style={{ fontSize: "14px", fontWeight: 600, color: "#1e3a5f" }}>{bsaA2} Å²</div>
            </div>
          )}
        </div>
      </div>

      {error && (
        <div style={{ marginBottom: "12px", fontSize: "13px", color: "#dc2626" }}>{error}</div>
      )}

      <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
        <button
          onClick={() => choose("literature_and_design")}
          disabled={loading !== null}
          style={btnStyle("#1d4ed8", loading !== null)}
        >
          {loading === "literature_and_design" ? "Starting…" : "Run full pipeline →"}
        </button>
        <button
          onClick={() => choose("design_only")}
          disabled={loading !== null}
          style={btnStyle("#0369a1", loading !== null)}
        >
          {loading === "design_only" ? "Starting…" : "Skip literature → Design only"}
        </button>
        <button
          onClick={() => choose("stop")}
          disabled={loading !== null}
          style={btnStyle("#6b7280", loading !== null)}
        >
          {loading === "stop" ? "Stopping…" : "Stop here"}
        </button>
      </div>

      <p style={{ margin: "10px 0 0", fontSize: "12px", color: "#64748b" }}>
        <strong>Run full pipeline</strong> — literature search + design inputs.&ensp;
        <strong>Skip literature</strong> — go straight to BoltzGen / RFD3 design files.&ensp;
        <strong>Stop here</strong> — keep the structure report, no further stages.
      </p>
    </div>
  );
}

function btnStyle(bg: string, disabled: boolean): React.CSSProperties {
  return {
    padding: "8px 16px",
    borderRadius: "6px",
    border: "none",
    background: disabled ? "#d1d5db" : bg,
    color: disabled ? "#9ca3af" : "#fff",
    fontSize: "13px",
    fontWeight: 600,
    cursor: disabled ? "not-allowed" : "pointer",
    whiteSpace: "nowrap",
  };
}
