import type { components } from "./schema";

export type Schemas = components["schemas"];
export type Amount = Schemas["Amount"];
export type CaseRow = Schemas["CaseRow"];
export type CaseDetail = Schemas["CaseDetailOut"];
export type MatchRow = Schemas["MatchRow"];
export type PeriodSummary = Schemas["PeriodSummary"];
export type Upload = Schemas["UploadOut"];
export type Job = Schemas["JobOut"];
export type Channel = Schemas["ChannelOut"];
export type ChannelBody = Schemas["ChannelBody"];
export type ChannelPatch = Schemas["ChannelPatch"];
export type Delivery = Schemas["DeliveryOut"];
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
  if (response.status === 204) return undefined as T;
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

/** How a queued reconciliation is waited for. Exported so a test can make it quick. */
export const jobPolling = { intervalMs: 1000, timeoutMs: 10 * 60 * 1000 };

async function untilDone(first: Job): Promise<Job> {
  const deadline = Date.now() + jobPolling.timeoutMs;
  let job = first;
  while (job.status === "queued" || job.status === "running") {
    if (Date.now() > deadline) {
      throw new ApiError(0, "job_timeout", "Reconciliation is still running. Reload in a minute to see the result.");
    }
    await new Promise((resolve) => setTimeout(resolve, jobPolling.intervalMs));
    job = await request<Job>(`/api/jobs/${job.id}`);
  }
  if (job.status === "failed") {
    throw new ApiError(0, "job_failed", `Reconciliation failed: ${job.last_error ?? "no reason was recorded"}`);
  }
  return job;
}

/** A server with a job queue answers 202 with a job instead of the result. Wait for the job and
 * return what the caller would have got if the work had been done in the request. */
async function settled<T extends { job?: Job | null; run: Job["run"]; cases: Job["cases"] }>(
  result: T,
): Promise<T> {
  if (!result.job) return result;
  const done = await untilDone(result.job);
  return {
    ...result,
    ...("reconciled" in result ? { reconciled: true } : {}),
    job: done,
    run: done.run,
    cases: done.cases,
  };
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
    }).then(settled);
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
  ) => post<Confirmation>(`/api/setup/proposals/${proposalId}/confirm`, body).then(settled),
  capabilities: () => request<Capabilities>("/api/capabilities"),
  channels: () => request<Channel[]>("/api/alert-channels"),
  createChannel: (body: ChannelBody) => post<Channel>("/api/alert-channels", body),
  updateChannel: (id: string, body: ChannelPatch) =>
    request<Channel>(`/api/alert-channels/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteChannel: (id: string) => request<void>(`/api/alert-channels/${id}`, { method: "DELETE" }),
  testChannel: (id: string) => request<Delivery>(`/api/alert-channels/${id}/test`, { method: "POST" }),
  deliveries: () => request<Delivery[]>("/api/alert-deliveries"),
  askForExplanation: (caseId: string) =>
    request<AskedExplanation>(`/api/cases/${caseId}/explanation`, { method: "POST" }),
  askForSuggestion: (caseId: string) =>
    request<Suggestion>(`/api/cases/${caseId}/suggestion`, { method: "POST" }),
  demo: () => request<Schemas["DemoOut"]>("/api/demo"),
  loadCorrectedExport: () =>
    request<Upload>("/api/demo/load-corrected-export", { method: "POST" }).then(settled),
};
