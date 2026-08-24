/**
 * Landing page after GitHub OAuth redirect.
 * Pulls the short-lived, single-use exchange code from the URL query param
 * and trades it for the real JWT via POST — keeps the JWT out of the URL
 * (browser history, Referer headers, proxy/access logs).
 */
import { useEffect } from "react";
import { api, setToken } from "../lib/api";

export default function AuthCallback() {
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    if (!code) {
      window.location.replace("/login");
      return;
    }
    api.auth
      .exchangeCode(code)
      .then(({ access_token }) => {
        setToken(access_token);
        // Hard redirect so RequireAuth reads the freshly stored token
        window.location.replace("/");
      })
      .catch(() => window.location.replace("/login"));
  }, []);

  return (
    <div style={{ padding: 40, fontFamily: "system-ui, sans-serif" }}>
      Signing in…
    </div>
  );
}
