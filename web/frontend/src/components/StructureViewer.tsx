/**
 * Mol* structure viewer — loads via the pre-built IIFE bundle (/molstar.js).
 *
 * Using the IIFE bundle instead of ESM imports sidesteps Vite 8 (rolldown)
 * chunk-splitting issues that cause module initialisation-order errors inside
 * Mol*'s PluginUIContext.  The bundle sets window.molstar and is fetched once
 * (cached via a singleton Promise); subsequent mounts reuse the cached bundle.
 *
 * Visualization scheme
 * ────────────────────
 * • Whole structure → "chain-id" cartoon (each chain gets a distinct color)
 * • Hotspot residues → focus ball-and-stick + selection highlight on top
 *
 * Blue + orange/amber is the safest colorblind pair (safe for deuteranopia,
 * protanopia, and tritanopia) and reads well on both dark and light backgrounds.
 *
 * Implementation notes
 * ────────────────────
 * • `Script` and `StructureSelection` are imported from the molstar ESM
 *   package (pre-optimised in `.vite/deps`).  They are pure utilities with
 *   no global singleton state — safe alongside the IIFE bundle.
 * • Hotspot highlighting uses the documented Loci pattern:
 *     1. `Script.getStructureSelection(Q => …, data)` compiles a MolQL
 *        callback against the loaded Structure object.
 *     2. `StructureSelection.toLociWithSourceUnits(selection)` converts it
 *        to a Loci.
 *     3. `plugin.managers.interactivity.lociSelects.select({ loci })`
 *        applies the persistent selection highlight (green glow).
 *     4. `plugin.managers.structure.focus.setFromLoci(loci)` shows the
 *        residues in ball-and-stick with nearby non-covalent interactions.
 */
import { useEffect, useRef, useState } from "react";
import { getToken } from "../lib/api";
// These two are already Vite-pre-optimised (present in node_modules/.vite/deps)
// and are pure utilities — safe alongside the IIFE bundle.
import { Script } from "molstar/lib/mol-script/script";
import { StructureSelection } from "molstar/lib/mol-model/structure";

// ── Types ─────────────────────────────────────────────────────────────────────

interface HotspotResidue {
  residue: string;
  auth_seq_id: number;
  label_seq_id: number;
  rfd3_atoms: string;
}

interface HotspotData {
  target_chain: string;
  partner_chain: string;
  residues: HotspotResidue[];
}

interface Props {
  pdbId: string;
  hotspotResidues: string | null;
}

// ── IIFE bundle loader ────────────────────────────────────────────────────────

declare global {
  interface Window {
    molstar?: any;
  }
}

let _molstarPromise: Promise<void> | null = null;

function loadMolstarBundle(): Promise<void> {
  if (_molstarPromise) return _molstarPromise;
  _molstarPromise = new Promise<void>((resolve, reject) => {
    if (window.molstar) { resolve(); return; }

    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "/molstar.css";
    document.head.appendChild(link);

    const script = document.createElement("script");
    script.src = "/molstar.js";
    script.onload = () => {
      if (window.molstar) resolve();
      else reject(new Error("molstar.js loaded but window.molstar is undefined"));
    };
    script.onerror = () => reject(new Error("Failed to load /molstar.js"));
    document.head.appendChild(script);
  });
  return _molstarPromise;
}

// ── Visualization helpers ─────────────────────────────────────────────────────

// Mol* built-in color theme used for the whole-structure cartoon.
// "chain-id" assigns each chain a distinct color automatically — the simplest
// way to visually separate chain A (target) from chain B (partner) without
// building per-chain selections.
const CHAIN_COLOR_THEME = "chain-id";

/**
 * Recolor all existing representations to steel-blue and, if hotspot data is
 * provided, overlay hotspot residues using the documented Loci-based APIs:
 *   • lociSelects.select  → persistent selection highlight
 *   • focus.setFromLoci   → ball-and-stick focus representation
 *
 * The Loci is built via Script.getStructureSelection (documented pattern),
 * which compiles a MolQL callback against the actual Structure data object.
 */
