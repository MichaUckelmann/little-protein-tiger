/**
 * Pipeline stage panels with live status indicators.
 * - Horizontal stepper at top showing overall progress
 * - Each stage card: colour-coded left border, auto-expands when running
 * - Animated pulse on the active stage
 */
import { useState, useEffect, lazy, Suspense, Component } from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Run, TargetChoice } from "../lib/api";
import { PathwayChoicePanel } from "./PathwayChoicePanel";
import { StructureChoicePanel } from "./StructureChoicePanel";
import { StructureNeededPanel } from "./StructureNeededPanel";
import { LiteratureChoicePanel } from "./LiteratureChoicePanel";
import { ContactTable } from "./ContactTable";

// Lazy-load the Mol* viewer so it doesn't block the initial page render.
// The ~5 MB Mol* bundle is only fetched when a structure stage result is visible.
const StructureViewer = lazy(() =>
  import("./StructureViewer").then((m) => ({ default: m.StructureViewer }))
);

// Error boundary — prevents any Mol* crash from killing the whole page.
class ViewerErrorBoundary extends Component<
  { children: ReactNode },
  { error: string | null }
> {
  state = { error: null };
  static getDerivedStateFromError(e: unknown) {
    return { error: e instanceof Error ? e.message : "Viewer error" };
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{
          height: 450, display: "flex", alignItems: "center",
          justifyContent: "center", background: "#fef2f2",
          border: "1px solid #fca5a5", borderRadius: 10,
          color: "#dc2626", fontSize: 13, padding: 16, textAlign: "center",
        }}>
          Structure viewer error: {this.state.error}
        </div>
      );
    }
    return this.props.children;
  }
}

const STAGE_ORDER = ["pathway", "structure", "literature", "design"] as const;
const STAGE_LABELS: Record<string, string> = {
  pathway:   "Pathway Expert",
  structure: "Structure Analysis",
  literature: "Literature Review",
  design:    "Design Inputs",
};
const STAGE_FILES: Record<string, string> = {
  pathway:   "00_pathway.md",
  structure: "01_structure.md",
  literature: "02_literature.md",
  design:    "03_design_report.md",
};
const STAGE_DESC: Record<string, string> = {
  pathway:   "Identifies target PPI and PDB from literature corpus",
  structure: "Maps interface hotspots from atomic coordinates",
  literature: "Assesses tractability and prior art; go/no-go decision",
  design:    "Generates BoltzGen YAML and RFD3 JSON design inputs",
};

type StageStatus = "complete" | "running" | "paused" | "pending" | "failed";

function stageStatus(
  stage: string,
  run: Run,
  stageFiles: Record<string, string>
): StageStatus {
  if (stageFiles[STAGE_FILES[stage]]) return "complete";
  if (run.stage_current === stage) {
    if (run.status === "PAUSED") return "paused";
    return "running";
  }
  if (run.status === "FAILED" && !stageFiles[STAGE_FILES[stage]]) {
    // Only mark failed if a prior stage was running (i.e. pipeline started this stage)
    const idx = STAGE_ORDER.indexOf(stage as typeof STAGE_ORDER[number]);
    const prevComplete = idx === 0 || !!stageFiles[STAGE_FILES[STAGE_ORDER[idx - 1]]];
    if (prevComplete) return "failed";
  }
  return "pending";
}

const STATUS_BORDER: Record<StageStatus, string> = {
  complete: "#22c55e",
  running:  "#3b82f6",
  paused:   "#f59e0b",
  pending:  "#e5e7eb",
  failed:   "#ef4444",
};
const STATUS_BG: Record<StageStatus, string> = {
  complete: "#f0fdf4",
  running:  "#eff6ff",
  paused:   "#fffbeb",
  pending:  "#fafafa",
  failed:   "#fef2f2",
};

function StatusDot({ status }: { status: StageStatus }) {
  const base: React.CSSProperties = {
    width: 10, height: 10, borderRadius: "50%", flexShrink: 0,
    background: STATUS_BORDER[status],
  };
  if (status === "running") {
    return (
      <span style={{ position: "relative", display: "inline-flex", alignItems: "center" }}>
        <span style={{ ...base, opacity: 0.3, position: "absolute", animation: "lpt-pulse 1.4s ease-in-out infinite" }} />
        <span style={base} />
      </span>
    );
  }
  if (status === "paused") {
    return <span style={{ ...base, background: "#f59e0b" }} />;
  }
  return <span style={base} />;
}

// ── Horizontal stepper ─────────────────────────────────────────────────────

