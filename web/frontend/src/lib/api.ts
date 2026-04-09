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
  stage_models_json: string | null;
  extended_thinking: boolean;
  status: "QUEUED" | "RUNNING" | "COMPLETE" | "FAILED" | "BLOCKED" | "PAUSED";
  stage_current: string | null;
  pdb_id: string | null;
  target_complex: string | null;
  go_recommendation: string | null;
  hotspot_residues: string | null;
  auto_mode: boolean;
  pause_point: string | null;
  pathway_choices_json: string | null;
  structure_next_step: string | null;
  celery_task_id: string | null;
  parent_run_id: number | null;
  round_number: number;
  input_structure_path: string | null;
  created_at: string;
  completed_at: string | null;
  error: string | null;
}

export interface TargetChoice {
  index: number;
  tier: string; // VALIDATED | BIOLOGICALLY_JUSTIFIED | PATHWAY_INFERRED
  complex: string;
  pdb_ids: string[];
  evidence_basis: string;
  key_uncertainty: string;
  structure_query: string;
  chain_ids_inferred: boolean;
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

export interface BinderMeasurement {
  id: number;
  binder_id: number;
  method: string;
  kd_molar: number | null;
  ki_molar: number | null;
  notes: string | null;
  measured_at: string;
  uploaded_by: number;
}

export interface Binder {
  id: number;
  campaign_id: number;
  parent_id: number | null;
  name: string;
  sequence: string;
  mutation_label: string | null;
  source: "csv_import" | "optimizer" | "manual";
  design_to_target_iptm: number | null;
  min_design_to_target_pae: number | null;
  filter_rmsd: number | null;
  cif_path: string | null;
  notes: string | null;
  optimizer_report_path: string | null;
  created_at: string;
  measurements: BinderMeasurement[];
}

export interface BinderCampaign {
  id: number;
  project_id: number;
  user_id: number;
  name: string;
  target_name: string | null;
  csv_filename: string | null;
  created_at: string;
  binders?: Binder[];
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
    if (res.status === 401) {
      clearToken();
      window.location.replace("/login");
      throw new Error("Session expired");
    }
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
      payload: {
        query: string;
        pdb_id?: string;
        provider?: string;
        model_id?: string;
        stage_models?: Record<string, string>;
        extended_thinking?: boolean;
        auto_mode?: boolean;
      }
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
    resume: (
      id: number,
      payload: {
        chosen_target_index?: number;
        chosen_pdb_id?: string;
        next_step?: string;
        pdb_id?: string;
      }
    ) =>
      apiFetch<Run>(`/runs/${id}/resume`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    uploadStructure: (id: number, file: File): Promise<Run> => {
      const token = getToken();
      const fd = new FormData();
      fd.append("file", file);
      return fetch(`${BASE}/runs/${id}/upload-structure`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: fd,
      }).then(async (res) => {
        if (!res.ok) {
          if (res.status === 401) { clearToken(); window.location.replace("/login"); throw new Error("Session expired"); }
          const detail = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(detail.detail ?? res.statusText);
        }
        return res.json();
      });
    },
    retry: (id: number) =>
      apiFetch<Run>(`/runs/${id}/retry`, { method: "POST" }),
    delete: (id: number) =>
      apiFetch<void>(`/runs/${id}`, { method: "DELETE" }),
    fileUrl: (id: number, filename: string) =>
      `${BASE}/runs/${id}/files/${filename}`,
  },

  // -------------------------------------------------------------------------
  // Binder campaigns
  // -------------------------------------------------------------------------
  campaigns: {
    list: (projectId: number) =>
      apiFetch<BinderCampaign[]>(`/projects/${projectId}/campaigns`),
    create: (projectId: number, payload: { name: string; target_name?: string }) =>
      apiFetch<BinderCampaign>(`/projects/${projectId}/campaigns`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    get: (id: number) =>
      apiFetch<BinderCampaign>(`/campaigns/${id}`),
    uploadCsv: (id: number, file: File): Promise<{ created: number }> => {
      const token = getToken();
      const fd = new FormData();
      fd.append("file", file);
      return fetch(`${BASE}/campaigns/${id}/upload-csv`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: fd,
      }).then(async (res) => {
        if (!res.ok) {
          if (res.status === 401) { clearToken(); window.location.replace("/login"); throw new Error("Session expired"); }
          const detail = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(detail.detail ?? res.statusText);
        }
        return res.json();
      });
    },
  },

  // -------------------------------------------------------------------------
  // Binders
  // -------------------------------------------------------------------------
  binders: {
    uploadCif: (binderId: number, file: File): Promise<{ cif_path: string }> => {
      const token = getToken();
      const fd = new FormData();
      fd.append("file", file);
      return fetch(`${BASE}/binders/${binderId}/upload-cif`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: fd,
      }).then(async (res) => {
        if (!res.ok) {
          if (res.status === 401) { clearToken(); window.location.replace("/login"); throw new Error("Session expired"); }
          const detail = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(detail.detail ?? res.statusText);
        }
        return res.json();
      });
    },
    cifUrl: (binderId: number) => `${BASE}/binders/${binderId}/cif`,
    optimizerReportUrl: (binderId: number) => `${BASE}/binders/${binderId}/optimizer-report`,
    addMeasurement: (
      binderId: number,
      payload: { method: string; kd_molar?: number; ki_molar?: number; notes?: string }
    ) =>
      apiFetch<BinderMeasurement>(`/binders/${binderId}/measurements`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    deleteMeasurement: (binderId: number, measurementId: number) =>
      apiFetch<void>(`/binders/${binderId}/measurements/${measurementId}`, {
        method: "DELETE",
      }),
    runOptimizer: (binderId: number) =>
      apiFetch<{ status: string }>(`/binders/${binderId}/run-optimizer`, {
        method: "POST",
      }),
    addMutation: (
      binderId: number,
      payload: { sequence: string; mutation_label?: string; name?: string; notes?: string }
    ) =>
      apiFetch<Binder>(`/binders/${binderId}/add-mutation`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
  },
};
