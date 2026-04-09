/**
 * PathwayChoicePanel — shown after the pathway stage when the run is paused
 * at "pathway_choice". Renders 1–4 target candidate cards and lets the user
 * select one before resuming the pipeline.
 */
import { useState } from "react";
import { api } from "../lib/api";
import type { TargetChoice } from "../lib/api";

interface Props {
  runId: number;
  choices: TargetChoice[];
  onResumed: () => void;
}

const TIER_STYLES: Record<string, { bg: string; text: string; label: string }> = {
  VALIDATED: { bg: "#dcfce7", text: "#166534", label: "Validated" },
  BIOLOGICALLY_JUSTIFIED: { bg: "#fef9c3", text: "#854d0e", label: "Biologically Justified" },
  PATHWAY_INFERRED: { bg: "#f3f4f6", text: "#374151", label: "Pathway Inferred" },
};

function tierStyle(tier: string) {
  return TIER_STYLES[tier] ?? { bg: "#f3f4f6", text: "#374151", label: tier };
}

export function PathwayChoicePanel({ runId, choices, onResumed }: Props) {
  const [selected, setSelected] = useState<number | null>(
    choices.length > 0 ? 0 : null
  );
  const [selectedPdb, setSelectedPdb] = useState<string | null>(
    choices[0]?.pdb_ids[0] ?? null
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedChoice = selected !== null ? choices[selected] : null;
  const canContinue =
    selected !== null &&
    selectedChoice != null &&
    selectedChoice.pdb_ids.length > 0 &&
    selectedPdb !== null;

  function selectTarget(idx: number) {
    const choice = choices[idx];
    if (!choice || choice.pdb_ids.length === 0) return;
    setSelected(idx);
    setSelectedPdb(choice.pdb_ids[0] ?? null);
  }

  async function handleContinue() {
    if (selected === null || selectedPdb === null) return;
    setLoading(true);
    setError(null);
    try {
      await api.runs.resume(runId, {
        chosen_target_index: selected,
        chosen_pdb_id: selectedPdb,
      });
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
      background: "#fffbeb",
      border: "1px solid #f59e0b",
      borderRadius: "8px",
    }}>
      <div style={{ marginBottom: "16px" }}>
        <h3 style={{ margin: "0 0 4px", fontSize: "15px", fontWeight: 600, color: "#92400e" }}>
          Choose a target to continue
        </h3>
        <p style={{ margin: 0, fontSize: "13px", color: "#78350f" }}>
          The pathway expert found the following candidates. Select one and choose which structure to analyze.
        </p>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: "10px", marginBottom: "16px" }}>
        {choices.map((choice) => {
          const ts = tierStyle(choice.tier);
          const isSelected = selected === choice.index;
          const noPdb = choice.pdb_ids.length === 0;
          return (
            <button
              key={choice.index}
              onClick={() => !noPdb && selectTarget(choice.index)}
              disabled={noPdb}
              style={{
                textAlign: "left",
                padding: "12px 14px",
                borderRadius: "6px",
                border: isSelected ? "2px solid #f59e0b" : "1px solid #e5e7eb",
                background: isSelected ? "#fef3c7" : "#fff",
                cursor: noPdb ? "not-allowed" : "pointer",
                opacity: noPdb ? 0.6 : 1,
                transition: "border-color 0.15s, background 0.15s",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "6px" }}>
                <span style={{
                  fontSize: "11px",
                  fontWeight: 600,
                  padding: "2px 7px",
                  borderRadius: "10px",
                  background: ts.bg,
                  color: ts.text,
                  whiteSpace: "nowrap",
                }}>
                  {ts.label}
                </span>
                <span style={{ fontSize: "14px", fontWeight: 600, color: "#111827" }}>
                  {choice.complex}
                </span>
                {choice.chain_ids_inferred && (
                  <span title="Chain assignments are auto-inferred — verify in the structure report before continuing"
                    style={{ fontSize: "12px", color: "#d97706", marginLeft: "4px" }}>
                    ⚠ Chain IDs inferred
                  </span>
                )}
              </div>

              <div style={{ fontSize: "12px", color: "#4b5563", marginBottom: "4px" }}>
                <strong>Evidence:</strong> {choice.evidence_basis || "—"}
              </div>
              <div style={{ fontSize: "12px", color: "#6b7280", marginBottom: "8px" }}>
                <strong>Uncertainty:</strong> {choice.key_uncertainty || "—"}
              </div>

              {/* PDB structure list */}
              {noPdb ? (
                <span style={{ fontSize: "11px", color: "#9ca3af", fontStyle: "italic" }}>
                  No PDB in corpus — cannot select
                </span>
              ) : isSelected ? (
                /* Selected card: show radio-selectable PDB rows */
                <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                  <div style={{ fontSize: "11px", color: "#78350f", fontWeight: 600, marginBottom: "2px" }}>
                    Choose structure:
                  </div>
                  {choice.pdb_ids.map((pdb) => {
                    const isPdbSelected = selectedPdb === pdb;
                    return (
                      <div
                        key={pdb}
                        onClick={(e) => { e.stopPropagation(); setSelectedPdb(pdb); }}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "8px",
                          padding: "5px 8px",
                          borderRadius: "5px",
                          background: isPdbSelected ? "#fde68a" : "#fffbeb",
                          border: isPdbSelected ? "1px solid #f59e0b" : "1px solid #fde68a",
                          cursor: "pointer",
                        }}
                      >
                        {/* Radio indicator */}
                        <div style={{
                          width: "14px",
                          height: "14px",
                          borderRadius: "50%",
                          border: isPdbSelected ? "4px solid #d97706" : "2px solid #d97706",
                          background: isPdbSelected ? "#f59e0b" : "transparent",
                          flexShrink: 0,
                          transition: "all 0.1s",
                        }} />
                        <span style={{
                          fontFamily: "monospace",
                          fontSize: "12px",
                          fontWeight: isPdbSelected ? 700 : 400,
                          color: "#0369a1",
                          flex: 1,
                        }}>
                          {pdb}
                        </span>
                        <a
                          href={`https://www.rcsb.org/structure/${pdb}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          title={`View ${pdb} on RCSB`}
                          style={{
                            fontSize: "12px",
                            color: "#6b7280",
                            textDecoration: "none",
                            padding: "1px 4px",
                            borderRadius: "3px",
                            lineHeight: 1,
                          }}
                        >
                          ↗
                        </a>
                      </div>
                    );
                  })}
                </div>
              ) : (
                /* Non-selected card: show PDB IDs as plain link badges */
                <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
                  {choice.pdb_ids.map((pdb) => (
                    <a
                      key={pdb}
                      href={`https://www.rcsb.org/structure/${pdb}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      onClick={(e) => e.stopPropagation()}
                      style={{
                        fontSize: "11px",
                        fontFamily: "monospace",
                        background: "#e0f2fe",
                        color: "#0369a1",
                        padding: "1px 6px",
                        borderRadius: "4px",
                        textDecoration: "none",
                      }}
                    >
                      {pdb} ↗
                    </a>
                  ))}
                </div>
              )}
            </button>
          );
        })}
      </div>

      {error && (
        <div style={{ marginBottom: "12px", fontSize: "13px", color: "#dc2626" }}>{error}</div>
      )}

      <button
        onClick={handleContinue}
        disabled={!canContinue || loading}
        style={{
          padding: "8px 18px",
          borderRadius: "6px",
          border: "none",
          background: canContinue && !loading ? "#f59e0b" : "#d1d5db",
          color: canContinue && !loading ? "#fff" : "#9ca3af",
          fontSize: "14px",
          fontWeight: 600,
          cursor: canContinue && !loading ? "pointer" : "not-allowed",
        }}
      >
        {loading ? "Starting…" : `Continue with ${selectedPdb ?? "selected structure"} →`}
      </button>
    </div>
  );
}
