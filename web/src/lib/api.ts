function getToken(): string {
  const w = window as any;
  return w.__MARKBOT_SESSION_TOKEN__ ?? "";
}

async function request<T>(path: string, opts?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-Markbot-Session-Token": token,
      ...opts?.headers,
    },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

export const api = {
  getSessions: (params?: { limit?: number; offset?: number }) =>
    request<{ sessions: any[] }>(`/api/sessions?${new URLSearchParams(params as any)}`),

  getSession: (id: string) => request<any>(`/api/sessions/${id}`),

  deleteSession: (id: string) =>
    request<{ ok: boolean }>(`/api/sessions/${id}`, { method: "DELETE" }),

  patchSession: (id: string, data: { title?: string }) =>
    request<{ ok: boolean }>(`/api/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  searchSessions: (q: string) =>
    request<{ sessions: any[] }>(`/api/sessions/search?q=${encodeURIComponent(q)}`),

  getSessionStats: () =>
    request<{ total: number; active: number; messages: number }>("/api/sessions/stats"),

  bulkDeleteSessions: (ids: string[]) =>
    request<{ ok: boolean; deleted: number }>("/api/sessions/bulk-delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),

  exportSession: (id: string, format: "markdown" | "json" = "markdown") => {
    const token = getToken();
    return fetch(`/api/sessions/${id}/export?format=${format}`, {
      headers: { "X-Markbot-Session-Token": token },
    });
  },

  getConfig: () => request<any>("/api/config"),
  getRawConfig: () => request<{ raw: string }>("/api/config/raw"),
  saveConfig: (data: any) =>
    request<{ ok: boolean }>("/api/config", {
      method: "PUT",
      body: JSON.stringify({ config: typeof data === "string" ? data : data }),
    }),

  getEnv: () => request<{ env: any[] }>("/api/env"),
  setEnv: (key: string, value: string) =>
    request<{ ok: boolean }>("/api/env", {
      method: "PUT",
      body: JSON.stringify({ key, value }),
    }),
  deleteEnv: (key: string) =>
    request<{ ok: boolean }>(`/api/env?key=${encodeURIComponent(key)}`, { method: "DELETE" }),
  revealEnv: (key: string) =>
    request<{ key: string; value: string }>("/api/env/reveal", {
      method: "POST",
      body: JSON.stringify({ key }),
    }),

  getModelInfo: () => request<any>("/api/model/info"),
  getModelOptions: () => request<any>("/api/model/options"),
  setModel: (provider: string, model: string) =>
    request<{ ok: boolean }>("/api/model/set", {
      method: "POST",
      body: JSON.stringify({ provider, model }),
    }),

  getLogs: (params?: { file?: string; lines?: number; level?: string; component?: string }) =>
    request<{ logs: string[]; file: string; path: string }>(
      `/api/logs?${new URLSearchParams(params as any)}`,
    ),
  getLogFiles: () => request<{ files: string[] }>("/api/logs/files"),

  getSkills: () => request<{ skills: any[] }>("/api/skills"),
  toggleSkill: (name: string, enabled: boolean) =>
    request<{ ok: boolean }>("/api/skills/toggle", {
      method: "PUT",
      body: JSON.stringify({ name, enabled }),
    }),

  getCronJobs: () => request<{ jobs: any[] }>("/api/cron/jobs"),
  createCronJob: (data: any) =>
    request<{ ok: boolean; id: string }>("/api/cron/jobs", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  updateCronJob: (id: string, data: any) =>
    request<{ ok: boolean }>(`/api/cron/jobs/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  deleteCronJob: (id: string) =>
    request<{ ok: boolean }>(`/api/cron/jobs/${id}`, { method: "DELETE" }),
  controlCronJob: (id: string, action: "pause" | "resume" | "trigger") =>
    request<{ ok: boolean }>(`/api/cron/jobs/${id}/${action}`, { method: "POST" }),

  getChannels: () => request<{ channels: any[] }>("/api/channels"),
  testChannel: (id: string) =>
    request<{ ok: boolean }>(`/api/channels/${id}/test`, { method: "POST" }),

  getSystemStats: () => request<any>("/api/system/stats"),

  getMcpServers: () => request<{ servers: any[] }>("/api/mcp/servers"),
  addMcpServer: (data: any) =>
    request<{ ok: boolean }>("/api/mcp/servers", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  removeMcpServer: (name: string) =>
    request<{ ok: boolean }>(`/api/mcp/servers/${encodeURIComponent(name)}`, { method: "DELETE" }),
  testMcpServer: (name: string) =>
    request<{ ok: boolean }>(`/api/mcp/servers/${encodeURIComponent(name)}/test`, {
      method: "POST",
    }),
  toggleMcpServer: (name: string, enabled: boolean) =>
    request<{ ok: boolean }>(`/api/mcp/servers/${encodeURIComponent(name)}/enabled`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
};
