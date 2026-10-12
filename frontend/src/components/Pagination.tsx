import { formatCount } from "../lib/formatters";

interface Props {
  total: number;
  offset: number;
  limit: number;
  onChange: (offset: number) => void;
}

export function Pagination({ total, offset, limit, onChange }: Props) {
  if (total === 0) return null;
  const from = offset + 1;
  const to = Math.min(offset + limit, total);
  return (
    <nav className="pagination" aria-label="Pagination">
      <span>
        {formatCount(from)}–{formatCount(to)} of {formatCount(total)}
      </span>
      <span className="actions">
        <button type="button" className="btn" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>
          Previous
        </button>
        <button type="button" className="btn" disabled={to >= total} onClick={() => onChange(offset + limit)}>
          Next
        </button>
      </span>
    </nav>
  );
}
