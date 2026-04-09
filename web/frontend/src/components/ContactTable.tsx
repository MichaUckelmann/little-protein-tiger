/**
 * Hotspot residues contact table — shown alongside the Mol* structure viewer.
 * Parses run.hotspot_residues JSON and renders a compact table with a coloured
 * dot matching the orange hotspot highlight in the viewer.
 */

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
  hotspotResidues: string | null;
}

const HOTSPOT_ORANGE = "#E67E22";

export function ContactTable({ hotspotResidues }: Props) {
  let data: HotspotData | null = null;
  if (hotspotResidues) {
    try {
      data = JSON.parse(hotspotResidues) as HotspotData;
    } catch {
      // malformed JSON — show nothing
    }
  }

  if (!data || !data.residues.length) {
    return (
      <div style={wrapStyle}>
        <div style={headerStyle}>Hotspot Residues</div>
        <div style={{ padding: "16px", color: "#9ca3af", fontSize: 13 }}>
          No hotspot data yet
        </div>
      </div>
    );
  }

  return (
    <div style={wrapStyle}>
      <div style={headerStyle}>
        Hotspot Residues
        <span style={badgeStyle}>{data.residues.length}</span>
      </div>
      <div style={{ padding: "0 8px 8px" }}>
        <div style={{ fontSize: 11, color: "#6b7280", marginBottom: 6, paddingLeft: 4 }}>
          Target chain <strong>{data.target_chain}</strong>
          {data.partner_chain && (
            <> · Partner chain <strong>{data.partner_chain}</strong></>
          )}
        </div>
        <table style={tableStyle}>
          <thead>
            <tr>
              <th style={thStyle}>Residue</th>
              <th style={thStyle}>PDB #</th>
              <th style={thStyle}>RFD3 Atoms</th>
            </tr>
          </thead>
          <tbody>
            {data.residues.map((r, i) => (
              <tr key={i} style={i % 2 === 0 ? rowEvenStyle : rowOddStyle}>
                <td style={tdStyle}>
                  <span
                    style={{
                      display: "inline-block",
                      width: 8,
                      height: 8,
                      borderRadius: "50%",
                      background: HOTSPOT_ORANGE,
                      marginRight: 6,
                      flexShrink: 0,
                    }}
                  />
                  {r.residue}
                </td>
                <td style={{ ...tdStyle, fontVariantNumeric: "tabular-nums" }}>
                  {r.auth_seq_id}
                </td>
                <td style={{ ...tdStyle, fontFamily: "monospace", fontSize: 11 }}>
                  {r.rfd3_atoms}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const wrapStyle: React.CSSProperties = {
  background: "#fff",
  border: "1px solid #e5e7eb",
  borderRadius: 10,
  overflow: "hidden",
  height: "100%",
  display: "flex",
  flexDirection: "column",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "12px 16px",
  borderBottom: "1px solid #f3f4f6",
  fontWeight: 700,
  fontSize: 13,
  color: "#111827",
  background: "#fafafa",
  flexShrink: 0,
};

const badgeStyle: React.CSSProperties = {
  background: HOTSPOT_ORANGE,
  color: "#fff",
  borderRadius: 10,
  padding: "1px 7px",
  fontSize: 11,
  fontWeight: 700,
};

const tableStyle: React.CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: 12,
};

const thStyle: React.CSSProperties = {
  padding: "6px 8px",
  textAlign: "left",
  fontWeight: 600,
  color: "#6b7280",
  fontSize: 11,
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  borderBottom: "1px solid #f3f4f6",
};

const tdStyle: React.CSSProperties = {
  padding: "5px 8px",
  color: "#374151",
  verticalAlign: "middle",
  display: "table-cell",
};

const rowEvenStyle: React.CSSProperties = { background: "#fff" };
const rowOddStyle: React.CSSProperties = { background: "#fafafa" };
