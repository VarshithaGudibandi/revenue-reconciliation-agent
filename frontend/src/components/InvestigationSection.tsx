import { useState } from "react";
import type { Investigation } from "../api/client";
import { useDecideInvestigation, useInvestigation, useStartInvestigation } from "../api/hooks";
import { AlertIcon, CheckCircleIcon } from "./Icons";

/** The kinds of case an investigation can look into. */
export const INVESTIGABLE = new Set(["amount_mismatch", "missing_payment", "unmatched_payment"]);

const words = (text: string) => text.replaceAll("_", " ");

function describeStep(step: Investigation["trace"][number]): string {
  const args = Object.entries(step.arguments)
    .map(([key, value]) => `${words(key)} ${String(value)}`)
    .join(", ");
  return `${words(step.tool)}${args ? ` (${args})` : ""}`;
}

/** A model's investigation of why the case arose: what it looked at, what it found, and a
 * decision for a person to make. Nothing changes until someone approves. */
export function InvestigationSection({
  caseId,
  kind,
  caseVersion,
  canInvestigate,
}: {
  caseId: string;
  kind: string;
  caseVersion: number;
  canInvestigate: boolean;
}) {
  const current = useInvestigation(caseId);
  const start = useStartInvestigation(caseId);
  const decide = useDecideInvestigation(caseId);
  const [note, setNote] = useState("");
  if (!INVESTIGABLE.has(kind)) return null;
  const investigation = current.data ?? null;
  if (!investigation && !canInvestigate) return null;

  const decideWith = (approve: boolean) =>
    investigation &&
    decide.mutate({
      id: investigation.id,
      body: { approve, note: note.trim() || null, expected_version: caseVersion },
    });
  const finding = investigation?.finding;
  const actionText =
    finding?.action === "accept_variance" ? "Accept the variance as a cost and note why" : "Add the finding to the notes";

  return (
    <section className="explanation" aria-label="Investigation by the model">
      <h3 className="section-title">Why it happened</h3>
      {!investigation || investigation.status === "failed" ? (
        <div className="actions">
          <button
            type="button"
            className="btn"
            disabled={start.isPending || !canInvestigate}
            onClick={() => start.mutate()}
          >
            {start.isPending ? "Investigating…" : "Investigate with the model"}
          </button>
          <span className="chart-sub">
            The model looks through this month's records with read-only tools, under labels, and proposes a cause.
            Nothing changes unless you approve.
          </span>
        </div>
      ) : null}

      {investigation?.status === "failed" ? (
        <p className="chart-sub">The last attempt did not finish: {investigation.error}</p>
      ) : null}

      {investigation?.status === "not_shown" ? (
        <div className="notice notice-warning" role="status">
          <AlertIcon />
          <div>
            <p>The model's finding failed the checks, so it is not shown and cannot be approved.</p>
            <ul>
              {investigation.problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      {finding && investigation && investigation.status !== "not_shown" ? (
        <>
          <p>
            <span className="badge badge-good">
              <CheckCircleIcon />
              Checked
            </span>{" "}
            <strong>{words(finding.cause)}</strong> <span className="chart-sub">({finding.confidence} confidence)</span>
          </p>
          <p>{finding.note}</p>
          <p className="chart-sub">Rests on: {finding.evidence.join(", ")}</p>
          {investigation.status === "awaiting_decision" ? (
            <>
              <p>
                <strong>Proposed:</strong> {actionText}.
              </p>
              <label className="field">
                <span>Your note (optional)</span>
                <textarea value={note} onChange={(event) => setNote(event.target.value)} rows={2} />
              </label>
              <div className="actions">
                <button type="button" className="btn btn-primary" disabled={decide.isPending} onClick={() => decideWith(true)}>
                  Approve
                </button>
                <button type="button" className="btn" disabled={decide.isPending} onClick={() => decideWith(false)}>
                  Reject
                </button>
              </div>
            </>
          ) : (
            <p className="chart-sub" role="status">
              {investigation.status === "applied"
                ? investigation.outcome === "accepted"
                  ? "Approved: the variance was accepted and the finding added to the notes."
                  : "Approved: the finding was added to the notes."
                : "Rejected: nothing was changed."}
            </p>
          )}
        </>
      ) : null}

      {investigation && investigation.trace.length > 0 ? (
        <details>
          <summary className="chart-sub">
            What {investigation.model} looked at ({investigation.trace.length} steps)
          </summary>
          <ol className="steps">
            {investigation.trace.map((step, index) => (
              <li key={index}>{describeStep(step)}</li>
            ))}
          </ol>
        </details>
      ) : null}

      {start.error || decide.error ? (
        <p className="error-text" role="alert">
          {(start.error ?? decide.error)?.message}
        </p>
      ) : null}
    </section>
  );
}
