import type { components } from "./schema";

export type Schemas = components["schemas"];
export type Amount = Schemas["Amount"];
export type CaseRow = Schemas["CaseRow"];
export type CaseDetail = Schemas["CaseDetailOut"];
export type MatchRow = Schemas["MatchRow"];
export type PeriodSummary = Schemas["PeriodSummary"];
export type Upload = Schemas["UploadOut"];
export type AcceptReason = Schemas["AcceptBody"]["reason"];
export type Proposal = Schemas["ProposalOut"];
export type FileProposal = Schemas["FileProposalOut"];
export type ColumnChoice = Schemas["ColumnChoiceOut"];
export type MappingBody = Schemas["MappingBody"];
export type Preview = Schemas["PreviewOut"];
export type FilePreview = Schemas["FilePreview"];
export type Suggestion = Schemas["SuggestionOut"];
export type Explanation = Schemas["ExplanationOut"];
export type AskedExplanation = Schemas["AskExplanationOut"];
export type Capabilities = Schemas["CapabilitiesOut"];
export type Confirmation = Schemas["ConfirmOut"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly currentVersion?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, "network", "Could not reach the server. Check your connection and try again.");
  }
  if (!response.ok) {
    let body: Schemas["ErrorBody"] | undefined;
    try {
      body = (await response.json()) as Schemas["ErrorBody"];
    } catch {
      /* not JSON */
    }
    const error = body?.error;
    throw new ApiError(
      response.status,
      error?.code ?? "http_error",
      error?.message ?? `The server answered ${response.status}.`,
      error?.current_version ?? undefined,
    );
  }
  return (await response.json()) as T;
}

function query(params: Record<string, string | number | string[] | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, v));
    else search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

function post<T>(path: string, body: unknown, idempotencyKey?: string): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  return request<T>(path, { method: "POST", headers, body: JSON.stringify(body) });
}

export interface CaseQuery {
  status?: string[];
  kind?: string[];
  q?: string;
  sort?: "amount" | "number" | "updated";
  limit?: number;
  offset?: number;
}

const periodPath = (profileId: string, period: string) =>
  `/api/profiles/${profileId}/periods/${period}`;

export const api = {
  profiles: () => request<Schemas["ProfileOut"][]>("/api/profiles"),
  periods: (profileId: string) => request<Schemas["PeriodOut"][]>(`/api/profiles/${profileId}/periods`),
  summary: (profileId: string, period: string) =>
    request<PeriodSummary>(`${periodPath(profileId, period)}/summary`),
  cases: (profileId: string, period: string, params: CaseQuery) =>
    request<Schemas["CasePage"]>(`${periodPath(profileId, period)}/cases${query({ ...params })}`),
  matches: (profileId: string, period: string, params: { q?: string; limit?: number; offset?: number }) =>
    request<Schemas["MatchPage"]>(`${periodPath(profileId, period)}/matches${query(params)}`),
  caseDetail: (caseId: string) => request<CaseDetail>(`/api/cases/${caseId}`),

  reportFix: (caseId: string, expectedVersion: number, key: string) =>
    post<CaseDetail>(`/api/cases/${caseId}/report-fix`, { expected_version: expectedVersion }, key),
  retractFix: (caseId: string, expectedVersion: number, key: string) =>
    post<CaseDetail>(`/api/cases/${caseId}/retract-fix`, { expected_version: expectedVersion }, key),
  accept: (caseId: string, body: { reason: AcceptReason; note: string; expected_version: number }, key: string) =>
    post<CaseDetail>(`/api/cases/${caseId}/accept`, body, key),
  reopen: (caseId: string, expectedVersion: number, key: string) =>
    post<CaseDetail>(`/api/cases/${caseId}/reopen`, { expected_version: expectedVersion }, key),
  addNote: (caseId: string, body: string, key: string) =>
    post<CaseDetail>(`/api/cases/${caseId}/notes`, { body }, key),

  upload: (profileId: string, period: string, source: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<Upload>(`${periodPath(profileId, period)}/files/${source}`, {
      method: "POST",
      body: form,
    });
  },
  propose: (invoices: File, payments: File, period: string) => {
    const form = new FormData();
    form.append("invoices", invoices);
    form.append("payments", payments);
    form.append("period", period);
    return request<Proposal>("/api/setup/proposals", { method: "POST", body: form });
  },
  previewMapping: (proposalId: string, body: { invoices: MappingBody; payments: MappingBody }) =>
    post<Preview>(`/api/setup/proposals/${proposalId}/preview`, body),
  confirmMapping: (
    proposalId: string,
    body: { name: string; invoices: MappingBody; payments: MappingBody },
  ) => post<Confirmation>(`/api/setup/proposals/${proposalId}/confirm`, body),
  capabilities: () => request<Capabilities>("/api/capabilities"),
  askForExplanation: (caseId: string) =>
    request<AskedExplanation>(`/api/cases/${caseId}/explanation`, { method: "POST" }),
  askForSuggestion: (caseId: string) =>
    request<Suggestion>(`/api/cases/${caseId}/suggestion`, { method: "POST" }),
  demo: () => request<Schemas["DemoOut"]>("/api/demo"),
  loadCorrectedExport: () =>
    request<Upload>("/api/demo/load-corrected-export", { method: "POST" }),
};
