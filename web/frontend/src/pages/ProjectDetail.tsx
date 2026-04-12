import { useState, useCallback } from "react";
import { Link, useParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Run, type BinderCampaign } from "../lib/api";

const STATUS_COLOR: Record<string, string> = {
  QUEUED: "#9ca3af",
  RUNNING: "#3b82f6",
  COMPLETE: "#22c55e",
  FAILED: "#ef4444",
  BLOCKED: "#f59e0b",
  PAUSED: "#0369a1",
};

function RunRow({ run, projectId, onDeleted, onRerun }: {
  run: Run;
  projectId: number;
  onDeleted: () => void;
  onRerun: (mode: string) => void;
}) {
  const [deleting, setDeleting] = useState(false);

  async function handleDelete(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (!window.confirm("Delete this run and all its output files?")) return;
    setDeleting(true);
    try {
      await api.runs.delete(run.id);
      onDeleted();
    } catch (err) {
      alert((err as Error).message);
      setDeleting(false);
    }
  }

  function handleRerun(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    const flipped = run.pathway_mode === "wildcard" ? "standard" : "wildcard";
    onRerun(flipped);
  }

  const isPathwayDone = run.stage_current != null || run.status === "COMPLETE"
    || run.status === "PAUSED" || run.status === "FAILED";
  const flippedLabel = run.pathway_mode === "wildcard" ? "Standard" : "Wildcard";

  return (
    <div style={{ position: "relative" }}>
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
          {run.pathway_mode === "wildcard" && (
            <span style={{
              fontSize: 10, fontWeight: 600, color: "#7c3aed",
              background: "#ede9fe", borderRadius: 4, padding: "1px 5px",
            }}>
              WILDCARD
            </span>
          )}
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
          {isPathwayDone && (
            <button
              onClick={handleRerun}
              title={`Re-run pathway as ${flippedLabel}`}
              style={styles.rerunBtn}
            >
              ↺ {flippedLabel}
            </button>
          )}
          <button
            onClick={handleDelete}
            disabled={deleting}
            title="Delete run"
            style={styles.deleteBtn}
          >
            {deleting ? "…" : "✕"}
          </button>
        </div>
      </Link>
    </div>
  );
}

function BinderCampaignsSection({ projectId }: { projectId: number }) {
  const qc = useQueryClient();
  const [showNew, setShowNew] = useState(false);
  const [campName, setCampName] = useState("");
  const [targetName, setTargetName] = useState("");

  const { data: campaigns = [], isLoading } = useQuery({
    queryKey: ["campaigns", projectId],
    queryFn: () => api.campaigns.list(projectId),
  });

  const createCampaign = useMutation({
    mutationFn: () =>
      api.campaigns.create(projectId, {
        name: campName.trim(),
        target_name: targetName.trim() || undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["campaigns", projectId] });
      setCampName("");
      setTargetName("");
      setShowNew(false);
    },
  });

  return (
    <div style={{ marginTop: 40 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>Binder Campaigns</h2>
        <button style={styles.btn} onClick={() => setShowNew(true)}>+ New Campaign</button>
      </div>

      {showNew && (
        <div style={{ background: "#f9fafb", border: "1px solid #e5e7eb", borderRadius: 8, padding: 16, marginBottom: 12 }}>
          <label style={styles.label}>Campaign name</label>
          <input
            autoFocus
            style={styles.input}
            placeholder="e.g. YAP1 BoltzGen round 1"
            value={campName}
            onChange={(e) => setCampName(e.target.value)}
          />
          <label style={{ ...styles.label, marginTop: 10 }}>
            Target <span style={{ color: "#9ca3af", fontWeight: 400 }}>(optional)</span>
          </label>
          <input
            style={styles.input}
            placeholder="e.g. YAP1-TEAD4"
            value={targetName}
            onChange={(e) => setTargetName(e.target.value)}
          />
          <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
            <button
              style={styles.btn}
              disabled={!campName.trim() || createCampaign.isPending}
              onClick={() => createCampaign.mutate()}
            >
              {createCampaign.isPending ? "Creating…" : "Create"}
            </button>
            <button style={styles.btnSecondary} onClick={() => setShowNew(false)}>Cancel</button>
          </div>
        </div>
      )}

      {isLoading && <p style={{ color: "#9ca3af", fontSize: 13 }}>Loading…</p>}
      {!isLoading && campaigns.length === 0 && (
        <p style={{ color: "#6b7280", fontSize: 13 }}>No campaigns yet. Import design run results above.</p>
      )}
      {campaigns.map((c: BinderCampaign) => (
        <Link key={c.id} to={`/campaigns/${c.id}`} style={styles.runRow}>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 500, fontSize: 14 }}>{c.name}</div>
            <div style={{ fontSize: 12, color: "#9ca3af" }}>
              {c.target_name && `${c.target_name} · `}
              {new Date(c.created_at).toLocaleString()}
              {c.csv_filename && ` · ${c.csv_filename}`}
            </div>
          </div>
          <span style={{ fontSize: 12, color: "#6b7280" }}>View →</span>
        </Link>
      ))}
    </div>
  );
}

