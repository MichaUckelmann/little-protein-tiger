import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useRunStatus } from "../lib/sse";
import { StageStepper } from "../components/StagePanel";
import { GoRecommendation } from "../components/GoRecommendation";

const STAGE_LABEL: Record<string, string> = {
  pathway: "Pathway Expert",
  structure: "Structure Analysis",
  literature: "Literature Review",
  design: "Design Inputs",
  optimizer: "Binder Optimizer",
};

export default function RunDetail() {
  const { id } = useParams<{ id: string }>();
  const runId = Number(id);
  const qc = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.runs.get(runId),
  });

  // Live status via SSE — merges into the cached run data
  const liveStatus = useRunStatus(runId, data?.run.status);

  // Re-fetch run detail when status becomes terminal (to pick up final stage files)
  useEffect(() => {
    if (liveStatus?.status === "COMPLETE" || liveStatus?.status === "FAILED") {
      qc.invalidateQueries({ queryKey: ["run", runId] });
    }
  }, [liveStatus?.status, runId, qc]);

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
        completed_at: liveStatus.completed_at ?? data.run.completed_at,
      }
    : data.run;

  const isRunning = run.status === "QUEUED" || run.status === "RUNNING";

  return (
    <div style={styles.page}>
      {/* Breadcrumb */}
      <div style={styles.breadcrumb}>
        <Link to="/" style={styles.link}>Projects</Link>
        {" / "}
        <Link to={`/projects/${run.project_id}`} style={styles.link}>Project</Link>
        {" / "}Run #{run.id}
      </div>

      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <h1 style={styles.h1}>
          {run.target_complex ?? `Run #${run.id}`}
        </h1>
        <div style={{ fontSize: 13, color: "#6b7280", marginTop: 4 }}>
          {run.pdb_id && <span>PDB: <strong>{run.pdb_id}</strong> · </span>}
          Started {new Date(run.created_at).toLocaleString()}
          {run.completed_at && ` · Completed ${new Date(run.completed_at).toLocaleString()}`}
        </div>
      </div>

      {/* Live status bar */}
      {isRunning && (
        <div style={styles.statusBar}>
          <span style={styles.spinner}>⟳</span>
          {run.stage_current
            ? `Running: ${STAGE_LABEL[run.stage_current] ?? run.stage_current}…`
            : "Queued — waiting for worker…"}
        </div>
      )}

      {/* Error */}
      {run.status === "FAILED" && run.error && (
        <div style={styles.errorBox}>
          <strong>Run failed:</strong> {run.error}
        </div>
      )}

      {run.status === "BLOCKED" && run.error && (
        <div style={styles.warningBox}>
          <strong>Blocked:</strong> {run.error}
        </div>
      )}

      {/* GO recommendation */}
      {run.status === "COMPLETE" && (
        <GoRecommendation recommendation={run.go_recommendation} />
      )}

      {/* Stage panels */}
      <StageStepper run={run} stageFiles={data.stages} />

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

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: 860, margin: "0 auto", padding: "32px 20px", fontFamily: "system-ui, sans-serif" },
  h1: { margin: 0, fontSize: 22, fontWeight: 700 },
  loading: { padding: 40, fontFamily: "system-ui, sans-serif", color: "#6b7280" },
  breadcrumb: { fontSize: 13, color: "#6b7280", marginBottom: 16 },
  link: { color: "#3b82f6", textDecoration: "none" },
  statusBar: {
    display: "flex", alignItems: "center", gap: 8,
    padding: "10px 14px", background: "#eff6ff", border: "1px solid #bfdbfe",
    borderRadius: 8, marginBottom: 20, fontSize: 14, color: "#1d4ed8",
  },
  spinner: { animation: "spin 1s linear infinite", display: "inline-block" },
  errorBox: {
    padding: "10px 14px", background: "#fef2f2", border: "1px solid #fca5a5",
    borderRadius: 8, marginBottom: 20, fontSize: 14, color: "#991b1b",
  },
  warningBox: {
    padding: "10px 14px", background: "#fffbeb", border: "1px solid #fcd34d",
    borderRadius: 8, marginBottom: 20, fontSize: 14, color: "#92400e",
  },
  downloadSection: {
    marginTop: 20, padding: "14px 16px", background: "#f9fafb",
    border: "1px solid #e5e7eb", borderRadius: 8,
  },
  downloadLink: { fontSize: 13, color: "#3b82f6" },
};
