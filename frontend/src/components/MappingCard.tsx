import type { ColumnChoice, FileProposal } from "../api/client";
import { DECIMAL_LABELS, describeDateFormat } from "../lib/labels";
import { fieldHints, fieldLabels, useTypeLabels } from "../lib/typeLabels";
import { AlertIcon, CheckCircleIcon } from "./Icons";

export type Source = "invoices" | "payments";

/** The mapping being edited. An empty string means nothing is chosen yet. */
export interface MappingState {
  record_id: string;
  counterparty: string;
  date: string;
  amount: string;
  currency: string;
  reference: string;
  text: string;
  date_format: string;
  decimal_separator: "." | "," | "";
}

export type MappingKey = keyof MappingState;

/** The key under which a row's answer is tracked, so a source's rows stay apart. */
export const answerKey = (source: Source, key: MappingKey) => `${source}.${key}`;

export function initialMapping(proposal: FileProposal): MappingState {
  const column = (field: string) => proposal.fields.find((f) => f.field === field)?.column ?? "";
  return {
    record_id: column("record_id"),
    counterparty: column("counterparty"),
    date: column("date"),
    amount: column("amount"),
    currency: column("currency"),
    reference: column("reference"),
    text: column("text"),
    date_format: proposal.date_format.value ?? "",
    decimal_separator: (proposal.decimal_separator.value as "." | "," | null) ?? "",
  };
}

/** Rows the engine settled by itself need no answer; the rest wait for the user. */
export function initialAnswered(source: Source, proposal: FileProposal): Set<string> {
  const answered = new Set<string>();
  for (const f of proposal.fields) if (f.confident) answered.add(answerKey(source, f.field as MappingKey));
  if (proposal.date_format.confident) answered.add(answerKey(source, "date_format"));
  if (proposal.decimal_separator.confident) answered.add(answerKey(source, "decimal_separator"));
  return answered;
}

const REQUIRED: MappingKey[] = [
  "record_id",
  "counterparty",
  "date",
  "amount",
  "currency",
  "date_format",
  "decimal_separator",
];

/** What must be chosen before a file can be read at all. */
export function requiredKeys(): MappingKey[] {
  return REQUIRED;
}

/** What the user has to look at: everything required, and the optional columns. */
export function reviewKeys(source: Source): MappingKey[] {
  return source === "payments" ? [...REQUIRED, "reference", "text"] : [...REQUIRED, "text"];
}

const OPTIONAL: MappingKey[] = ["reference", "text"];

/** True when every required choice is made, so the files can be previewed. */
export function isComplete(mapping: MappingState): boolean {
  return REQUIRED.every((k) => mapping[k] !== "");
}

export function toBody(mapping: MappingState) {
  return {
    record_id: mapping.record_id,
    counterparty: mapping.counterparty,
    date: mapping.date,
    amount: mapping.amount,
    currency: mapping.currency,
    reference: mapping.reference || null,
    text: mapping.text || null,
    date_format: mapping.date_format,
    decimal_separator: mapping.decimal_separator as "." | ",",
  };
}

function Status({ settled, answered }: { settled: boolean; answered: boolean }) {
  if (answered) {
    return (
      <span className="badge badge-good">
        <CheckCircleIcon />
        {settled ? "Confident" : "Checked by you"}
      </span>
    );
  }
  return (
    <span className="badge badge-warning">
      <AlertIcon />
      Needs your answer
    </span>
  );
}

interface RowProps {
  id: string;
  label: string;
  hint?: string;
  value: string;
  options: { value: string; label: string }[];
  settled: boolean;
  answered: boolean;
  reasons: string[];
  onChange: (value: string) => void;
  onAccept: () => void;
}

function Row({ id, label, hint, value, options, settled, answered, reasons, onChange, onAccept }: RowProps) {
  return (
    <tr>
      <td>
        <label htmlFor={id}>{label}</label>
        {hint ? <div className="chart-sub">{hint}</div> : null}
      </td>
      <td>
        <select id={id} className="select" value={value} onChange={(e) => onChange(e.target.value)}>
          {value === "" ? <option value="">Choose…</option> : null}
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        {reasons.length > 0 ? <div className="chart-sub reasons">{reasons.join(" · ")}</div> : null}
      </td>
      <td className="nowrap">
        <Status settled={settled} answered={answered} />
        {!answered && value !== "" ? (
          <button type="button" className="btn btn-quiet" onClick={onAccept}>
            This is right
          </button>
        ) : null}
      </td>
    </tr>
  );
}

interface CardProps {
  source: Source;
  proposal: FileProposal;
  dateFormats: string[];
  mapping: MappingState;
  answered: ReadonlySet<string>;
  onChange: (key: MappingKey, value: string) => void;
  onAccept: (key: MappingKey) => void;
}

export function MappingCard({ source, proposal, dateFormats, mapping, answered, onChange, onAccept }: CardProps) {
  const labels = useTypeLabels();
  const hints = fieldHints(labels);
  const title = `${source === "invoices" ? labels.left : labels.right} file`;
  const fields = proposal.fields as ColumnChoice[];
  const choice = (key: string) => fields.find((f) => f.field === key);

  const columnOptions = (key: MappingKey) => {
    const possible = new Set(choice(key)?.alternatives ?? []);
    const base = proposal.headers.map((h) => ({
      value: h,
      label: possible.has(h) && h !== mapping[key] ? `${h} · possible` : h,
    }));
    return OPTIONAL.includes(key) ? [{ value: "", label: "No such column" }, ...base] : base;
  };

  return (
    <section className="card mapping-card" aria-label={title}>
      <div className="chart-head">
        <h3 className="chart-title">{title}</h3>
        <span className="chart-sub">
          {proposal.file_name} · {proposal.rows} rows
        </span>
      </div>
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Field</th>
            <th scope="col">Column in your file</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {fields.map((f) => {
            const key = f.field as MappingKey;
            const k = answerKey(source, key);
            return (
              <Row
                key={f.field}
                id={`${source}-${f.field}`}
                label={fieldLabels(labels)[source][f.field] ?? f.field}
                hint={hints[f.field]}
                value={mapping[key]}
                options={columnOptions(key)}
                settled={f.confident}
                answered={answered.has(k)}
                reasons={f.reasons}
                onChange={(v) => onChange(key, v)}
                onAccept={() => onAccept(key)}
              />
            );
          })}
          <Row
            id={`${source}-date_format`}
            label="Date format"
            hint="Read from the values; never guessed."
            value={mapping.date_format}
            options={[...new Set([...proposal.date_format.alternatives, ...dateFormats])].map((f) => ({
              value: f,
              label: describeDateFormat(f),
            }))}
            settled={proposal.date_format.confident}
            answered={answered.has(answerKey(source, "date_format"))}
            reasons={proposal.date_format.reasons}
            onChange={(v) => onChange("date_format", v)}
            onAccept={() => onAccept("date_format")}
          />
          <Row
            id={`${source}-decimal_separator`}
            label="Decimal separator"
            value={mapping.decimal_separator}
            options={[".", ","].map((v) => ({ value: v, label: DECIMAL_LABELS[v] ?? v }))}
            settled={proposal.decimal_separator.confident}
            answered={answered.has(answerKey(source, "decimal_separator"))}
            reasons={proposal.decimal_separator.reasons}
            onChange={(v) => onChange("decimal_separator", v)}
            onAccept={() => onAccept("decimal_separator")}
          />
        </tbody>
      </table>
    </section>
  );
}
