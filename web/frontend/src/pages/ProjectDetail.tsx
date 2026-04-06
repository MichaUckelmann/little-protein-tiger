import { useState } from "react";
import { Link, useParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Run } from "../lib/api";

const STATUS_COLOR: Record<string, string> = {
  QUEUED: "#9ca3af",
  RUNNING: "#3b82f6",
  COMPLETE: "#22c55e",
  FAILED: "#ef4444",
  BLOCKED: "#f59e0b",
};

function RunRow({ run }: { run: Run }) {
  return (
    <Link to={`/runs/${run.id}`} style={styles.runRow}>
      <div style={{ flex: 1 }}>
        <div style={{ fontWeight: 500, marginBottom: 2, fontSize: 14 }}>
          {run.target_complex ?? run.query.slice(0, 80)}
        </div>
        <div style={{ fontSize: 12, color: "#9ca3af" }}>
          {new Date(run.created_at).toLocaleString()}
          {run.pdb_id && ` · PDB ${run.pdb_id}`}
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {run.go_recommendation && (
          <span style={{ fontSize: 11, fontWeight: 600, color: "#374151" }}>
            {run.go_recommendation.replace("_", " ")}
          </span>
        )}
        <span style={{
          fontSize: 11, fontWeight: 600,
          color: STATUS_COLOR[run.status] ?? "#9ca3af",
        }}>
          {run.status}
        </span>
      </div>
    </Link>
  );
}

export default function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const projectId = Number(id);

  const { data, isLoading } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.projects.get(projectId),
  });

  const [showRun, setShowRun] = useState(false);
  const [query, setQuery] = useState("");
  const [pdbId, setPdbId] = useState("");
  const [modelKey, setModelKey] = useState("claude/claude-sonnet-4-6");

  const MODEL_OPTIONS = [
    { label: "Claude Sonnet 4.6 (default)", value: "claude/claude-sonnet-4-6" },
    { label: "Claude Haiku 4.5 (fast)", value: "claude/claude-haiku-4-5-20251001" },
    { label: "Gemini Flash Lite (experimental)", value: "gemini/gemini-3.1-flash-lite-preview" },
  ];

  const createRun = useMutation({
    mutationFn: () => {
      const [provider, model_id] = modelKey.split("/");
      return api.runs.create(projectId, {
        query: query.trim(),
        pdb_id: pdbId.trim() || undefined,
        provider,
        model_id,
      });
    },
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      setShowRun(false);
      setQuery("");
      setPdbId("");
      setModelKey("claude/claude-sonnet-4-6");
      navigate(`/runs/${run.id}`);
    },
  });

  if (isLoading) return <div style={styles.loading}>Loading…</div>;
  if (!data) return <div style={styles.loading}>Project not found.</div>;

  const { project, runs } = data;

  return (
    <div style={styles.page}>
      <div style={styles.breadcrumb}>
        <Link to="/" style={styles.link}>Projects</Link> / {project.name}
      </div>

      <div style={styles.header}>
        <h1 style={styles.h1}>{project.name}</h1>
        <button style={styles.btn} onClick={() => setShowRun(true)}>
          + New Run
        </button>
      </div>

      {showRun && (
        <div style={styles.modal}>
          <div style={styles.modalBox}>
            <h2 style={{ margin: "0 0 16px", fontSize: 18 }}>New Design Run</h2>
            <label style={styles.label}>Query</label>
            <textarea
              autoFocus
              style={{ ...styles.input, height: 80, resize: "vertical" }}
              placeholder="e.g. Design PPI inhibitors targeting SCAP/SREBP in NASH"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <label style={{ ...styles.label, marginTop: 12 }}>
              PDB ID <span style={{ color: "#9ca3af", fontWeight: 400 }}>(optional — skips pathway stage)</span>
            </label>
            <input
              style={styles.input}
              placeholder="e.g. 5GPD"
              value={pdbId}
              onChange={(e) => setPdbId(e.target.value.toUpperCase())}
            />
            <label style={{ ...styles.label, marginTop: 12 }}>Model</label>
            <select
              style={styles.input}
              value={modelKey}
              onChange={(e) => setModelKey(e.target.value)}
            >
              {MODEL_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
            <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
              <button
                style={styles.btn}
                disabled={!query.trim() || createRun.isPending}
                onClick={() => createRun.mutate()}
              >
                {createRun.isPending ? "Submitting…" : "Start Run"}
              </button>
              <button style={styles.btnSecondary} onClick={() => setShowRun(false)}>
                Cancel
              </button>
            </div>
            {createRun.isError && (
              <p style={{ color: "#ef4444", marginTop: 8, fontSize: 13 }}>
                {(createRun.error as Error).message}
              </p>
            )}
          </div>
        </div>
      )}

      {runs.length === 0 ? (
        <p style={{ color: "#6b7280" }}>No runs yet. Start a new design run above.</p>
      ) : (
        <div>{runs.map((r) => <RunRow key={r.id} run={r} />)}</div>
      )}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: 800, margin: "0 auto", padding: "32px 20px", fontFamily: "system-ui, sans-serif" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24 },
  h1: { margin: 0, fontSize: 24, fontWeight: 700 },
  loading: { padding: 40, fontFamily: "system-ui, sans-serif", color: "#6b7280" },
  breadcrumb: { fontSize: 13, color: "#6b7280", marginBottom: 16 },
  link: { color: "#3b82f6", textDecoration: "none" },
  runRow: {
    display: "flex", alignItems: "center", padding: "12px 16px",
    border: "1px solid #e5e7eb", borderRadius: 8, marginBottom: 8,
    textDecoration: "none", color: "inherit", background: "#fff",
  },
  btn: {
    padding: "8px 16px", background: "#18181b", color: "#fff",
    border: "none", borderRadius: 6, cursor: "pointer", fontWeight: 600, fontSize: 14,
  },
  btnSecondary: {
    padding: "8px 16px", background: "#f3f4f6", color: "#374151",
    border: "1px solid #d1d5db", borderRadius: 6, cursor: "pointer", fontSize: 14,
  },
  input: {
    width: "100%", padding: "8px 12px", border: "1px solid #d1d5db",
    borderRadius: 6, fontSize: 14, boxSizing: "border-box" as const, display: "block",
  },
  label: { display: "block", fontSize: 13, fontWeight: 600, marginBottom: 4, color: "#374151" },
  modal: {
    position: "fixed" as const, inset: 0, background: "rgba(0,0,0,.4)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50,
  },
  modalBox: {
    background: "#fff", padding: 24, borderRadius: 10,
    width: 480, boxShadow: "0 8px 32px rgba(0,0,0,.15)",
  },
};
