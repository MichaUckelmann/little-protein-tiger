/**
 * Landing page after GitHub OAuth redirect.
 * Pulls the JWT token from the URL query param and stores it.
 */
import { useEffect } from "react";
import { setToken } from "../lib/api";

export default function AuthCallback() {
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const token = params.get("token");
    if (token) {
      setToken(token);
      // Hard redirect so RequireAuth reads the freshly stored token
      window.location.replace("/");
    } else {
      window.location.replace("/login");
    }
  }, []);

  return (
    <div style={{ padding: 40, fontFamily: "system-ui, sans-serif" }}>
      Signing in…
    </div>
  );
}
