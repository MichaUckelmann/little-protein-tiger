/**
 * GO / CONDITIONAL_GO / NO_GO banner shown on completed runs.
 */
interface Props {
  recommendation: string | null;
}

const CONFIG = {
  GO: { bg: "#f0fdf4", border: "#86efac", color: "#166534", label: "GO — Proceed to design" },
  CONDITIONAL_GO: {
    bg: "#fffbeb", border: "#fcd34d", color: "#92400e",
    label: "CONDITIONAL GO — Review caveats before proceeding",
  },
  NO_GO: { bg: "#fef2f2", border: "#fca5a5", color: "#991b1b", label: "NO GO — Target not recommended" },
};

export function GoRecommendation({ recommendation }: Props) {
  if (!recommendation) return null;
  const cfg = CONFIG[recommendation as keyof typeof CONFIG];
  if (!cfg) return null;

  return (
    <div style={{
      padding: "12px 16px",
      borderRadius: 8,
      border: `1px solid ${cfg.border}`,
      background: cfg.bg,
      color: cfg.color,
      fontWeight: 600,
      marginBottom: 20,
      fontSize: 15,
    }}>
      {cfg.label}
    </div>
  );
}
