/**
 * Binder Campaign detail page.
 *
 * Shows all binders imported from a CSV design run, supports:
 * - CSV upload to populate binders
 * - CIF upload per binder
 * - Experimental measurement entry (Kd / Ki)
 * - Running the binder-optimizer skill (creates 4 child mutants)
 * - Manual mutation entry
 * - Sequence copy to clipboard
 * - Lineage tree (parent → mutants) or flat list view
 */
import { useState, useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Binder, type BinderCampaign as CampaignType, type BinderMeasurement } from "../lib/api";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function bestKd(measurements: BinderMeasurement[]): string {
  const kds = measurements.map((m) => m.kd_molar).filter((v): v is number => v !== null);
  if (!kds.length) return "—";
  const best = Math.min(...kds);
  // Display in nM for readability
  if (best < 1e-6) return `${(best * 1e9).toFixed(1)} nM`;
  if (best < 1e-3) return `${(best * 1e6).toFixed(1)} µM`;
  return `${best.toFixed(3)} M`;
}

function truncSeq(seq: string, n = 20): string {
  return seq.length > n ? seq.slice(0, n) + "…" : seq;
}

// ---------------------------------------------------------------------------
// Modals
// ---------------------------------------------------------------------------

function CsvUploadModal({
  campaignId,
  onClose,
}: {
  campaignId: number;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ created: number } | null>(null);

  async function handleUpload() {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      const res = await api.campaigns.uploadCsv(campaignId, file);
      setResult(res);
      qc.invalidateQueries({ queryKey: ["campaign", campaignId] });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
    }
  }

  return (
    <div style={S.overlay}>
      <div style={S.modal}>
        <h3 style={S.modalTitle}>Upload Design CSV</h3>
        <p style={S.hint}>
          CSV must contain: <code>designed_sequence</code>, <code>design_to_target_iptm</code>,
          <code>min_design_to_target_pae</code>, <code>filter_rmsd</code>
        </p>
        <input
          type="file"
          accept=".csv"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          style={{ marginBottom: 12 }}
        />
        {file && <p style={S.hint}>{file.name} selected</p>}
        {error && <p style={S.err}>{error}</p>}
        {result && <p style={{ color: "#22c55e", fontSize: 13 }}>{result.created} binders imported.</p>}
        <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
          <button style={S.btn} onClick={handleUpload} disabled={!file || uploading}>
            {uploading ? "Uploading…" : "Upload"}
          </button>
          <button style={S.btnSec} onClick={onClose}>
            {result ? "Close" : "Cancel"}
          </button>
        </div>
      </div>
    </div>
  );
}

function CifUploadModal({
  binder,
  onClose,
}: {
  binder: Binder;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function handleUpload() {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      await api.binders.uploadCif(binder.id, file);
      setDone(true);
      qc.invalidateQueries({ queryKey: ["campaign", binder.campaign_id] });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
    }
  }

  return (
    <div style={S.overlay}>
      <div style={S.modal}>
        <h3 style={S.modalTitle}>Upload CIF — {binder.name}</h3>
        <input
          type="file"
          accept=".cif,.pdb"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          style={{ marginBottom: 12 }}
        />
        {error && <p style={S.err}>{error}</p>}
        {done && <p style={{ color: "#22c55e", fontSize: 13 }}>CIF uploaded successfully.</p>}
        <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
          <button style={S.btn} onClick={handleUpload} disabled={!file || uploading || done}>
            {uploading ? "Uploading…" : "Upload"}
          </button>
          <button style={S.btnSec} onClick={onClose}>{done ? "Close" : "Cancel"}</button>
        </div>
      </div>
    </div>
  );
}

