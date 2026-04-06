import { BrowserRouter, Routes, Route, Navigate, Link } from "react-router-dom";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { api, getToken } from "./lib/api";
import Login from "./pages/Login";
import AuthCallback from "./pages/AuthCallback";
import Projects from "./pages/Projects";
import ProjectDetail from "./pages/ProjectDetail";
import RunDetail from "./pages/RunDetail";
import Settings from "./pages/Settings";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 30_000 } },
});

function RequireAuth({ children }: { children: React.ReactNode }) {
  if (!getToken()) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function NavBar() {
  const { data: user } = useQuery({
    queryKey: ["me"],
    queryFn: api.auth.me,
    enabled: !!getToken(),
  });

  return (
    <nav style={navStyles.bar}>
      <Link to="/" style={navStyles.brand}>LittleProteinTiger</Link>
      <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
        {user && !user.has_api_key && (
          <Link to="/settings" style={navStyles.warning}>⚠ Set API key</Link>
        )}
        {user && (
          <Link to="/settings" style={navStyles.link}>{user.github_login}</Link>
        )}
      </div>
    </nav>
  );
}

function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ minHeight: "100vh", background: "#f8fafc" }}>
      <NavBar />
      <main style={{ paddingTop: 64 }}>{children}</main>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/oauth" element={<AuthCallback />} />
          <Route
            path="/*"
            element={
              <RequireAuth>
                <AppLayout>
                  <Routes>
                    <Route path="/" element={<Projects />} />
                    <Route path="/projects/:id" element={<ProjectDetail />} />
                    <Route path="/runs/:id" element={<RunDetail />} />
                    <Route path="/settings" element={<Settings />} />
                  </Routes>
                </AppLayout>
              </RequireAuth>
            }
          />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

const navStyles: Record<string, React.CSSProperties> = {
  bar: {
    position: "fixed", top: 0, left: 0, right: 0, height: 52,
    background: "#18181b", display: "flex", alignItems: "center",
    justifyContent: "space-between", padding: "0 20px", zIndex: 100,
  },
  brand: {
    color: "#fff", fontWeight: 700, textDecoration: "none",
    fontSize: 15, fontFamily: "system-ui, sans-serif",
  },
  link: { color: "#d1d5db", textDecoration: "none", fontSize: 13, fontFamily: "system-ui, sans-serif" },
  warning: { color: "#fbbf24", textDecoration: "none", fontSize: 13, fontFamily: "system-ui, sans-serif" },
};
