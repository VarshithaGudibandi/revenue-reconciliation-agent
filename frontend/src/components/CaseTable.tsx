import type { CaseRow } from "../api/client";
import { formatDateTime } from "../lib/formatters";
import { capitalise, useTypeLabels } from "../lib/typeLabels";
import { KindLabel, StatusBadge } from "./Badges";

function Ids({ ids }: { ids: string[] }) {
  if (ids.length === 0) return <span className="muted">–</span>;
  const shown = ids.slice(0, 2);
  return (
    <span className="mono">
      {shown.join(", ")}
      {ids.length > shown.length ? ` +${ids.length - shown.length}` : ""}
    </span>
  );
}

interface Props {
  rows: CaseRow[];
  selectedId: string | null;
  onOpen: (id: string) => void;
}

export function CaseTable({ rows, selectedId, onOpen }: Props) {
  const labels = useTypeLabels();
  if (rows.length === 0) {
    return <p className="empty">No cases match these filters.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Case</th>
            <th scope="col">Problem</th>
            <th scope="col">Customer</th>
            <th scope="col">{capitalise(labels.left_plural)}</th>
            <th scope="col">{capitalise(labels.right_plural)}</th>
            <th scope="col" className="num">
              Difference
            </th>
            <th scope="col">Status</th>
            <th scope="col">Updated</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.id}
              className={`clickable${row.id === selectedId ? " selected" : ""}`}
              onClick={() => onOpen(row.id)}
            >
              <td className="num">
                <button type="button" className="row-link" onClick={(e) => { e.stopPropagation(); onOpen(row.id); }}>
                  #{row.number}
                </button>
              </td>
              <td>
                <KindLabel kind={row.kind} />
                <div className="muted">{row.summary}</div>
              </td>
              <td className="nowrap">{row.customer ?? <span className="muted">–</span>}</td>
              <td>
                <Ids ids={row.invoice_ids} />
              </td>
              <td>
                <Ids ids={row.payment_ids} />
              </td>
              <td className="num">{row.variance ? row.variance.display : <span className="muted">unknown</span>}</td>
              <td>
                <StatusBadge status={row.status} unverified={row.fix_unverified} />
              </td>
              <td className="muted nowrap">{formatDateTime(row.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
