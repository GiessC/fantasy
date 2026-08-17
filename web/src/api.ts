// Typed HTTP client.
//
// The backend returns structured errors ({error, detail, hint}) for anything a
// user can cause, so this surfaces `detail` and `hint` rather than a status
// code — during a draft, "sync data first" beats "409".

import type {
  Answer,
  ApiInfo,
  Board,
  DataStatus,
  DraftStatus,
  LLMStatus,
  PickResult,
  PlayerDetail,
  Recommendation,
  SearchResult,
} from "./types";

export class RequestError extends Error {
  readonly status: number;
  readonly hint: string | null;
  readonly kind: string;

  constructor(message: string, status: number, hint: string | null, kind: string) {
    super(message);
    this.name = "RequestError";
    this.status = status;
    this.hint = hint;
    this.kind = kind;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch (cause) {
    throw new RequestError(
      "Cannot reach the local server. Is `fantasy-ai serve` still running?",
      0,
      null,
      "NetworkError",
    );
  }

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}.`;
    let hint: string | null = null;
    let kind = "HTTPError";
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else if (Array.isArray(body?.detail)) detail = JSON.stringify(body.detail);
      if (typeof body?.hint === "string") hint = body.hint;
      if (typeof body?.error === "string") kind = body.error;
    } catch {
      // Non-JSON error body; the default message stands.
    }
    throw new RequestError(detail, response.status, hint, kind);
  }

  return (await response.json()) as T;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: JSON.stringify(body ?? {}),
  });
}

export const api = {
  info: () => request<ApiInfo>("/api/info"),
  dataStatus: () => request<DataStatus>("/api/data/status"),

  board: (options: { limit?: number; position?: string | null; simulate?: boolean } = {}) => {
    const params = new URLSearchParams();
    params.set("limit", String(options.limit ?? 60));
    if (options.position) params.set("position", options.position);
    if (options.simulate === false) params.set("simulate", "false");
    return request<Board>(`/api/board?${params.toString()}`);
  },

  player: (playerId: string) =>
    request<PlayerDetail>(`/api/players/${encodeURIComponent(playerId)}`),

  search: (query: string, limit = 12) =>
    request<SearchResult[]>(
      `/api/search?q=${encodeURIComponent(query)}&limit=${limit}`,
    ),

  draftStatus: () => request<DraftStatus>("/api/draft/status"),
  startDraft: (body: {
    position?: number | null;
    teams?: number | null;
    rounds?: number | null;
    replace?: boolean;
  }) => post<DraftStatus>("/api/draft/start", body),
  pick: (body: { player_id?: string; name?: string; keeper?: boolean }) =>
    post<PickResult>("/api/draft/pick", body),
  skip: () => post<PickResult>("/api/draft/skip"),
  undo: () => post<PickResult>("/api/draft/undo"),
  completeDraft: () => post<DraftStatus>("/api/draft/complete"),

  llmStatus: () => request<LLMStatus>("/api/llm/status"),
  recommend: (body: { position?: string | null; candidates?: number; live?: boolean }) =>
    post<Recommendation>("/api/recommend", body),
  ask: (question: string) => post<Answer>("/api/ask", { question }),
};
