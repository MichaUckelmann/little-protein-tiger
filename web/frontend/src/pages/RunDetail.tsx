import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useRunStatus } from "../lib/sse";
import { StageStepper } from "../components/StagePanel";
import { GoRecommendation } from "../components/GoRecommendation";

function fmt(dateStr: string | null | undefined): string {
  if (!dateStr) return "";
  const d = new Date(dateStr);
  return isNaN(d.getTime()) ? "" : d.toLocaleString();
}

export default function RunDetail() {
  const { id } = useParams<{ id: string }>();
  const runId = Number(id);
  const qc = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.runs.get(runId),
  });

  const liveStatus = useRunStatus(runId, data?.run.status);

  useEffect(() => {
    if (liveStatus?.status && ["COMPLETE", "FAILED", "BLOCKED", "PAUSED"].includes(liveStatus.status)) {
      qc.invalidateQueries({ queryKey: ["run", runId] });
    }
  }, [liveStatus?.status, runId, qc]);

  function handleResumed() {
    qc.invalidateQueries({ queryKey: ["run", runId] });
  }

  const [retrying, setRetrying] = useState(false);
  async function handleRetry() {
    setRetrying(true);
    try {
      await api.runs.retry(runId);
      qc.invalidateQueries({ queryKey: ["run", runId] });
    } finally {
      setRetrying(false);
    }
  }

  if (isLoading) return <div style={styles.loading}>Loading…</div>;
  if (!data) return <div style={styles.loading}>Run not found.</div>;

  const run = liveStatus
    ? {
        ...data.run,
        status: liveStatus.status as typeof data.run.status,
        stage_current: liveStatus.stage_current,
        pdb_id: liveStatus.pdb_id ?? data.run.pdb_id,
        target_complex: liveStatus.target_complex ?? data.run.target_complex,
        go_recommendation: liveStatus.go_recommendation ?? data.run.go_recommendation,
        error: liveStatus.error ?? data.run.error,
        pause_point: liveStatus.pause_point ?? data.run.pause_point,
        pathway_choices_json: liveStatus.pathway_choices_json ?? data.run.pathway_choices_json,
        completed_at: liveStatus.completed_at ?? data.run.completed_at,
      }
    : data.run;

  const title = run.target_complex ?? (run.id ? `Run #${run.id}` : "Run");
  const started = fmt(run.created_at);
  const completed = fmt(run.completed_at);
  const isRunning = run.status === "QUEUED" || run.status === "RUNNING";
  const isPaused = run.status === "PAUSED";

  return (
    <div style={styles.page}>
      {/* Breadcrumb */}
      <div style={styles.breadcrumb}>
        <Link to="/" style={styles.link}>Projects</Link>
        {" / "}
        <Link to={`/projects/${run.project_id}`} style={styles.link}>Project</Link>
        {" / "}
        {run.id ? `Run #${run.id}` : "Run"}
      </div>

      {/* Header */}
      <div style={styles.header}>
        <div>
          <h1 style={styles.h1}>{title}</h1>
          <div style={styles.meta}>
            {run.pdb_id && <span>PDB <strong>{run.pdb_id}</strong> · </span>}
            {started && <span>Started {started}</span>}
            {completed && <span> · Completed {completed}</span>}
            {run.model_id && (
              <span> · {run.model_id}{run.extended_thinking ? " + thinking" : ""}</span>
            )}
          </div>
        </div>

        {/* Status pill */}
        <div style={{
          ...styles.statusPill,
          background: STATUS_BG[run.status] ?? "#f3f4f6",
          color: STATUS_FG[run.status] ?? "#374151",
          borderColor: STATUS_BORDER_COL[run.status] ?? "#d1d5db",
        }}>
          {isRunning && <span style={styles.pillSpinner}>⟳ </span>}
          {run.status}
        </div>
      </div>

      {/* Paused banner */}
      {isPaused && (
        <div style={styles.pausedBox}>
          <strong>Waiting for your input</strong> — choose a target or next step below to continue the pipeline.
        </div>
      )}

      {/* Error / blocked banners */}
      {run.status === "FAILED" && run.error && (
        <div style={styles.errorBox}>
          <strong>Run failed:</strong> {run.error}
          {run.error.includes("account settings") && (
            <span> → <Link to="/settings" style={{ color: "#991b1b", fontWeight: 600 }}>Go to settings</Link></span>
          )}
          <button
            onClick={handleRetry}
            disabled={retrying}
            style={styles.retryBtn}
          >
            {retrying ? "Retrying…" : "↺ Retry from failed stage"}
          </button>
        </div>
      )}
      {run.status === "BLOCKED" && run.error && (
        <div style={styles.warningBox}>
          <strong>Blocked:</strong> {run.error}
          <button onClick={handleRetry} disabled={retrying} style={styles.retryBtn}>
            {retrying ? "Retrying…" : "↺ Retry"}
          </button>
        </div>
      )}

      {/* GO recommendation */}
      {run.status === "COMPLETE" && (
        <GoRecommendation recommendation={run.go_recommendation} />
      )}

      {/* Stage stepper + panels */}
      <StageStepper run={run} stageFiles={data.stages} onResumed={handleResumed} />

      {/* Design file downloads */}
      {run.status === "COMPLETE" && data.stages["03_design_report.md"] && (
        <div style={styles.downloadSection}>
          <h3 style={{ margin: "0 0 8px", fontSize: 15 }}>Download Design Inputs</h3>
          <a
            href={api.runs.fileUrl(runId, "03_design_report.md")}
            style={styles.downloadLink}
            download
          >
            03_design_report.md
          </a>
        </div>
      )}
    </div>
  );
}

