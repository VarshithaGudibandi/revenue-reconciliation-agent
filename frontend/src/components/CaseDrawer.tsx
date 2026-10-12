import { useState } from "react";
import { api, ApiError, type CaseDetail } from "../api/client";
import { useCapabilities, useCase, useCaseAction } from "../api/hooks";
import { formatDate, formatDateTime } from "../lib/formatters";
import { EVENT_LABELS, KIND_LABELS, REASON_LABELS, label } from "../lib/labels";
import { StatusBadge } from "./Badges";
import { AcceptDialog } from "./Dialogs";
import { CloseIcon } from "./Icons";
import { ExplanationSection } from "./ExplanationSection";
import { SuggestionSection } from "./SuggestionSection";

function Notice({ tone, children }: { tone?: "warning" | "critical" | "good"; children: React.ReactNode }) {
  return <div className={`notice${tone ? ` notice-${tone}` : ""}`}>{children}</div>;
}

function StatusNotice({ detail, onOpenCase }: { detail: CaseDetail; onOpenCase: (id: string) => void }) {
  if (detail.fix_unverified) {
    return (
      <Notice tone="critical">
        A fix was reported, but the latest data still shows this problem. It has been reopened.
      </Notice>
    );
  }
  switch (detail.status) {
    case "fix_reported":
      return (
        <Notice tone="warning">
          You reported this as fixed. It is checked when the next export arrives: if the records
          reconcile it resolves, otherwise it reopens.
        </Notice>
      );
    case "accepted":
      return (
        <Notice>
          Accepted as {label(REASON_LABELS, detail.accepted_reason ?? "other").toLowerCase()}. It
          reopens if the underlying data changes.
        </Notice>
      );
    case "resolved":
      return (
        <Notice tone="good">
          Resolved by the data{detail.resolved_at ? ` on ${formatDateTime(detail.resolved_at)}` : ""}:
          its records now reconcile.
        </Notice>
      );
    case "withdrawn":
      return (
        <Notice>
          {detail.closed_reason === "merged" && detail.successor_case_id ? (
            <span>
              This case became part of another.{" "}
              <button type="button" className="row-link" style={{ textDecoration: "underline" }} onClick={() => onOpenCase(detail.successor_case_id!)}>
                Open it
              </button>
            </span>
          ) : (
            "Its records are no longer in the data, so it no longer applies."
          )}
        </Notice>
      );
    default:
      return null;
  }
}

