// OAuth redirects must go directly to the backend — never through the Vite proxy,
// since the flow involves external GitHub redirects that bypass the dev server.
const BACKEND = import.meta.env.VITE_BACKEND_URL ?? "http://localhost:8000";

export default function Login() {
  return (
    <div style={{
      minHeight: "100vh",
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      background: "#f8fafc",
      fontFamily: "system-ui, sans-serif",
    }}>
      <div style={{
        background: "#fff",
        padding: "48px 40px",
        borderRadius: 12,
        border: "1px solid #e5e7eb",
        boxShadow: "0 1px 4px rgba(0,0,0,.06)",
        textAlign: "center",
        maxWidth: 360,
        width: "100%",
      }}>
        <h1 style={{ margin: "0 0 8px", fontSize: 22, fontWeight: 700 }}>
          LittleProteinTiger
        </h1>
        <p style={{ margin: "0 0 32px", color: "#6b7280", fontSize: 14 }}>
          AI-driven protein binder design pipeline
        </p>
        <a
          href={`${BACKEND}/auth/github`}
          style={{
            display: "inline-block",
            padding: "10px 24px",
            background: "#18181b",
            color: "#fff",
            borderRadius: 8,
            textDecoration: "none",
            fontWeight: 600,
            fontSize: 14,
          }}
        >
          Sign in with GitHub
        </a>
      </div>
    </div>
  );
}
