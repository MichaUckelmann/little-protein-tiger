/**
 * Collapsible panel for a single pipeline stage.
 * Shows status icon + key fields collapsed; full markdown report expanded.
 */
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Run } from "../lib/api";

const STAGE_ORDER = ["pathway", "structure", "literature", "design"] as const;
const STAGE_LABELS: Record<string, string> = {
  pathway: "Pathway Expert",
  structure: "Structure Analysis",
  literature: "Literature Review",
  design: "Design Inputs",
};
const STAGE_FILES: Record<string, string> = {
  pathway: "00_pathway.md",
  structure: "01_structure.md",
  literature: "02_literature.md",
  design: "03_design_report.md",
};

type StageStatus = "complete" | "running" | "pending" | "failed";

function stageStatus(
  stage: string,
  run: Run,
  stageFiles: Record<string, string>
): StageStatus {
  const file = STAGE_FILES[stage];
  if (stageFiles[file]) return "complete";
  if (run.stage_current === stage) return "running";
  if (run.status === "FAILED" && !stageFiles[file]) return "failed";
  return "pending";
}

function StatusIcon({ status }: { status: StageStatus }) {
  if (status === "complete")
    return <span style={{ color: "#22c55e", fontWeight: "bold" }}>✓</span>;
  if (status === "running")
    return <span style={{ color: "#3b82f6" }}>⟳</span>;
  if (status === "failed")
    return <span style={{ color: "#ef4444" }}>✗</span>;
  return <span style={{ color: "#9ca3af" }}>○</span>;
}

interface Props {
  stage: string;
  run: Run;
  stageFiles: Record<string, string>;
}

export function StagePanel({ stage, run, stageFiles }: Props) {
  const [open, setOpen] = useState(false);
  const status = stageStatus(stage, run, stageFiles);
  const content = stageFiles[STAGE_FILES[stage]];

  return (
    <div style={{
      border: "1px solid #e5e7eb",
      borderRadius: 8,
      marginBottom: 12,
      overflow: "hidden",
    }}>
      <button
        onClick={() => content && setOpen((o) => !o)}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "12px 16px",
          background: status === "running" ? "#eff6ff" : "#f9fafb",
          border: "none",
          cursor: content ? "pointer" : "default",
          textAlign: "left",
        }}
      >
        <StatusIcon status={status} />
        <span style={{ fontWeight: 600, flex: 1 }}>{STAGE_LABELS[stage]}</span>
        {status === "running" && (
          <span style={{ fontSize: 12, color: "#3b82f6" }}>Running…</span>
        )}
        {content && (
          <span style={{ fontSize: 12, color: "#6b7280" }}>{open ? "▲ hide" : "▼ show"}</span>
        )}
      </button>

      {open && content && (
        <div style={{
          padding: "16px 20px",
          background: "#fff",
          borderTop: "1px solid #e5e7eb",
          maxHeight: 600,
          overflowY: "auto",
          fontSize: 14,
          lineHeight: 1.6,
        }}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}

export function StageStepper({
  run,
  stageFiles,
}: {
  run: Run;
  stageFiles: Record<string, string>;
}) {
  return (
    <div>
      {STAGE_ORDER.map((stage) => (
        <StagePanel key={stage} stage={stage} run={run} stageFiles={stageFiles} />
      ))}
    </div>
  );
}
