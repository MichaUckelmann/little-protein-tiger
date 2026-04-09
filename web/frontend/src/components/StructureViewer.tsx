/**
 * Mol* structure viewer — loads via the pre-built IIFE bundle (/molstar.js).
 *
 * Using the IIFE bundle instead of ESM imports sidesteps Vite 8 (rolldown)
 * chunk-splitting issues that cause module initialisation-order errors inside
 * Mol*'s PluginUIContext.  The bundle sets window.molstar and is fetched once
 * (cached via a singleton Promise); subsequent mounts reuse the cached bundle.
 *
 * Hotspot residues are persistently selected via structureInteractivity so they
 * are visually distinguished from the rest of the structure.
 */
import { useEffect, useRef, useState } from "react";
import { getToken } from "../lib/api";

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

// ── Component ─────────────────────────────────────────────────────────────────

export function StructureViewer({ pdbId, hotspotResidues }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!containerRef.current) return;

    // Dispose any viewer from the previous effect run (handles React strict-mode
    // double-invoke: cleanup fires before the next run starts).
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

        // Fetch the CIF with auth header
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

        // Highlight hotspot residues via persistent selection
        if (hotspotResidues) {
          let hd: HotspotData | null = null;
          try { hd = JSON.parse(hotspotResidues) as HotspotData; } catch { /* ignore */ }

          if (hd?.target_chain && hd.residues.length) {
            const targetChain = hd.target_chain;
            const residueNums = hd.residues.map((r) => r.auth_seq_id);

            // structureInteractivity passes the MolScriptBuilder as its first
            // argument so we never need a separate ESM import.
            viewer.structureInteractivity({
              action: "select",
              expression: (MS: any) => {
                const chainTest = MS.core.rel.eq([
                  MS.ammp("auth_asym_id"),
                  targetChain,
                ]);
                const resTest =
                  residueNums.length === 1
                    ? MS.core.rel.eq([MS.ammp("auth_seq_id"), residueNums[0]])
                    : MS.core.logic.or(
                        residueNums.map((n: number) =>
                          MS.core.rel.eq([MS.ammp("auth_seq_id"), n])
                        )
                      );
                return MS.struct.generator.atomGroups({
                  "chain-test": chainTest,
                  "residue-test": resTest,
                });
              },
            });
          }
        }

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
      // Viewer disposal is handled at the START of the next run so we don't
      // race with in-flight Mol* React renders on this container.
    };
  }, [pdbId]); // hotspotResidues excluded — applied once on initial load

  // Final cleanup when the component fully unmounts
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