export function CaseDrawer({
  caseId,
  onClose,
  onOpenCase,
}: {
  caseId: string;
  onClose: () => void;
  onOpenCase: (id: string) => void;
}) {
  const { data, isLoading, error } = useCase(caseId);
  const capabilities = useCapabilities();
  const action = useCaseAction(caseId);
  const [accepting, setAccepting] = useState(false);
  const [note, setNote] = useState("");

  const actionError = action.error instanceof ApiError ? action.error : null;

  function run(call: (key: string) => Promise<CaseDetail>, then?: () => void) {
    action.mutate(call, { onSuccess: then });
  }

  return (
    <div
      className="overlay"
      onClick={onClose}
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
    >
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label="Case details"
        onClick={(e) => e.stopPropagation()}
      >
        {isLoading ? <p className="muted">Loading…</p> : null}
        {error ? (
          <p className="error-text" role="alert">
            {error.message}
          </p>
        ) : null}
        {data ? (
          <>
            <div className="drawer-head">
              <div>
                <p className="muted">
                  Case #{data.number} · {label(KIND_LABELS, data.kind)}
                </p>
                <h2 style={{ marginTop: 2 }}>{data.analysis.summary}</h2>
              </div>
              <button type="button" className="btn btn-quiet" aria-label="Close" onClick={onClose} autoFocus>
                <CloseIcon />
              </button>
            </div>

            <div className="actions" style={{ alignItems: "center" }}>
              <StatusBadge status={data.status} unverified={data.fix_unverified} />
              {data.variance ? <span>Difference {data.variance.display}</span> : <span className="muted">Difference unknown</span>}
            </div>

            <StatusNotice detail={data} onOpenCase={onOpenCase} />

            <section>
              <h3 className="section-title">Why</h3>
              <p>{data.analysis.why}</p>
              {data.analysis.next_steps.length > 0 ? (
                <>
                  <h3 className="section-title" style={{ marginTop: 14 }}>
                    What to check
                  </h3>
                  <ul className="steps">
                    {data.analysis.next_steps.map((step) => (
                      <li key={step}>{step}</li>
                    ))}
                  </ul>
                </>
              ) : null}
              <p className="chart-sub" style={{ marginTop: 10 }}>
                Written by rules from the records, not generated.
              </p>
            </section>

            <ExplanationSection
              key={data.explanation?.summary ?? "none"}
              caseId={caseId}
              explanation={data.explanation ?? null}
              canAskModel={capabilities.data?.explainer.model != null}
            />

            {data.suggestion ? (
              <SuggestionSection
                caseId={caseId}
                suggestion={data.suggestion}
                canAskModel={capabilities.data?.adjudicator.model != null}
              />
            ) : null}

            <section>
              <h3 className="section-title">Records</h3>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Record</th>
                      <th scope="col">Customer</th>
                      <th scope="col">Date</th>
                      <th scope="col" className="num">
                        Amount
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.records.map((r) => (
                      <tr key={`${r.role}-${r.record_id}`}>
                        <td>
                          <span className="muted">{r.role === "invoice" ? "Invoice" : "Payment"}</span>{" "}
                          <span className="mono">{r.record_id}</span>
                          {r.text ? <div className="chart-sub record-text">{r.text}</div> : null}
                        </td>
                        <td>{r.customer ?? <span className="muted">–</span>}</td>
                        <td>{r.occurred_on ? formatDate(r.occurred_on) : <span className="muted">–</span>}</td>
                        <td className="num">
                          {r.amount ? r.amount.display : <span className="muted">not in current files</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>

            {actionError ? (
              <p className="error-text" role="alert">
                {actionError.code === "stale_case"
                  ? "This case changed since you opened it, so nothing was changed. The latest version is shown; review it and try again."
                  : actionError.message}
              </p>
            ) : null}

            <div className="actions">
              {data.status === "open" ? (
                <>
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={action.isPending}
                    onClick={() => run((key) => api.reportFix(caseId, data.version, key))}
                  >
                    I fixed this in the source
                  </button>
                  <button type="button" className="btn" onClick={() => setAccepting(true)}>
                    Accept this difference…
                  </button>
                </>
              ) : null}
              {data.status === "fix_reported" ? (
                <>
                  <button
                    type="button"
                    className="btn"
                    disabled={action.isPending}
                    onClick={() => run((key) => api.retractFix(caseId, data.version, key))}
                  >
                    Take back the fix report
                  </button>
                  <button type="button" className="btn" onClick={() => setAccepting(true)}>
                    Accept this difference…
                  </button>
                </>
              ) : null}
              {data.status === "accepted" ? (
                <button
                  type="button"
                  className="btn"
                  disabled={action.isPending}
                  onClick={() => run((key) => api.reopen(caseId, data.version, key))}
                >
                  Reopen
                </button>
              ) : null}
            </div>

            <section>
              <h3 className="section-title">Notes</h3>
              <div style={{ display: "grid", gap: 8 }}>
                {data.notes.map((n) => (
                  <div key={n.id} className="note">
                    {n.body}
                    <div className="chart-sub">{formatDateTime(n.created_at)}</div>
                  </div>
                ))}
                <form
                  style={{ display: "grid", gap: 8 }}
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (!note.trim()) return;
                    run((key) => api.addNote(caseId, note.trim(), key), () => setNote(""));
                  }}
                >
                  <label className="field">
                    <span>Add a note</span>
                    <textarea className="input" value={note} onChange={(e) => setNote(e.target.value)} />
                  </label>
                  <div>
                    <button type="submit" className="btn" disabled={!note.trim() || action.isPending}>
                      Add note
                    </button>
                  </div>
                </form>
              </div>
            </section>

            <section>
              <h3 className="section-title">History</h3>
              <ol className="timeline">
                {data.events.map((e) => (
                  <li key={e.id}>
                    <span>{label(EVENT_LABELS, e.kind)}</span>
                    <span className="chart-sub">{formatDateTime(e.created_at)}</span>
                  </li>
                ))}
              </ol>
            </section>
          </>
        ) : null}

        {accepting && data ? (
          <AcceptDialog
            busy={action.isPending}
            onClose={() => setAccepting(false)}
            onSubmit={(reason, text) =>
              run(
                (key) => api.accept(caseId, { reason, note: text, expected_version: data.version }, key),
                () => setAccepting(false),
              )
            }
          />
        ) : null}
      </aside>
    </div>
  );
}