export function StageProgressBar({
  run,
  stageFiles,
}: {
  run: Run;
  stageFiles: Record<string, string>;
}) {
  return (
    <div style={stepperStyles.wrap}>
      {STAGE_ORDER.map((stage, i) => {
        const status = stageStatus(stage, run, stageFiles);
        const isLast = i === STAGE_ORDER.length - 1;
        return (
          <div key={stage} style={stepperStyles.item}>
            {/* Circle */}
            <div style={{
              ...stepperStyles.circle,
              background: status === "pending" ? "#e5e7eb" : STATUS_BORDER[status],
              color: status === "pending" ? "#9ca3af" : "#fff",
              boxShadow: status === "running" ? `0 0 0 4px #bfdbfe` : status === "paused" ? `0 0 0 4px #fde68a` : "none",
            }}>
              {status === "complete" ? "✓" : status === "failed" ? "✗" : status === "paused" ? "⏸" : i + 1}
            </div>
            {/* Label */}
            <div style={{
              ...stepperStyles.label,
              color: status === "pending" ? "#9ca3af" : status === "running" ? "#1d4ed8" : status === "paused" ? "#92400e" : "#374151",
              fontWeight: status === "running" || status === "paused" ? 700 : 500,
            }}>
              {STAGE_LABELS[stage]}
              {status === "running" && (
                <span style={stepperStyles.runningBadge}>running</span>
              )}
              {status === "paused" && (
                <span style={{ ...stepperStyles.runningBadge, background: "#fef3c7", color: "#92400e" }}>paused</span>
              )}
            </div>
            {/* Connector line */}
            {!isLast && (
              <div style={{
                ...stepperStyles.line,
                background: stageFiles[STAGE_FILES[stage]] ? "#22c55e" : "#e5e7eb",
              }} />
            )}
          </div>
        );
      })}
    </div>
  );
}

const stepperStyles: Record<string, React.CSSProperties> = {
  wrap: {
    display: "flex", alignItems: "flex-start", justifyContent: "space-between",
    padding: "20px 24px", background: "#fff",
    border: "1px solid #e5e7eb", borderRadius: 10, marginBottom: 24,
    position: "relative",
  },
  item: {
    display: "flex", flexDirection: "column", alignItems: "center",
    flex: 1, position: "relative",
  },
  circle: {
    width: 30, height: 30, borderRadius: "50%",
    display: "flex", alignItems: "center", justifyContent: "center",
    fontSize: 12, fontWeight: 700, marginBottom: 8,
    transition: "box-shadow 0.3s",
  },
  label: {
    fontSize: 12, textAlign: "center", lineHeight: 1.3,
    display: "flex", flexDirection: "column", alignItems: "center", gap: 3,
  },
  runningBadge: {
    display: "inline-block", fontSize: 10, fontWeight: 700,
    background: "#dbeafe", color: "#1d4ed8", borderRadius: 4,
    padding: "1px 5px", textTransform: "uppercase", letterSpacing: "0.05em",
  },
  line: {
    position: "absolute", top: 15, left: "calc(50% + 20px)",
    right: "calc(-50% + 20px)", height: 2,
    transition: "background 0.4s",
  },
};

// ── Individual stage card ──────────────────────────────────────────────────

interface Props {
  stage: string;
  run: Run;
  stageFiles: Record<string, string>;
}

