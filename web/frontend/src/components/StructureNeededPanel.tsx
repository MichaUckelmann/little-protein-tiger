/**
 * StructureNeededPanel — shown when the run is paused at "structure_needed".
 * The pathway expert recommended a target but found no PDB structure in the corpus.
 * The user can provide a 4-char PDB accession (downloaded from RCSB) or upload a .cif file.
 */
import { useRef, useState } from "react";
import { api } from "../lib/api";

interface Props {
  runId: number;
  targetComplex: string | null;
  onResumed: () => void;
}

export function StructureNeededPanel({ runId, targetComplex, onResumed }: Props) {
  const [mode, setMode] = useState<"pdb_id" | "upload">("pdb_id");
  const [pdbId, setPdbId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const pdbIdValid = /^[A-Za-z0-9]{4}$/.test(pdbId.trim());
  const canSubmit = mode === "pdb_id" ? pdbIdValid : file !== null;

  async function handleSubmit() {
    setLoading(true);
    setError(null);
    try {
      if (mode === "pdb_id") {
        await api.runs.resume(runId, { pdb_id: pdbId.trim().toUpperCase() });
      } else if (file) {
        await api.runs.uploadStructure(runId, file);
      }
      onResumed();
    } catch (e: any) {
      setError(e.message ?? "Failed to resume run");
      setLoading(false);
    }
  }

  return (
    <div style={{
      margin: "16px 0",
      padding: "20px 24px",
      background: "#faf5ff",
      border: "1px solid #a855f7",
      borderRadius: "8px",
    }}>
      <div style={{ marginBottom: "16px" }}>
        <h3 style={{ margin: "0 0 4px", fontSize: "15px", fontWeight: 600, color: "#6b21a8" }}>
          Structure needed to continue
        </h3>
        <p style={{ margin: 0, fontSize: "13px", color: "#7e22ce" }}>
          {targetComplex
            ? <>The pathway expert recommended <strong>{targetComplex}</strong> but found no PDB structure in the corpus.</>
            : "The pathway expert could not find a PDB structure in the corpus for the recommended target."
          }{" "}
          Provide a structure to proceed with interface analysis.
        </p>
      </div>

      {/* Mode toggle */}
      <div style={{ display: "flex", gap: "8px", marginBottom: "16px" }}>
        {(["pdb_id", "upload"] as const).map((m) => (
          <button
            key={m}
            onClick={() => { setMode(m); setError(null); }}
            style={{
              padding: "6px 14px",
              borderRadius: "6px",
              border: mode === m ? "2px solid #a855f7" : "1px solid #d1d5db",
              background: mode === m ? "#f3e8ff" : "#fff",
              color: mode === m ? "#6b21a8" : "#374151",
              fontSize: "13px",
              fontWeight: mode === m ? 600 : 400,
              cursor: "pointer",
            }}
          >
            {m === "pdb_id" ? "Enter PDB ID" : "Upload .cif file"}
          </button>
        ))}
      </div>

      {mode === "pdb_id" ? (
        <div style={{ display: "flex", gap: "8px", alignItems: "center", marginBottom: "12px" }}>
          <input
            type="text"
            placeholder="e.g. 5GN0"
            value={pdbId}
            onChange={(e) => setPdbId(e.target.value.toUpperCase().slice(0, 4))}
            maxLength={4}
            style={{
              width: "100px",
              padding: "7px 10px",
              border: "1px solid #d1d5db",
              borderRadius: "6px",
              fontSize: "14px",
              fontFamily: "monospace",
              letterSpacing: "0.05em",
            }}
          />
          <a
            href={`https://www.rcsb.org/search?query=${encodeURIComponent(targetComplex ?? "")}`}
            target="_blank"
            rel="noopener noreferrer"
            style={{ fontSize: "12px", color: "#6b21a8" }}
          >
            Search RCSB ↗
          </a>
        </div>
      ) : (
        <div style={{ marginBottom: "12px" }}>
          <input
            ref={fileRef}
            type="file"
            accept=".cif,.mmcif"
            style={{ display: "none" }}
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <button
            onClick={() => fileRef.current?.click()}
            style={{
              padding: "7px 14px",
              borderRadius: "6px",
              border: "1px solid #d1d5db",
              background: "#fff",
              fontSize: "13px",
              cursor: "pointer",
            }}
          >
            {file ? `Selected: ${file.name}` : "Choose .cif file…"}
          </button>
        </div>
      )}

      {error && (
        <div style={{ marginBottom: "12px", fontSize: "13px", color: "#dc2626" }}>{error}</div>
      )}

      <button
        onClick={handleSubmit}
        disabled={!canSubmit || loading}
        style={{
          padding: "8px 18px",
          borderRadius: "6px",
          border: "none",
          background: canSubmit && !loading ? "#a855f7" : "#d1d5db",
          color: canSubmit && !loading ? "#fff" : "#9ca3af",
          fontSize: "14px",
          fontWeight: 600,
          cursor: canSubmit && !loading ? "pointer" : "not-allowed",
        }}
      >
        {loading ? "Submitting…" : "Continue with this structure →"}
      </button>
    </div>
  );
}