async function applyStructureVisualization(
  viewer: any,
  hotspotResiduesStr: string | null
): Promise<void> {
  const plugin = viewer.plugin;
  const structures = plugin.managers?.structure?.hierarchy?.current?.structures;
  if (!structures?.length) return;

  const struct = structures[0];

  // ── 1. Recolor whole structure to steel-blue ─────────────────────────────

  try {
    await plugin.managers.structure.component.updateRepresentationsTheme(
      struct.components,
      { color: CHAIN_COLOR_THEME }
    );
  } catch (e) {
    console.warn("[StructureViewer] updateRepresentationsTheme failed:", e);
  }

  // ── 2. Hotspot residues ───────────────────────────────────────────────────

  if (!hotspotResiduesStr) return;

  let hd: HotspotData | null = null;
  try { hd = JSON.parse(hotspotResiduesStr) as HotspotData; } catch { return; }
  if (!hd?.residues?.length) return;

  const targetChain: string = hd.target_chain ?? "";   // may be "" in older runs
  const residueNums = hd.residues.map((r) => r.auth_seq_id);

  // Get the raw Structure data object (needed by Script.getStructureSelection)
  const data = struct.cell?.obj?.data;
  if (!data) return;

  let loci: any;
  try {
    const selection = Script.getStructureSelection(
      (Q) => {
        // Build residue test — match any of the hotspot auth_seq_ids
        const resTest =
          residueNums.length === 1
            ? Q.core.rel.eq([Q.ammp("auth_seq_id"), residueNums[0]])
            : Q.core.logic.or(
                residueNums.map((n: number) =>
                  Q.core.rel.eq([Q.ammp("auth_seq_id"), n])
                )
              );
        // Only add chain-test when target_chain is known — older runs stored
        // target_chain as "" so we fall back to residue-number-only matching
        const args: Record<string, any> = { "residue-test": resTest };
        if (targetChain) {
          args["chain-test"] = Q.core.rel.eq([Q.ammp("auth_asym_id"), targetChain]);
        }
        return Q.struct.generator.atomGroups(args);
      },
      data
    );
    loci = StructureSelection.toLociWithSourceUnits(selection);
  } catch (e) {
    console.warn("[StructureViewer] Script.getStructureSelection failed:", e);
    return;
  }

  try {
    plugin.managers.interactivity.lociSelects.select({ loci });
  } catch (e) {
    console.warn("[StructureViewer] lociSelects.select failed:", e);
  }

  try {
    plugin.managers.structure.focus.setFromLoci(loci);
  } catch (e) {
    console.warn("[StructureViewer] focus.setFromLoci failed:", e);
  }
}

// ── Component ─────────────────────────────────────────────────────────────────

export function StructureViewer({ pdbId, hotspotResidues }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!containerRef.current) return;

    if (viewerRef.current) {
      try { viewerRef.current.plugin.dispose(); } catch { /* ignore */ }
      viewerRef.current = null;
    }
    containerRef.current.innerHTML = "";

    let cancelled = false;

    (async () => {
      try {
        setLoading(true);
        setError(null);

        await loadMolstarBundle();
        if (cancelled) return;

        const viewer = await window.molstar!.Viewer.create(containerRef.current!, {
          layoutIsExpanded: false,
          layoutShowControls: false,
          layoutShowRemoteState: false,
          layoutShowSequence: false,
          layoutShowLog: false,
          layoutShowLeftPanel: false,
          viewportShowExpand: false,
          viewportShowSelectionMode: false,
          viewportShowAnimation: false,
        });

        if (cancelled) {
          viewer.plugin.dispose();
          return;
        }
        viewerRef.current = viewer;

        const token = getToken();
        const res = await fetch(`/structures/${pdbId}.cif`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (cancelled) return;
        if (!res.ok) {
          throw new Error(
            `Structure not available (${res.status}). It may need to be downloaded first.`
          );
        }
        const cifText = await res.text();
        if (cancelled) return;

        await viewer.loadStructureFromData(cifText, "mmcif", {
          dataLabel: pdbId,
        });
        if (cancelled) return;

        await applyStructureVisualization(viewer, hotspotResidues);

        if (!cancelled) setLoading(false);
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load structure");
          setLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [pdbId]);

  useEffect(() => {
    return () => {
      try { viewerRef.current?.plugin.dispose(); } catch { /* ignore */ }
      viewerRef.current = null;
    };
  }, []);

  return (
    <div
      style={{
        position: "relative",
        width: "100%",
        height: 450,
        background: "#f8fafc",
        border: "1px solid #e5e7eb",
        borderRadius: 10,
        overflow: "hidden",
      }}
    >
      <div ref={containerRef} style={{ width: "100%", height: "100%" }} />

      {loading && !error && (
        <div style={overlayStyle}>
          <span style={{ color: "#6b7280", fontSize: 13 }}>
            Loading structure…
          </span>
        </div>
      )}

      {error && (
        <div style={overlayStyle}>
          <span
            style={{
              color: "#dc2626",
              fontSize: 13,
              textAlign: "center",
              maxWidth: 280,
            }}
          >
            {error}
          </span>
        </div>
      )}
    </div>
  );
}

const overlayStyle: React.CSSProperties = {
  position: "absolute",
  inset: 0,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: "#f8fafc",
  pointerEvents: "none",
};