export function StagePanel({ stage, run, stageFiles }: Props) {
  const status = stageStatus(stage, run, stageFiles);
  const content = stageFiles[STAGE_FILES[stage]];

  // Auto-open when running; auto-open when just completed
  const [open, setOpen] = useState(status === "running");
  useEffect(() => {
    if (status === "running") setOpen(true);
    if (status === "complete" && !open) setOpen(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const canToggle = !!content;

  return (
    <div style={{
      borderRadius: 10,
      marginBottom: 14,
      overflow: "hidden",
      border: `1px solid ${STATUS_BORDER[status]}`,
      borderLeft: `4px solid ${STATUS_BORDER[status]}`,
      background: "#fff",
      transition: "border-color 0.3s",
    }}>
      {/* Header row */}
      <button
        onClick={() => canToggle && setOpen((o) => !o)}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "16px 20px",
          background: STATUS_BG[status],
          border: "none",
          cursor: canToggle ? "pointer" : "default",
          textAlign: "left",
          transition: "background 0.3s",
        }}
      >
        <StatusDot status={status} />

        <div style={{ flex: 1 }}>
          <div style={{
            fontWeight: 700, fontSize: 15,
            color: status === "running" ? "#1d4ed8" : "#111827",
          }}>
            {STAGE_LABELS[stage]}
          </div>
          <div style={{ fontSize: 12, color: "#6b7280", marginTop: 2 }}>
            {status === "running"
              ? <span style={{ color: "#3b82f6", fontWeight: 600 }}>Running — analysing data…</span>
              : status === "complete"
              ? <span style={{ color: "#16a34a" }}>Complete</span>
              : status === "paused"
              ? <span style={{ color: "#d97706", fontWeight: 600 }}>Waiting for your input</span>
              : status === "failed"
              ? <span style={{ color: "#dc2626" }}>Failed</span>
              : STAGE_DESC[stage]}
          </div>
        </div>

        {status === "running" && (
          <div style={spinnerStyle}>⟳</div>
        )}

        {canToggle && (
          <span style={{ fontSize: 12, color: "#9ca3af", marginLeft: 8 }}>
            {open ? "▲" : "▼"}
          </span>
        )}
      </button>

      {/* Content */}
      {open && content && (
        <div style={{
          padding: "20px 28px",
          background: "#fff",
          borderTop: `1px solid ${STATUS_BORDER[status]}22`,
          fontSize: 14,
          lineHeight: 1.7,
          color: "#1f2937",
        }}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}

const spinnerStyle: React.CSSProperties = {
  fontSize: 20, color: "#3b82f6",
  animation: "lpt-spin 1s linear infinite",
  display: "inline-block",
};

// ── Combined stepper + panels ──────────────────────────────────────────────

export function StageStepper({
  run,
  stageFiles,
  onResumed,
}: {
  run: Run;
  stageFiles: Record<string, string>;
  onResumed?: () => void;
}) {
  const pathwayChoices: TargetChoice[] | null =
    run.pause_point === "pathway_choice" && run.pathway_choices_json
      ? (() => { try { return JSON.parse(run.pathway_choices_json); } catch { return null; } })()
      : null;

  return (
    <div>
      <StageProgressBar run={run} stageFiles={stageFiles} />
      {STAGE_ORDER.map((stage) => (
        <div key={stage}>
          <StagePanel stage={stage} run={run} stageFiles={stageFiles} />

          {/* Pathway choice panel — shown after pathway stage card */}
          {stage === "pathway" &&
            run.status === "PAUSED" &&
            run.pause_point === "pathway_choice" &&
            pathwayChoices &&
            onResumed && (
              <PathwayChoicePanel
                runId={run.id}
                choices={pathwayChoices}
                onResumed={onResumed}
              />
            )}

          {/* Structure choice panel — shown after structure stage card */}
          {stage === "structure" &&
            run.status === "PAUSED" &&
            run.pause_point === "structure_choice" &&
            onResumed && (
              <StructureChoicePanel
                runId={run.id}
                tractability={null}
                modality={null}
                bsaA2={null}
                targetComplex={run.target_complex}
                onResumed={onResumed}
              />
            )}

          {/* Structure needed panel — shown after pathway stage when no PDB was found */}
          {stage === "pathway" &&
            run.status === "PAUSED" &&
            run.pause_point === "structure_needed" &&
            onResumed && (
              <StructureNeededPanel
                runId={run.id}
                targetComplex={run.target_complex}
                onResumed={onResumed}
              />
            )}

          {/* Literature choice panel — shown after literature stage card */}
          {stage === "literature" &&
            run.status === "PAUSED" &&
            run.pause_point === "literature_choice" &&
            onResumed && (
              <LiteratureChoicePanel
                runId={run.id}
                goRecommendation={run.go_recommendation}
                onResumed={onResumed}
              />
            )}

          {/* Structure viewer + contact table — shown once structure stage is complete */}
          {stage === "structure" &&
            stageFiles["01_structure.md"] &&
            run.pdb_id && (
              <div style={{
                display: "flex",
                gap: 14,
                marginBottom: 14,
                alignItems: "stretch",
              }}>
                <div style={{ flex: 2, minWidth: 0 }}>
                  <ViewerErrorBoundary>
                    <Suspense fallback={
                      <div style={{
                        height: 450, display: "flex", alignItems: "center",
                        justifyContent: "center", background: "#f8fafc",
                        border: "1px solid #e5e7eb", borderRadius: 10,
                        color: "#9ca3af", fontSize: 13,
                      }}>
                        Loading viewer…
                      </div>
                    }>
                      <StructureViewer
                        pdbId={run.pdb_id}
                        hotspotResidues={run.hotspot_residues}
                      />
                    </Suspense>
                  </ViewerErrorBoundary>
                </div>
                <div style={{ flex: 1, minWidth: 200 }}>
                  <ContactTable hotspotResidues={run.hotspot_residues} />
                </div>
              </div>
            )}
        </div>
      ))}
    </div>
  );
}
