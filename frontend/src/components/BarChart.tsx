import { useRef, useState } from "react";

export interface BarDatum {
  key: string;
  label: string;
  value: number;
  display: string;
}

interface Props {
  title: string;
  subtitle?: string;
  valueHeading: string;
  data: BarDatum[];
  refreshing?: boolean;
  emptyText?: string;
}

interface Hover {
  datum: BarDatum;
  x: number;
  y: number;
}

/**
 * Horizontal bars for one series: thin marks, a rounded data end, the value at the tip.
 * The tooltip repeats what the labels already say and the table view holds the same
 * numbers, so nothing is reachable only by hovering.
 */
export function BarChart({ title, subtitle, valueHeading, data, refreshing, emptyText }: Props) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const [hover, setHover] = useState<Hover | null>(null);
  const card = useRef<HTMLDivElement>(null);
  const max = Math.max(1, ...data.map((d) => d.value));

  function show(datum: BarDatum, clientX: number, clientY: number) {
    const box = card.current?.getBoundingClientRect();
    if (!box) return;
    setHover({ datum, x: clientX - box.left, y: clientY - box.top - 8 });
  }

  return (
    <section className="chart-card" ref={card} aria-label={title}>
      <div className="chart-head">
        <div>
          <h2 className="chart-title">{title}</h2>
          {subtitle ? <p className="chart-sub">{subtitle}</p> : null}
        </div>
        <div className="view-toggle" role="group" aria-label={`${title} view`}>
          <button type="button" aria-pressed={view === "chart"} onClick={() => setView("chart")}>
            Chart
          </button>
          <button type="button" aria-pressed={view === "table"} onClick={() => setView("table")}>
            Table
          </button>
        </div>
      </div>

      <div className={refreshing ? "refreshing" : undefined}>
        {data.length === 0 ? (
          <p className="empty">{emptyText ?? "Nothing to show."}</p>
        ) : view === "chart" ? (
          <div className="bar-rows" onPointerLeave={() => setHover(null)}>
            {data.map((d) => (
              <div
                key={d.key}
                className="bar-row"
                tabIndex={0}
                aria-label={`${d.label}: ${d.display}`}
                onPointerMove={(e) => show(d, e.clientX, e.clientY)}
                onFocus={(e) => {
                  const r = e.currentTarget.getBoundingClientRect();
                  show(d, r.left + r.width / 2, r.top);
                }}
                onBlur={() => setHover(null)}
              >
                <span className="bar-label" title={d.label}>
                  {d.label}
                </span>
                <span className="bar-track">
                  <span className="bar" style={{ width: `${(d.value / max) * 78}%` }} />
                  <span className="bar-value">{d.display}</span>
                </span>
              </div>
            ))}
          </div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Category</th>
                <th scope="col" className="num">
                  {valueHeading}
                </th>
              </tr>
            </thead>
            <tbody>
              {data.map((d) => (
                <tr key={d.key}>
                  <td>{d.label}</td>
                  <td className="num">{d.display}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {hover ? (
        <div className="tooltip" role="presentation" style={{ left: hover.x, top: hover.y }}>
          <div className="tooltip-value">{hover.datum.display}</div>
          <div className="tooltip-label">{hover.datum.label}</div>
        </div>
      ) : null}
    </section>
  );
}
