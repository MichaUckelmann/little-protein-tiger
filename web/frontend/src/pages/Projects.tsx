import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";

export default function Projects() {
  const qc = useQueryClient();
  const { data: projects, isLoading } = useQuery({
    queryKey: ["projects"],
    queryFn: api.projects.list,
  });

  const [showNew, setShowNew] = useState(false);
  const [name, setName] = useState("");

  const create = useMutation({
    mutationFn: () => api.projects.create(name.trim()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setShowNew(false);
      setName("");
    },
  });

  if (isLoading) return <div style={styles.loading}>Loading…</div>;

  return (
    <div style={styles.page}>
      <div style={styles.header}>
        <h1 style={styles.h1}>Projects</h1>
        <button style={styles.btn} onClick={() => setShowNew(true)}>
          + New Project
        </button>
      </div>

      {showNew && (
        <div style={styles.modal}>
          <div style={styles.modalBox}>
            <h2 style={{ margin: "0 0 16px", fontSize: 18 }}>New Project</h2>
            <input
              autoFocus
              style={styles.input}
              placeholder="e.g. SCAP/SREBP NASH programme"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && name.trim() && create.mutate()}
            />
            <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
              <button
                style={styles.btn}
                disabled={!name.trim() || create.isPending}
                onClick={() => create.mutate()}
              >
                {create.isPending ? "Creating…" : "Create"}
              </button>
              <button style={styles.btnSecondary} onClick={() => setShowNew(false)}>
                Cancel
              </button>
            </div>
            {create.isError && (
              <p style={{ color: "#ef4444", marginTop: 8, fontSize: 13 }}>
                {(create.error as Error).message}
              </p>
            )}
          </div>
        </div>
      )}

      {projects?.length === 0 && (
        <p style={{ color: "#6b7280" }}>No projects yet. Create one to start a design run.</p>
      )}

      <div style={styles.grid}>
        {projects?.map((p) => (
          <Link key={p.id} to={`/projects/${p.id}`} style={styles.card}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>{p.name}</div>
            <div style={{ fontSize: 12, color: "#9ca3af" }}>
              {new Date(p.created_at).toLocaleDateString()}
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: 800, margin: "0 auto", padding: "32px 20px", fontFamily: "system-ui, sans-serif" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24 },
  h1: { margin: 0, fontSize: 24, fontWeight: 700 },
  loading: { padding: 40, fontFamily: "system-ui, sans-serif", color: "#6b7280" },
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 12 },
  card: {
    display: "block", padding: 16, border: "1px solid #e5e7eb", borderRadius: 8,
    textDecoration: "none", color: "inherit", background: "#fff",
    transition: "border-color .15s",
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
    borderRadius: 6, fontSize: 14, boxSizing: "border-box" as const,
  },
  modal: {
    position: "fixed" as const, inset: 0, background: "rgba(0,0,0,.4)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50,
  },
  modalBox: {
    background: "#fff", padding: 24, borderRadius: 10,
    width: 400, boxShadow: "0 8px 32px rgba(0,0,0,.15)",
  },
};
