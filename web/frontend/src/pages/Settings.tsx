/**
 * Account settings — BYOK API key management.
 */
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { clearToken } from "../lib/api";
import { useNavigate } from "react-router-dom";

export default function Settings() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { data: user } = useQuery({ queryKey: ["me"], queryFn: api.auth.me });

  const [apiKey, setApiKey] = useState("");
  const [saved, setSaved] = useState(false);
  const [geminiKey, setGeminiKey] = useState("");
  const [geminiSaved, setGeminiSaved] = useState(false);

  const setKey = useMutation({
    mutationFn: () => api.auth.setApiKey(apiKey.trim()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["me"] });
      setApiKey("");
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    },
  });

  const deleteKey = useMutation({
    mutationFn: api.auth.deleteApiKey,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });

  const setGKey = useMutation({
    mutationFn: () => api.auth.setGeminiKey(geminiKey.trim()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["me"] });
      setGeminiKey("");
      setGeminiSaved(true);
      setTimeout(() => setGeminiSaved(false), 3000);
    },
  });

  const deleteGKey = useMutation({
    mutationFn: api.auth.deleteGeminiKey,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });

  const logout = () => {
    clearToken();
    navigate("/login");
  };

  return (
    <div style={styles.page}>
      <h1 style={styles.h1}>Account Settings</h1>

      {user && (
        <div style={styles.section}>
          <div style={{ fontSize: 14, color: "#374151" }}>
            Signed in as <strong>{user.github_login}</strong>
            {user.email && ` (${user.email})`}
          </div>
          <button style={styles.btnDanger} onClick={logout}>Sign out</button>
        </div>
      )}

      <div style={styles.section}>
        <h2 style={styles.h2}>Anthropic API Key (BYOK)</h2>
        <p style={styles.hint}>
          Your key is encrypted at rest (AES-256) and never logged. It's used only to call
          the Anthropic API on your behalf during pipeline runs.
        </p>

        {user?.has_api_key ? (
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 13, color: "#22c55e", fontWeight: 600 }}>
              ✓ API key stored
            </span>
            <button
              style={styles.btnSecondary}
              onClick={() => deleteKey.mutate()}
              disabled={deleteKey.isPending}
            >
              Remove key
            </button>
          </div>
        ) : (
          <div>
            <input
              type="password"
              style={styles.input}
              placeholder="sk-ant-api03-…"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <button
                style={styles.btn}
                disabled={!apiKey.trim() || setKey.isPending}
                onClick={() => setKey.mutate()}
              >
                {setKey.isPending ? "Saving…" : "Save Key"}
              </button>
            </div>
            {saved && <p style={{ color: "#22c55e", fontSize: 13, marginTop: 6 }}>Key saved.</p>}
            {setKey.isError && (
              <p style={{ color: "#ef4444", fontSize: 13, marginTop: 6 }}>
                {(setKey.error as Error).message}
              </p>
            )}
          </div>
        )}
      </div>

      <div style={styles.section}>
        <h2 style={styles.h2}>Google Gemini API Key (BYOK)</h2>
        <p style={styles.hint}>
          Required to run pipeline stages with Gemini Flash Lite. Encrypted at rest (AES-256).
        </p>

        {user?.has_gemini_key ? (
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 13, color: "#22c55e", fontWeight: 600 }}>
              ✓ Gemini key stored
            </span>
            <button
              style={styles.btnSecondary}
              onClick={() => deleteGKey.mutate()}
              disabled={deleteGKey.isPending}
            >
              Remove key
            </button>
          </div>
        ) : (
          <div>
            <input
              type="password"
              style={styles.input}
              placeholder="AIza…"
              value={geminiKey}
              onChange={(e) => setGeminiKey(e.target.value)}
            />
            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <button
                style={styles.btn}
                disabled={!geminiKey.trim() || setGKey.isPending}
                onClick={() => setGKey.mutate()}
              >
                {setGKey.isPending ? "Saving…" : "Save Key"}
              </button>
            </div>
            {geminiSaved && <p style={{ color: "#22c55e", fontSize: 13, marginTop: 6 }}>Key saved.</p>}
            {setGKey.isError && (
              <p style={{ color: "#ef4444", fontSize: 13, marginTop: 6 }}>
                {(setGKey.error as Error).message}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  page: { maxWidth: 600, margin: "0 auto", padding: "32px 20px", fontFamily: "system-ui, sans-serif" },
  h1: { margin: "0 0 24px", fontSize: 22, fontWeight: 700 },
  h2: { margin: "0 0 8px", fontSize: 16, fontWeight: 600 },
  section: {
    padding: 20, border: "1px solid #e5e7eb", borderRadius: 8,
    marginBottom: 16, background: "#fff", display: "flex",
    flexDirection: "column" as const, gap: 12,
  },
  hint: { margin: 0, fontSize: 13, color: "#6b7280", lineHeight: 1.5 },
  input: {
    width: "100%", padding: "8px 12px", border: "1px solid #d1d5db",
    borderRadius: 6, fontSize: 14, boxSizing: "border-box" as const,
  },
  btn: {
    padding: "8px 16px", background: "#18181b", color: "#fff",
    border: "none", borderRadius: 6, cursor: "pointer", fontWeight: 600, fontSize: 14,
  },
  btnSecondary: {
    padding: "6px 12px", background: "#f3f4f6", color: "#374151",
    border: "1px solid #d1d5db", borderRadius: 6, cursor: "pointer", fontSize: 13,
  },
  btnDanger: {
    padding: "6px 12px", background: "#fff", color: "#ef4444",
    border: "1px solid #fca5a5", borderRadius: 6, cursor: "pointer", fontSize: 13,
    alignSelf: "flex-start" as const,
  },
};
