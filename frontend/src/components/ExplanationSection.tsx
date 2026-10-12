import type { Explanation } from "../api/client";
import { useAskForExplanation } from "../api/hooks";
import { AlertIcon, CheckCircleIcon } from "./Icons";

/** A language model's reading of the case, next to the one the rules wrote. It is shown only
 * after the server checked every record, figure and id in it against the case. */
export function ExplanationSection({
  caseId,
  explanation,
  canAskModel,
}: {
  caseId: string;
  explanation: Explanation | null;
  canAskModel: boolean;
}) {
  const ask = useAskForExplanation(caseId);
  if (!explanation && !canAskModel) return null;
  const rejected = ask.data && !ask.data.explanation ? ask.data.problems : [];
  return (
    <section className="explanation" aria-label="Explanation by the model">
      <h3 className="section-title">In the model's words</h3>
      {explanation ? (
        <>
          <p>
            <span className="badge badge-good">
              <CheckCircleIcon />
              Checked
            </span>{" "}
            <strong>{explanation.summary}</strong>
          </p>
          <p>{explanation.why}</p>
          <ul className="steps">
            {explanation.next_steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ul>
          <p className="chart-sub">
            Written by {explanation.model}. Every record it names, every figure and every id was checked against
            the case before this was shown. It was given amounts, dates and memo text under labels, not the
            customer or the real ids. Nothing was changed.
          </p>
        </>
      ) : (
        <>
          <div className="actions">
            <button type="button" className="btn" disabled={ask.isPending} onClick={() => ask.mutate()}>
              {ask.isPending ? "Asking…" : "Explain with the model"}
            </button>
            <span className="chart-sub">
              Sends the records' amounts, dates and memo text under labels (R1, R2). Not the customer or the
              ids.
            </span>
          </div>
          {rejected.length > 0 ? (
            <div className="notice notice-warning" role="status">
              <AlertIcon />
              <div>
                <p>The model's text failed the check, so it is not shown. The reading above stands.</p>
                <ul>
                  {rejected.map((problem) => (
                    <li key={problem}>{problem}</li>
                  ))}
                </ul>
              </div>
            </div>
          ) : null}
          {ask.data && !ask.data.explanation && rejected.length === 0 ? (
            <p className="chart-sub">No model is configured.</p>
          ) : null}
        </>
      )}
      {ask.error ? (
        <p className="error-text" role="alert">
          {ask.error.message}
        </p>
      ) : null}
    </section>
  );
}
