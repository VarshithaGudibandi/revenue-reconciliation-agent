import type { PeriodSummary } from "../api/client";
import { formatCount, formatPercent } from "../lib/formatters";
import { capitalise } from "../lib/typeLabels";

/** The one number the page leads with: how much of the period reconciles. */
export function Hero({ summary }: { summary: PeriodSummary }) {
  const rate = summary.reconciliation_rate ?? 0;
  const { left_plural: left, right_plural: right } = summary.labels;
  return (
    <section className="card hero" aria-label="Reconciliation rate">
      <div>
        <div className="hero-figure">{formatPercent(summary.reconciliation_rate)}</div>
        <p className="hero-caption">
          of {formatCount(summary.invoices)} {left} reconcile with their {right}
        </p>
      </div>
      <div>
        <div
          className="meter"
          role="meter"
          aria-valuemin={0}
          aria-valuemax={summary.invoices}
          aria-valuenow={summary.matched_invoices}
          aria-label={`${capitalise(left)} reconciled`}
        >
          <div className="meter-fill" style={{ width: `${rate * 100}%` }} />
        </div>
        <div className="meter-legend">
          <span>{formatCount(summary.matched_invoices)} reconciled</span>
          <span>{formatCount(summary.invoices - summary.matched_invoices)} not reconciled</span>
        </div>
      </div>
    </section>
  );
}
