import type { Suggestion } from "../api/client";
import { useAskForSuggestion } from "../api/hooks";
import { AlertIcon, CheckCircleIcon } from "./Icons";

/** Which invoice an ambiguous payment was probably meant for. Advice for a person; the case is
 * not changed by it. */
export function SuggestionSection({
  caseId,
  suggestion,
  canAskModel,
}: {
  caseId: string;
  suggestion: Suggestion;
  canAskModel: boolean;
}) {
  const ask = useAskForSuggestion(caseId);
  const readBy = suggestion.source === "rules" ? "Read by rules" : `Read by rules and ${suggestion.model ?? "a model"}`;
  return (
    <section className="suggestion" aria-label="Suggested invoice">
      <h3 className="section-title">Which invoice was meant?</h3>
      {suggestion.invoice_id ? (
        <>
          <p>
            <span>Probably </span>
            <span className="mono">{suggestion.invoice_id}</span>{" "}
            {suggestion.confident ? (
              <span className="badge badge-good">
                <CheckCircleIcon />
                Confident
              </span>
            ) : (
              <span className="badge badge-warning">
                <AlertIcon />
                A suggestion, not settled
              </span>
            )}
          </p>
          {suggestion.evidence ? (
            <blockquote className="quote">
              <span className="chart-sub">From the payment's memo</span>
              <br />“{suggestion.evidence}”
            </blockquote>
          ) : null}
        </>
      ) : (
        <p>
          Nothing in the payment's memo tells these invoices apart
          {suggestion.alternatives.length > 1 ? (
            <span>
              {" "}
              (still in the running: <span className="mono">{suggestion.alternatives.join(", ")}</span>)
            </span>
          ) : null}
          .
        </p>
      )}
      {suggestion.reasons.length > 0 ? <p className="secondary">{suggestion.reasons.join(" · ")}</p> : null}
      {canAskModel && suggestion.source === "rules" ? (
        <div className="actions">
          <button type="button" className="btn" disabled={ask.isPending} onClick={() => ask.mutate()}>
            {ask.isPending ? "Asking…" : "Ask the model"}
          </button>
          <span className="chart-sub">
            Sends the memo and each invoice's number, description and age. Not the customer or the amounts.
          </span>
        </div>
      ) : null}
      {ask.error ? (
        <p className="error-text" role="alert">
          {ask.error.message}
        </p>
      ) : null}
      <p className="chart-sub">
        {readBy}. Advice only: nothing was changed. To act on it, correct the reference on the payment in the
        source system.
      </p>
    </section>
  );
}
