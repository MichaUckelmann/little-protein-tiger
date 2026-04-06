/**
 * Typed API client for the LittleProteinTiger backend.
 * All requests include the JWT token from localStorage.
 */

// Empty string = use Vite dev proxy (same origin); set VITE_API_URL for production
const BASE = import.meta.env.VITE_API_URL ?? "";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface User {
  id: number;
  github_login: string;
  email: string | null;
  has_api_key: boolean;
  has_gemini_key: boolean;
  created_at: string;
}

export interface Project {
  id: number;
  slug: string;
  name: string;
  owner_id: number;
  created_at: string;
}

export interface Run {
  id: number;
  project_id: number;
  user_id: number;
  query: string;
  provider: string;
  model_id: string | null;
  status: "QUEUED" | "RUNNING" | "COMPLETE" | "FAILED" | "BLOCKED";
  stage_current: string | null;
  pdb_id: string | null;
  target_complex: string | null;
  go_recommendation: string | null;
  hotspot_residues: string | null;
  celery_task_id: string | null;
  parent_run_id: number | null;
  round_number: number;
  input_structure_path: string | null;
  created_at: string;
  completed_at: string | null;
  error: string | null;
}

export interface RunDetail {
  run: Run;
  stages: Record<string, string>; // filename → markdown content
}

export interface Measurement {
  id: number;
  run_id: number;
  design_name: string;
  method: string;
  kd_molar: number | null;
  ki_molar: number | null;
  notes: string | null;
  measured_at: string;
  uploaded_by: number;
}

// ---------------------------------------------------------------------------
// Auth token helpers
// ---------------------------------------------------------------------------

export const getToken = (): string | null => localStorage.getItem("token");
export const setToken = (t: string) => localStorage.setItem("token", t);
export const clearToken = () => localStorage.removeItem("token");

// ---------------------------------------------------------------------------
// Core fetch wrapper
// ---------------------------------------------------------------------------

async function apiFetch<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = getToken();
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export const api = {
  auth: {
    me: () => apiFetch<User>("/auth/me"),
    logout: () => apiFetch<void>("/auth/logout", { method: "POST" }),
    setApiKey: (key: string) =>
      apiFetch<void>("/auth/me/api-key", {
        method: "PUT",
        body: JSON.stringify({ anthropic_api_key: key }),
      }),
    deleteApiKey: () =>
      apiFetch<void>("/auth/me/api-key", { method: "DELETE" }),
    setGeminiKey: (key: string) =>
      apiFetch<void>("/auth/me/gemini-key", {
        method: "PUT",
        body: JSON.stringify({ gemini_api_key: key }),
      }),
    deleteGeminiKey: () =>
      apiFetch<void>("/auth/me/gemini-key", { method: "DELETE" }),
  },

  // -------------------------------------------------------------------------
  // Projects
  // -------------------------------------------------------------------------
  projects: {
    list: () => apiFetch<Project[]>("/projects"),
    create: (name: string) =>
      apiFetch<Project>("/projects", {
        method: "POST",
        body: JSON.stringify({ name }),
      }),
    get: (id: number) =>
      apiFetch<{ project: Project; runs: Run[] }>(`/projects/${id}`),
    delete: (id: number) =>
      apiFetch<void>(`/projects/${id}`, { method: "DELETE" }),
  },

  // -------------------------------------------------------------------------
  // Runs
  // -------------------------------------------------------------------------
  runs: {
    create: (
      projectId: number,
      payload: { query: string; pdb_id?: string; provider?: string; model_id?: string }
    ) =>
      apiFetch<Run>(`/projects/${projectId}/runs`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    get: (id: number) => apiFetch<RunDetail>(`/runs/${id}`),
    children: (id: number) => apiFetch<Run[]>(`/runs/${id}/children`),
    measurements: (id: number) =>
      apiFetch<Measurement[]>(`/runs/${id}/measurements`),
    addMeasurement: (
      id: number,
      payload: {
        design_name: string;
        method: string;
        kd_molar?: number;
        ki_molar?: number;
        notes?: string;
      }
    ) =>
      apiFetch<Measurement>(`/runs/${id}/measurements`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    fileUrl: (id: number, filename: string) =>
      `${BASE}/runs/${id}/files/${filename}`,
  },
};