export default function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const projectId = Number(id);

  const handleRunDeleted = useCallback(() => {
    qc.invalidateQueries({ queryKey: ["project", projectId] });
  }, [qc, projectId]);

  const handleRerunAsMode = useCallback(async (sourceRun: Run, newPathwayMode: string) => {
    try {
      const stageModels = sourceRun.stage_models_json ? JSON.parse(sourceRun.stage_models_json) : undefined;
      const newRun = await api.runs.create(projectId, {
        query: sourceRun.query,
        pdb_id: undefined, // always re-run from pathway stage
        provider: sourceRun.provider,
        model_id: sourceRun.model_id ?? undefined,
        stage_models: stageModels,
        extended_thinking: sourceRun.extended_thinking,
        auto_mode: sourceRun.auto_mode,
        pathway_mode: newPathwayMode,
      });
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      navigate(`/runs/${newRun.id}`);
    } catch (err) {
      alert((err as Error).message);
    }
  }, [projectId, qc, navigate]);

  const [deletingProject, setDeletingProject] = useState(false);
  async function handleDeleteProject() {
    if (!window.confirm("Delete this project and ALL its runs? This cannot be undone.")) return;
    setDeletingProject(true);
    try {
      await api.projects.delete(projectId);
      qc.invalidateQueries({ queryKey: ["projects"] });
      navigate("/", { replace: true });
    } catch (e) {
      alert((e as Error).message);
      setDeletingProject(false);
    }
  }

  const { data, isLoading } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.projects.get(projectId),
  });

  const [showRun, setShowRun] = useState(false);
  const [query, setQuery] = useState("");
  const [pdbId, setPdbId] = useState("");
  const [mode, setMode] = useState<"standard" | "economy" | "quality" | "custom">("standard");
  const [extThinking, setExtThinking] = useState(false);
  const [stageModels, setStageModels] = useState<Record<string, string>>({});
  const [showAdvanced, setShowAdvanced] = useState(false);
  // false = interactive (pause for review), true = fully automatic
  const [fullAuto, setFullAuto] = useState(false);
  // Pathway analysis mode
  const [pathwayMode, setPathwayMode] = useState<"standard" | "wildcard" | "both">("standard");

  // Uniform model selector — used in Standard mode and as Custom base
  const [modelKey, setModelKey] = useState("claude/claude-sonnet-4-6");

  const MODEL_OPTIONS = [
    { label: "Claude Sonnet 4.6 (default)", value: "claude/claude-sonnet-4-6" },
    { label: "Claude Haiku 4.5 (fast)", value: "claude/claude-haiku-4-5-20251001" },
    { label: "Gemini Flash Lite (experimental)", value: "gemini/gemini-3.1-flash-lite-preview" },
  ];

  const STAGE_MODEL_OPTIONS = [
    { label: "Same as base model", value: "" },
    { label: "Claude Sonnet 4.6", value: "claude-sonnet-4-6" },
    { label: "Claude Haiku 4.5", value: "claude-haiku-4-5-20251001" },
  ];

  // Resolve the actual API payload from the current mode/settings
  function resolveRunConfig(): { provider: string; model_id: string; stage_models?: Record<string, string>; extended_thinking?: boolean } {
    if (mode === "economy") {
      return {
        provider: "claude",
        model_id: "claude-sonnet-4-6",
        stage_models: {
          pathway: "claude-haiku-4-5-20251001",
          literature: "claude-haiku-4-5-20251001",
          structure: "claude-sonnet-4-6",
          design: "claude-sonnet-4-6",
        },
      };
    }
    if (mode === "quality") {
      return {
        provider: "claude",
        model_id: "claude-sonnet-4-6",
        extended_thinking: true,
      };
    }
    if (mode === "custom") {
      const [provider, model_id] = modelKey.split("/");
      const overrides = Object.fromEntries(
        Object.entries(stageModels).filter(([, v]) => v !== "")
      );
      return {
        provider,
        model_id,
        stage_models: Object.keys(overrides).length > 0 ? overrides : undefined,
        extended_thinking: provider === "claude" ? extThinking : undefined,
      };
    }
    // standard
    const [provider, model_id] = modelKey.split("/");
    return { provider, model_id };
  }

  function resetForm() {
    setShowRun(false);
    setQuery("");
    setPdbId("");
    setMode("standard");
    setExtThinking(false);
    setStageModels({});
    setShowAdvanced(false);
    setModelKey("claude/claude-sonnet-4-6");
    setPathwayMode("standard");
  }

  const createRun = useMutation({
    mutationFn: async () => {
      const config = resolveRunConfig();
      const base = {
        query: query.trim(),
        pdb_id: pdbId.trim() || undefined,
        auto_mode: fullAuto,
        ...config,
      };
      if (pathwayMode === "both") {
        // Create two runs in sequence; navigate to the first (standard) one
        const first = await api.runs.create(projectId, { ...base, pathway_mode: "standard" });
        await api.runs.create(projectId, { ...base, pathway_mode: "wildcard" });
        return first;
      }
      return api.runs.create(projectId, { ...base, pathway_mode: pathwayMode });
    },
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      resetForm();
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
        <div style={{ display: "flex", gap: 8 }}>
          <button style={styles.btn} onClick={() => setShowRun(true)}>
            + New Run
          </button>
          <button
            style={styles.btnDanger}
            onClick={handleDeleteProject}
            disabled={deletingProject}
            title="Delete project"
          >
            {deletingProject ? "Deleting…" : "Delete Project"}
          </button>
        </div>
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

            <label style={{ ...styles.label, marginTop: 12 }}>Pathway Analysis</label>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 6, marginBottom: 4 }}>
              {(["standard", "wildcard", "both"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setPathwayMode(m)}
                  style={{
                    ...styles.modeBtn,
                    ...(pathwayMode === m ? styles.modeBtnActive : {}),
                  }}
                >
                  {m === "standard" && "Standard"}
                  {m === "wildcard" && "Wildcard"}
                  {m === "both" && "Both"}
                </button>
              ))}
            </div>
            <p style={styles.modeHint}>
              {pathwayMode === "standard" && "Conservative — follows corpus evidence directly."}
              {pathwayMode === "wildcard" && "Creative — uses a training-knowledge bridge to generate novel hypotheses, then validates against the corpus."}
              {pathwayMode === "both" && "Creates two runs in parallel (Standard + Wildcard) for direct comparison."}
            </p>

            <label style={{ ...styles.label, marginTop: 12 }}>Mode</label>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginBottom: 4 }}>
              {(["standard", "economy", "quality", "custom"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => { setMode(m); if (m !== "custom") setShowAdvanced(false); }}
                  style={{
                    ...styles.modeBtn,
                    ...(mode === m ? styles.modeBtnActive : {}),
                  }}
                >
                  {m === "standard" && "Standard"}
                  {m === "economy" && "Economy"}
                  {m === "quality" && "Quality"}
                  {m === "custom" && "Custom"}
                </button>
              ))}
            </div>
            <p style={styles.modeHint}>
              {mode === "standard" && "Sonnet 4.6 for all stages."}
              {mode === "economy" && "Haiku for pathway + literature · Sonnet for structure + design. ~40% cheaper."}
              {mode === "quality" && "Sonnet for all stages + extended thinking on structure. Best results, ~30% more expensive."}
              {mode === "custom" && "Choose base model and per-stage overrides below."}
            </p>

            {/* Base model selector — shown in standard + custom modes */}
            {(mode === "standard" || mode === "custom") && (
              <>
                <label style={{ ...styles.label, marginTop: 8 }}>Base model</label>
                <select
                  style={styles.input}
                  value={modelKey}
                  onChange={(e) => setModelKey(e.target.value)}
                >
                  {MODEL_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
              </>
            )}

            {/* Extended thinking checkbox — custom + claude only */}
            {mode === "custom" && modelKey.startsWith("claude/") && (
              <label style={{ ...styles.checkLabel, marginTop: 10 }}>
                <input
                  type="checkbox"
                  checked={extThinking}
                  onChange={(e) => setExtThinking(e.target.checked)}
                  style={{ marginRight: 6 }}
                />
                Extended thinking on structure stage
                <span style={styles.hint}> (higher quality · ~2× cost on structure)</span>
              </label>
            )}

            {/* Per-stage overrides — custom mode only */}
            {mode === "custom" && (
              <>
                <button
                  style={styles.advancedToggle}
                  onClick={() => setShowAdvanced((v) => !v)}
                >
                  {showAdvanced ? "▾" : "▸"} Per-stage model overrides
                </button>
                {showAdvanced && (
                  <div style={styles.advancedBox}>
                    {(["pathway", "structure", "literature", "design"] as const).map((stage) => (
                      <div key={stage} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                        <span style={{ width: 80, fontSize: 12, color: "#6b7280", textTransform: "capitalize" }}>{stage}</span>
                        <select
                          style={{ ...styles.input, flex: 1, padding: "4px 8px", fontSize: 12 }}
                          value={stageModels[stage] ?? ""}
                          onChange={(e) => setStageModels((prev) => ({ ...prev, [stage]: e.target.value }))}
                        >
                          {STAGE_MODEL_OPTIONS.map((opt) => (
                            <option key={opt.value} value={opt.value}>{opt.label}</option>
                          ))}
                        </select>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}

            {/* Auto-mode toggle */}
            <label style={{ ...styles.checkLabel, marginTop: 14 }}>
              <input
                type="checkbox"
                checked={fullAuto}
                onChange={(e) => setFullAuto(e.target.checked)}
                style={{ marginRight: 6 }}
              />
              <span>
                Skip review pauses — run fully automatic
              </span>
            </label>
            {!fullAuto && (
              <p style={{ fontSize: 12, color: "#6b7280", margin: "4px 0 0 20px" }}>
                The pipeline will pause after pathway and structure stages so you can review results and choose the next step.
              </p>
            )}

            <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
              <button
                style={styles.btn}
                disabled={!query.trim() || createRun.isPending}
                onClick={() => createRun.mutate()}
              >
                {createRun.isPending
                  ? (pathwayMode === "both" ? "Creating 2 runs…" : "Submitting…")
                  : (pathwayMode === "both" ? "Start 2 Runs" : "Start Run")
                }
              </button>
              <button style={styles.btnSecondary} onClick={resetForm}>
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

      <h2 style={{ fontSize: 16, fontWeight: 600, margin: "0 0 12px" }}>Design Runs</h2>
      {runs.length === 0 ? (
        <p style={{ color: "#6b7280" }}>No runs yet. Start a new design run above.</p>
      ) : (
        <div>{runs.map((r) => (
          <RunRow
            key={r.id}
            run={r}
            projectId={projectId}
            onDeleted={handleRunDeleted}
            onRerun={(mode) => handleRerunAsMode(r, mode)}
          />
        ))}</div>
      )}

      <BinderCampaignsSection projectId={projectId} />
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
  btnDanger: {
    padding: "8px 16px", background: "transparent", color: "#9ca3af",
    border: "1px solid #e5e7eb", borderRadius: 6, cursor: "pointer", fontSize: 13,
  },
  deleteBtn: {
    padding: "2px 7px", background: "transparent", color: "#d1d5db",
    border: "1px solid #e5e7eb", borderRadius: 4, cursor: "pointer",
    fontSize: 11, lineHeight: 1, flexShrink: 0,
  },
  rerunBtn: {
    padding: "2px 7px", background: "transparent", color: "#7c3aed",
    border: "1px solid #c4b5fd", borderRadius: 4, cursor: "pointer",
    fontSize: 11, lineHeight: 1, flexShrink: 0, whiteSpace: "nowrap" as const,
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
  modeBtn: {
    padding: "7px 12px", background: "#f3f4f6", color: "#374151",
    border: "1px solid #d1d5db", borderRadius: 6, cursor: "pointer",
    fontSize: 13, fontWeight: 500, textTransform: "capitalize" as const,
  },
  modeBtnActive: {
    background: "#18181b", color: "#fff", border: "1px solid #18181b",
  },
  modeHint: {
    fontSize: 12, color: "#6b7280", margin: "4px 0 0", lineHeight: 1.4,
  },
  checkLabel: {
    display: "flex", alignItems: "center", fontSize: 13, color: "#374151",
    cursor: "pointer",
  },
  hint: { color: "#9ca3af", fontWeight: 400 },
  advancedToggle: {
    background: "none", border: "none", color: "#6b7280", fontSize: 12,
    cursor: "pointer", padding: "6px 0 2px", textAlign: "left" as const,
  },
  advancedBox: {
    background: "#f9fafb", border: "1px solid #e5e7eb", borderRadius: 6,
    padding: "10px 12px", marginTop: 4,
  },
  modal: {
    position: "fixed" as const, inset: 0, background: "rgba(0,0,0,.4)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50,
  },
  modalBox: {
    background: "#fff", padding: 24, borderRadius: 10,
    width: 480, boxShadow: "0 8px 32px rgba(0,0,0,.15)",
  },
};
