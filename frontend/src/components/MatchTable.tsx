import type { MatchRow } from "../api/client";
import { formatDate } from "../lib/formatters";
import { capitalise, dateHeading, settledHeading, useTypeLabels } from "../lib/typeLabels";

export function MatchTable({ rows }: { rows: MatchRow[] }) {
  const labels = useTypeLabels();
  if (rows.length === 0) return <p className="empty">No matched {labels.left_plural} to show.</p>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">{labels.left}</th>
            <th scope="col">Customer</th>
            <th scope="col">{dateHeading(labels)}</th>
            <th scope="col" className="num">
              {labels.left} amount
            </th>
            <th scope="col">{capitalise(labels.right_plural)}</th>
            <th scope="col" className="num">
              {settledHeading(labels)}
            </th>
            <th scope="col" className="num">
              Difference
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.invoice_id}>
              <td className="mono">{row.invoice_id}</td>
              <td className="nowrap">{row.customer}</td>
              <td className="nowrap">{formatDate(row.issue_date)}</td>
              <td className="num">{row.amount.display}</td>
              <td className="mono">{row.payment_ids.join(", ")}</td>
              <td className="num">{row.paid.display}</td>
              <td className="num">{row.variance.minor === 0 ? "0.00" : row.variance.display}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