function MeasurementModal({
  binder,
  onClose,
}: {
  binder: Binder;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [method, setMethod] = useState("SPR");
  const [kdNm, setKdNm] = useState("");
  const [kiNm, setKiNm] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const kd = kdNm ? parseFloat(kdNm) * 1e-9 : undefined;
      const ki = kiNm ? parseFloat(kiNm) * 1e-9 : undefined;
      await api.binders.addMeasurement(binder.id, {
        method,
        kd_molar: isNaN(kd!) ? undefined : kd,
        ki_molar: isNaN(ki!) ? undefined : ki,
        notes: notes.trim() || undefined,
      });
      qc.invalidateQueries({ queryKey: ["campaign", binder.campaign_id] });
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div style={S.overlay}>
      <div style={S.modal}>
        <h3 style={S.modalTitle}>Add Measurement — {binder.name}</h3>
        <label style={S.label}>Method</label>
        <select style={S.input} value={method} onChange={(e) => setMethod(e.target.value)}>
          {["SPR", "ITC", "FP", "other"].map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
        <label style={{ ...S.label, marginTop: 10 }}>Kd (nM) <span style={S.hintSpan}>leave blank if not measured</span></label>
        <input style={S.input} type="number" placeholder="e.g. 150" value={kdNm} onChange={(e) => setKdNm(e.target.value)} />
        <label style={{ ...S.label, marginTop: 10 }}>Ki (nM) <span style={S.hintSpan}>optional</span></label>
        <input style={S.input} type="number" placeholder="e.g. 85" value={kiNm} onChange={(e) => setKiNm(e.target.value)} />
        <label style={{ ...S.label, marginTop: 10 }}>Notes</label>
        <input style={S.input} placeholder="e.g. 3 replicates, good quality sensorgram" value={notes} onChange={(e) => setNotes(e.target.value)} />
        {error && <p style={S.err}>{error}</p>}
        <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
          <button style={S.btn} onClick={handleSave} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
          <button style={S.btnSec} onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

function AddMutationModal({
  binder,
  onClose,
}: {
  binder: Binder;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [label, setLabel] = useState("");
  const [seq, setSeq] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    if (!seq.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await api.binders.addMutation(binder.id, {
        sequence: seq.trim().toUpperCase(),
        mutation_label: label.trim() || undefined,
        notes: notes.trim() || undefined,
      });
      qc.invalidateQueries({ queryKey: ["campaign", binder.campaign_id] });
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div style={S.overlay}>
      <div style={S.modal}>
        <h3 style={S.modalTitle}>Add Mutation — {binder.name}</h3>
        <label style={S.label}>Mutation label <span style={S.hintSpan}>e.g. A265E</span></label>
        <input style={S.input} placeholder="A265E" value={label} onChange={(e) => setLabel(e.target.value)} />
        <label style={{ ...S.label, marginTop: 10 }}>Full mutated sequence</label>
        <textarea
          style={{ ...S.input, height: 80, resize: "vertical", fontFamily: "monospace", fontSize: 12 }}
          placeholder="ACDEFGHIKLMNPQRSTVWY…"
          value={seq}
          onChange={(e) => setSeq(e.target.value)}
        />
        <label style={{ ...S.label, marginTop: 10 }}>Notes</label>
        <input style={S.input} placeholder="optional" value={notes} onChange={(e) => setNotes(e.target.value)} />
        {error && <p style={S.err}>{error}</p>}
        <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
          <button style={S.btn} onClick={handleSave} disabled={!seq.trim() || saving}>
            {saving ? "Saving…" : "Add"}
          </button>
          <button style={S.btnSec} onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

async function openReportInNewTab(binderId: number, name: string) {
  const token = localStorage.getItem("token");
  let markdown: string;
  try {
    const res = await fetch(api.binders.optimizerReportUrl(binderId), {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) throw new Error(res.statusText);
    markdown = await res.text();
  } catch (e: any) {
    alert("Failed to load report: " + e.message);
    return;
  }

  const title = `Optimizer Report — ${name}`.replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const html = `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>${title}</title>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"><\/script>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; line-height: 1.6; }
    h1, h2, h3 { color: #111; border-bottom: 1px solid #eee; padding-bottom: 4px; }
    code { background: #f3f4f6; padding: 2px 5px; border-radius: 3px; font-size: 0.9em; }
    pre { background: #f3f4f6; padding: 16px; border-radius: 6px; overflow-x: auto; }
    pre code { background: none; padding: 0; }
    table { border-collapse: collapse; width: 100%; margin: 12px 0; }
    th, td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }
    th { background: #f3f4f6; font-weight: 600; }
    blockquote { border-left: 4px solid #ddd; margin: 0; padding-left: 16px; color: #555; }
  </style>
</head>
<body>
  <div id="content"></div>
  <script>
    document.getElementById('content').innerHTML = marked.parse(${JSON.stringify(markdown)});
  <\/script>
</body>
</html>`;

  const blob = new Blob([html], { type: "text/html" });
  const url = URL.createObjectURL(blob);
  window.open(url, "_blank");
}

// ---------------------------------------------------------------------------
// Binder row
// ---------------------------------------------------------------------------

type Modal =
  | { type: "csv" }
  | { type: "cif"; binder: Binder }
  | { type: "measure"; binder: Binder }
  | { type: "mutate"; binder: Binder }
  | null;

function BinderRow({
  binder,
  depth,
  allBinders,
  openModal,
}: {
  binder: Binder;
  depth: number;
  allBinders: Binder[];
  openModal: (m: Modal) => void;
}) {
  const qc = useQueryClient();
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [optimizing, setOptimizing] = useState(false);

  const children = allBinders.filter((b) => b.parent_id === binder.id);

  function handleCopy() {
    navigator.clipboard.writeText(binder.sequence).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  async function handleRunOptimizer() {
    if (!binder.cif_path) {
      alert("Upload a CIF for this binder first.");
      return;
    }
    setOptimizing(true);
    try {
      await api.binders.runOptimizer(binder.id);
      // Poll every 5s for child binders to appear
      const pollInterval = setInterval(() => {
        qc.invalidateQueries({ queryKey: ["campaign", binder.campaign_id] });
      }, 5000);
      // Stop polling after 10 minutes
      setTimeout(() => clearInterval(pollInterval), 10 * 60 * 1000);
    } catch (e) {
      alert((e as Error).message);
      setOptimizing(false);
    }
  }

  const sourceLabel: Record<string, string> = {
    csv_import: "",
    optimizer: "AI",
    manual: "manual",
  };

  return (
    <>
      <tr style={{ background: depth % 2 === 0 ? "#fff" : "#f9fafb" }}>
        <td style={{ ...S.td, paddingLeft: 12 + depth * 20 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {children.length > 0 && (
              <button
                onClick={() => setExpanded((v) => !v)}
                style={{ background: "none", border: "none", cursor: "pointer", fontSize: 12, color: "#9ca3af", padding: 0, lineHeight: 1 }}
              >
                {expanded ? "▼" : "▶"}
              </button>
            )}
            <span style={{ fontWeight: 500, fontSize: 15 }}>{binder.name}</span>
            {binder.mutation_label && (
              <span style={S.badge}>{binder.mutation_label}</span>
            )}
            {sourceLabel[binder.source] && (
              <span style={{ ...S.badge, background: "#dbeafe", color: "#1d4ed8" }}>{sourceLabel[binder.source]}</span>
            )}
          </div>
        </td>
        <td style={S.td}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <code style={{ fontSize: 13, color: "#374151", letterSpacing: "0.02em" }}>{truncSeq(binder.sequence)}</code>
            <button onClick={handleCopy} style={S.copyBtn} title="Copy full sequence">
              {copied ? "✓" : "⎘"}
            </button>
          </div>
        </td>
        <td style={{ ...S.td, textAlign: "right" as const }}>
          {binder.design_to_target_iptm != null ? binder.design_to_target_iptm.toFixed(3) : "—"}
        </td>
        <td style={{ ...S.td, textAlign: "right" as const }}>
          {binder.min_design_to_target_pae != null ? binder.min_design_to_target_pae.toFixed(2) : "—"}
        </td>
        <td style={{ ...S.td, textAlign: "right" as const }}>
          {binder.filter_rmsd != null ? binder.filter_rmsd.toFixed(2) : "—"}
        </td>
        <td style={{ ...S.td, textAlign: "right" as const }}>
          {bestKd(binder.measurements)}
        </td>
        <td style={{ ...S.td }}>
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap" as const }}>
            <button
              style={S.actionBtn}
              onClick={() => openModal({ type: "cif", binder })}
              title={binder.cif_path ? "Replace CIF" : "Upload CIF"}
            >
              {binder.cif_path ? "CIF ✓" : "Upload CIF"}
            </button>
            <button style={S.actionBtn} onClick={() => setExpanded((v) => !v)}>
              {binder.measurements.length > 0 ? `Measurements (${binder.measurements.length})` : "Add Measurement"}
            </button>
            <button
              style={S.actionBtn}
              onClick={handleRunOptimizer}
              disabled={optimizing || !binder.cif_path}
              title={!binder.cif_path ? "Upload CIF first" : "Run binder optimizer"}
            >
              {optimizing ? "Optimizing…" : "Run Optimizer"}
            </button>
            <button style={S.actionBtn} onClick={() => openModal({ type: "mutate", binder })}>
              + Mutation
            </button>
            {binder.optimizer_report_path && (
              <button style={S.actionBtn} onClick={() => openReportInNewTab(binder.id, binder.name)}>
                View Report
              </button>
            )}
          </div>
        </td>
      </tr>

      {/* Expanded: measurements */}
      {expanded && (
        <tr>
          <td colSpan={7} style={{ padding: "0 12px 8px", background: "#f0f9ff" }}>
            <div style={{ paddingLeft: 12 + depth * 20 }}>
              {binder.measurements.length === 0 ? (
                <span style={{ fontSize: 14, color: "#9ca3af" }}>No measurements yet. </span>
              ) : (
                <table style={{ fontSize: 14, width: "auto", borderCollapse: "collapse" }}>
                  <thead>
                    <tr>
                      {["Method", "Kd", "Ki", "Notes", ""].map((h) => (
                        <th key={h} style={{ padding: "6px 14px", color: "#6b7280", fontWeight: 600, textAlign: "left" as const }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {binder.measurements.map((m) => (
                      <MeasurementRow key={m.id} m={m} binderId={binder.id} campaignId={binder.campaign_id} />
                    ))}
                  </tbody>
                </table>
              )}
              <button
                style={{ ...S.actionBtn, marginTop: 6 }}
                onClick={() => openModal({ type: "measure", binder })}
              >
                + Add Measurement
              </button>
            </div>
          </td>
        </tr>
      )}

      {/* Children rows */}
      {expanded && children.map((child) => (
        <BinderRow
          key={child.id}
          binder={child}
          depth={depth + 1}
          allBinders={allBinders}
          openModal={openModal}
        />
      ))}
    </>
  );
}

function MeasurementRow({
  m,
  binderId,
  campaignId,
}: {
  m: BinderMeasurement;
  binderId: number;
  campaignId: number;
}) {
  const qc = useQueryClient();
  const [deleting, setDeleting] = useState(false);

  async function handleDelete() {
    if (!confirm("Delete this measurement?")) return;
    setDeleting(true);
    try {
      await api.binders.deleteMeasurement(binderId, m.id);
      qc.invalidateQueries({ queryKey: ["campaign", campaignId] });
    } catch (e) {
      alert((e as Error).message);
      setDeleting(false);
    }
  }

  function fmt(v: number | null) {
    if (v == null) return "—";
    if (v < 1e-6) return `${(v * 1e9).toFixed(1)} nM`;
    if (v < 1e-3) return `${(v * 1e6).toFixed(1)} µM`;
    return `${v.toFixed(3)} M`;
  }

  return (
    <tr>
      <td style={{ padding: "6px 14px" }}>{m.method}</td>
      <td style={{ padding: "6px 14px" }}>{fmt(m.kd_molar)}</td>
      <td style={{ padding: "6px 14px" }}>{fmt(m.ki_molar)}</td>
      <td style={{ padding: "6px 14px", color: "#6b7280" }}>{m.notes ?? "—"}</td>
      <td style={{ padding: "6px 14px" }}>
        <button
          style={{ background: "none", border: "none", color: "#d1d5db", cursor: "pointer", fontSize: 13 }}
          onClick={handleDelete}
          disabled={deleting}
        >
          {deleting ? "…" : "✕"}
        </button>
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function BinderCampaign() {
  const { id } = useParams<{ id: string }>();
  const campaignId = Number(id);

  const [modal, setModal] = useState<Modal>(null);

  const { data: campaign, isLoading } = useQuery<CampaignType>({
    queryKey: ["campaign", campaignId],
    queryFn: () => api.campaigns.get(campaignId),
    refetchInterval: 5000, // poll to pick up optimizer children
  });

  if (isLoading) return <div style={S.page}>Loading…</div>;
  if (!campaign) return <div style={S.page}>Campaign not found.</div>;

  const binders: Binder[] = campaign.binders ?? [];
  // Top-level binders (no parent)
  const roots = binders.filter((b) => b.parent_id == null);

  return (
    <div style={S.page}>
      {/* Breadcrumb */}
      <div style={S.breadcrumb}>
        <Link to={`/projects/${campaign.project_id}`} style={S.link}>Project</Link>
        {" / "}
        {campaign.name}
      </div>

      <div style={S.header}>
        <div>
          <h1 style={S.h1}>{campaign.name}</h1>
          {campaign.target_name && (
            <div style={{ fontSize: 13, color: "#6b7280", marginTop: 2 }}>Target: {campaign.target_name}</div>
          )}
        </div>
        <button style={S.btn} onClick={() => setModal({ type: "csv" })}>
          {campaign.csv_filename ? `Reimport CSV` : "Upload CSV"}
        </button>
      </div>

      {binders.length === 0 && (
        <div style={{ textAlign: "center" as const, padding: "48px 0", color: "#9ca3af" }}>
          <p>No binders yet. Upload a design CSV to get started.</p>
          <button style={S.btn} onClick={() => setModal({ type: "csv" })}>
            Upload CSV
          </button>
        </div>
      )}

      {binders.length > 0 && (
        <div style={{ overflowX: "auto" as const }}>
          <table style={S.table}>
            <thead>
              <tr>
                {["Name", "Sequence", "iPTM", "iPAE", "RMSD", "Best Kd", "Actions"].map((h) => (
                  <th key={h} style={S.th}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {roots.map((b) => (
                <BinderRow
                  key={b.id}
                  binder={b}
                  depth={0}
                  allBinders={binders}
                  openModal={setModal}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Modals */}
      {modal?.type === "csv" && (
        <CsvUploadModal campaignId={campaignId} onClose={() => setModal(null)} />
      )}
      {modal?.type === "cif" && modal.binder && (
        <CifUploadModal binder={modal.binder} onClose={() => setModal(null)} />
      )}
      {modal?.type === "measure" && modal.binder && (
        <MeasurementModal binder={modal.binder} onClose={() => setModal(null)} />
      )}
      {modal?.type === "mutate" && modal.binder && (
        <AddMutationModal binder={modal.binder} onClose={() => setModal(null)} />
      )}

    </div>
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const S: Record<string, React.CSSProperties> = {
  page: { maxWidth: 1200, margin: "0 auto", padding: "36px 28px", fontFamily: "system-ui, sans-serif" },
  breadcrumb: { fontSize: 14, color: "#6b7280", marginBottom: 18 },
  link: { color: "#3b82f6", textDecoration: "none" },
  header: { display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 28 },
  h1: { margin: 0, fontSize: 28, fontWeight: 700 },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 15, background: "#fff", border: "1px solid #e5e7eb", borderRadius: 10 },
  th: { padding: "13px 16px", textAlign: "left" as const, fontSize: 13, fontWeight: 600, color: "#6b7280", borderBottom: "2px solid #e5e7eb", background: "#f9fafb" },
  td: { padding: "12px 16px", borderBottom: "1px solid #f3f4f6", verticalAlign: "middle" as const },
  badge: { fontSize: 11, fontWeight: 700, padding: "2px 7px", borderRadius: 4, background: "#fef3c7", color: "#b45309" },
  copyBtn: {
    background: "none", border: "1px solid #d1d5db", borderRadius: 5,
    cursor: "pointer", fontSize: 14, padding: "2px 7px", color: "#6b7280",
  },
  actionBtn: {
    padding: "5px 11px", fontSize: 13, background: "#f3f4f6", color: "#374151",
    border: "1px solid #e5e7eb", borderRadius: 6, cursor: "pointer", whiteSpace: "nowrap" as const,
    fontWeight: 500,
  },
  btn: {
    padding: "9px 18px", background: "#18181b", color: "#fff",
    border: "none", borderRadius: 7, cursor: "pointer", fontWeight: 600, fontSize: 15,
  },
  btnSec: {
    padding: "9px 18px", background: "#f3f4f6", color: "#374151",
    border: "1px solid #d1d5db", borderRadius: 7, cursor: "pointer", fontSize: 15,
  },
  overlay: {
    position: "fixed" as const, inset: 0, background: "rgba(0,0,0,.4)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50,
  },
  modal: {
    background: "#fff", padding: 28, borderRadius: 12,
    width: 500, maxWidth: "95vw", boxShadow: "0 8px 32px rgba(0,0,0,.15)",
  },
  modalTitle: { margin: "0 0 16px", fontSize: 19, fontWeight: 700 },
  label: { display: "block", fontSize: 14, fontWeight: 600, marginBottom: 5, color: "#374151" },
  input: {
    width: "100%", padding: "9px 13px", border: "1px solid #d1d5db",
    borderRadius: 7, fontSize: 15, boxSizing: "border-box" as const, display: "block",
  },
  hint: { fontSize: 13, color: "#6b7280", margin: "0 0 10px" },
  hintSpan: { color: "#9ca3af", fontWeight: 400, fontSize: 13 },
  err: { color: "#ef4444", fontSize: 14, margin: "8px 0 0" },
};