const STATUS_BG: Record<string, string> = {
  QUEUED: "#f3f4f6", RUNNING: "#eff6ff", COMPLETE: "#f0fdf4",
  FAILED: "#fef2f2", BLOCKED: "#fffbeb", PAUSED: "#f0f9ff",
};
const STATUS_FG: Record<string, string> = {
  QUEUED: "#6b7280", RUNNING: "#1d4ed8", COMPLETE: "#16a34a",
  FAILED: "#991b1b", BLOCKED: "#92400e", PAUSED: "#0369a1",
};
const STATUS_BORDER_COL: Record<string, string> = {
  QUEUED: "#d1d5db", RUNNING: "#bfdbfe", COMPLETE: "#bbf7d0",
  FAILED: "#fca5a5", BLOCKED: "#fcd34d", PAUSED: "#7dd3fc",
};

const styles: Record<string, React.CSSProperties> = {
  page: {
    maxWidth: 1100, margin: "0 auto",
    padding: "28px 32px", fontFamily: "system-ui, sans-serif",
  },
  h1: { margin: "0 0 4px", fontSize: 22, fontWeight: 700 },
  loading: { padding: 40, fontFamily: "system-ui, sans-serif", color: "#6b7280" },
  breadcrumb: { fontSize: 13, color: "#6b7280", marginBottom: 14 },
  link: { color: "#3b82f6", textDecoration: "none" },
  header: {
    display: "flex", alignItems: "flex-start",
    justifyContent: "space-between", marginBottom: 20, gap: 16,
  },
  meta: { fontSize: 13, color: "#6b7280", marginTop: 4 },
  statusPill: {
    display: "inline-flex", alignItems: "center",
    padding: "5px 12px", borderRadius: 20, fontSize: 12, fontWeight: 700,
    border: "1px solid", whiteSpace: "nowrap", marginTop: 4,
    letterSpacing: "0.04em",
  },
  pillSpinner: { animation: "lpt-spin 1s linear infinite", display: "inline-block", marginRight: 4 },
  errorBox: {
    padding: "12px 16px", background: "#fef2f2", border: "1px solid #fca5a5",
    borderRadius: 8, marginBottom: 20, fontSize: 14, color: "#991b1b",
  },
  warningBox: {
    padding: "12px 16px", background: "#fffbeb", border: "1px solid #fcd34d",
    borderRadius: 8, marginBottom: 20, fontSize: 14, color: "#92400e",
  },
  pausedBox: {
    padding: "12px 16px", background: "#f0f9ff", border: "1px solid #38bdf8",
    borderRadius: 8, marginBottom: 20, fontSize: 14, color: "#0369a1",
  },
  retryBtn: {
    marginLeft: 12, padding: "4px 10px", fontSize: 12, fontWeight: 600,
    borderRadius: 5, border: "1px solid currentColor", background: "transparent",
    cursor: "pointer", color: "inherit", opacity: 0.85,
  },
  downloadSection: {
    marginTop: 20, padding: "16px 20px", background: "#f9fafb",
    border: "1px solid #e5e7eb", borderRadius: 8,
  },
  downloadLink: { fontSize: 13, color: "#3b82f6" },
};
