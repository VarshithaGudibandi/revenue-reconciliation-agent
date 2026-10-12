import { useMemo, useState } from "react";
import type { Confirmation, FilePreview, Proposal } from "../api/client";
import { useConfirm, usePreview, usePropose } from "../api/hooks";
import { formatDate } from "../lib/formatters";
import { INVOICE_LABELS, LabelsProvider, TYPE_CHOICES, useTypeLabels } from "../lib/typeLabels";
import { AlertIcon, CheckCircleIcon } from "./Icons";
import {
  MappingCard,
  answerKey,
  initialAnswered,
  initialMapping,
  isComplete,
  reviewKeys,
  toBody,
  type MappingKey,
  type MappingState,
  type Source,
} from "./MappingCard";

const SOURCES: Source[] = ["invoices", "payments"];

function previousMonth(): string {
  const now = new Date();
  const month = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  return `${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, "0")}`;
}

export function Setup({
  firstTime,
  onDone,
  onCancel,
}: {
  firstTime: boolean;
  onDone: (result: Confirmation) => void;
  onCancel: () => void;
}) {
  const [proposal, setProposal] = useState<Proposal | null>(null);
  return (
    <section className="card setup" aria-label="Set up a reconciliation">
      <div className="chart-head">
        <h2 className="chart-title">Set up a reconciliation</h2>
        {!firstTime || proposal ? (
          <button type="button" className="btn btn-quiet" onClick={onCancel}>
            Cancel
          </button>
        ) : null}
      </div>
      {proposal ? (
        <LabelsProvider value={proposal.labels}>
          <Review key={proposal.id} proposal={proposal} onDone={onDone} />
        </LabelsProvider>
      ) : (
        <Start onProposed={setProposal} />
      )}
    </section>
  );
}

function Start({ onProposed }: { onProposed: (proposal: Proposal) => void }) {
  const [invoices, setInvoices] = useState<File | null>(null);
  const [payments, setPayments] = useState<File | null>(null);
  const [period, setPeriod] = useState(previousMonth);
  const [type, setType] = useState(INVOICE_LABELS.type);
  const labels = TYPE_CHOICES.find((t) => t.type === type) ?? INVOICE_LABELS;
  const propose = usePropose();
  return (
    <form
      className="setup-start"
      onSubmit={(e) => {
        e.preventDefault();
        if (invoices && payments)
          propose.mutate({ invoices, payments, period, type }, { onSuccess: onProposed });
      }}
    >
      <label className="field">
        <span>What to reconcile</span>
        <select className="select" value={type} onChange={(e) => setType(e.target.value)}>
          {TYPE_CHOICES.map((t) => (
            <option key={t.type} value={t.type}>
              {t.title}
            </option>
          ))}
        </select>
      </label>
      <p className="secondary">
        Upload one {labels.left.toLowerCase()} export and one {labels.right.toLowerCase()} export for the
        same month. Nothing is saved until you confirm how their columns should be read.
        {labels.type === "payout_deposit"
          ? " Only the rules read these files; no language model is used for this type."
          : ""}
      </p>
      <label className="field">
        <span>{labels.left} export (CSV)</span>
        <input type="file" accept=".csv,text/csv" onChange={(e) => setInvoices(e.target.files?.[0] ?? null)} />
      </label>
      <label className="field">
        <span>{labels.right} export (CSV)</span>
        <input type="file" accept=".csv,text/csv" onChange={(e) => setPayments(e.target.files?.[0] ?? null)} />
      </label>
      <label className="field">
        <span>Month</span>
        <input type="month" value={period} onChange={(e) => setPeriod(e.target.value)} required />
      </label>
      {propose.error ? (
        <p className="error-text" role="alert">
          {propose.error.message}
        </p>
      ) : null}
      <div className="actions">
        <button type="submit" className="btn btn-primary" disabled={!invoices || !payments || !period || propose.isPending}>
          {propose.isPending ? "Reading the files…" : "Read the files"}
        </button>
      </div>
    </form>
  );
}

function Review({ proposal, onDone }: { proposal: Proposal; onDone: (result: Confirmation) => void }) {
  const [mappings, setMappings] = useState<Record<Source, MappingState>>(() => ({
    invoices: initialMapping(proposal.invoices),
    payments: initialMapping(proposal.payments),
  }));
  const [answered, setAnswered] = useState<Set<string>>(
    () => new Set([...initialAnswered("invoices", proposal.invoices), ...initialAnswered("payments", proposal.payments)]),
  );
  const [name, setName] = useState(proposal.labels.title);
  const confirm = useConfirm(proposal.id);

  const change = (source: Source, key: MappingKey, value: string) => {
    setMappings((m) => ({ ...m, [source]: { ...m[source], [key]: value } }));
    setAnswered((a) => new Set(a).add(answerKey(source, key)));
  };
  const accept = (source: Source, key: MappingKey) =>
    setAnswered((a) => new Set(a).add(answerKey(source, key)));

  const complete = SOURCES.every((s) => isComplete(mappings[s]));
  const body = useMemo(
    () => (complete ? { invoices: toBody(mappings.invoices), payments: toBody(mappings.payments) } : null),
    [complete, mappings],
  );
  const preview = usePreview(proposal.id, body);

  const open = SOURCES.flatMap((s) =>
    reviewKeys(s)
      .map((k) => answerKey(s, k))
      .filter((k) => !answered.has(k)),
  );
  const ready = preview.data?.ready === true && open.length === 0 && name.trim() !== "";

  return (
    <div className="setup-review">
      <Provenance proposal={proposal} />
      {SOURCES.map((s) => (
        <MappingCard
          key={s}
          source={s}
          proposal={proposal[s]}
          dateFormats={proposal.date_formats}
          mapping={mappings[s]}
          answered={answered}
          onChange={(k, v) => change(s, k, v)}
          onAccept={(k) => accept(s, k)}
        />
      ))}
      <PreviewPanel complete={complete} loading={preview.isFetching} data={preview.data} error={preview.error?.message} />
      <form
        className="setup-confirm"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready && body)
            confirm.mutate({ name: name.trim(), ...body }, { onSuccess: onDone });
        }}
      >
        <label className="field">
          <span>Name of this reconciliation</span>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
        </label>
        {open.length > 0 ? (
          <p className="secondary" role="status">
            {open.length === 1 ? "1 item still needs your answer." : `${open.length} items still need your answer.`}
          </p>
        ) : null}
        {confirm.error ? (
          <p className="error-text" role="alert">
            {confirm.error.message}
          </p>
        ) : null}
        <div className="actions">
          <button type="submit" className="btn btn-primary" disabled={!ready || confirm.isPending}>
            {confirm.isPending ? "Reconciling…" : "Create and reconcile"}
          </button>
        </div>
      </form>
    </div>
  );
}

function Provenance({ proposal }: { proposal: Proposal }) {
  const withModel = proposal.mapper !== "rules";
  return (
    <p className="notice">
      {withModel
        ? `Suggested by rules that read your values, together with ${proposal.model ?? "a language model"}. Where the two disagree, nothing is chosen for you.`
        : "Suggested by rules that read your values. No language model is configured, so nothing here was generated."}
    </p>
  );
}

function PreviewPanel({
  complete,
  loading,
  data,
  error,
}: {
  complete: boolean;
  loading: boolean;
  data: { invoices: FilePreview; payments: FilePreview } | undefined;
  error: string | undefined;
}) {
  if (!complete)
    return <p className="secondary">Choose a column for every field to see how your files will be read.</p>;
  if (error)
    return (
      <p className="error-text" role="alert">
        {error}
      </p>
    );
  if (!data) return <p className="secondary">Reading a sample…</p>;
  return (
    <div className={loading ? "refreshing" : undefined} aria-label="How your files will be read">
      {SOURCES.map((s) => (
        <PreviewTable key={s} source={s} preview={data[s]} />
      ))}
    </div>
  );
}

function PreviewTable({ source, preview }: { source: Source; preview: FilePreview }) {
  const labels = useTypeLabels();
  const title = `${source === "invoices" ? labels.left : labels.right} file`;
  if (preview.error)
    return (
      <div className="notice notice-critical" role="alert">
        <AlertIcon /> {title}: {preview.error}
      </div>
    );
  const bad = preview.rejected > 0;
  return (
    <div>
      <h4 className="section-title">{title}, as it will be read</h4>
      <p className={bad ? "notice notice-warning" : "secondary"}>
        {bad ? <AlertIcon /> : <CheckCircleIcon />} {preview.records_read} records read, {preview.rejected} rows rejected.
        {preview.rejections.map((r) => ` Row ${r.row}: ${r.reason}.`)}
      </p>
      {preview.sample.length > 0 ? (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Row</th>
                <th scope="col">Id</th>
                <th scope="col">Customer</th>
                <th scope="col">Date</th>
                <th scope="col" className="num">
                  Amount
                </th>
                {source === "payments" ? <th scope="col">Reference</th> : null}
              </tr>
            </thead>
            <tbody>
              {preview.sample.map((r) => (
                <tr key={r.row}>
                  <td>{r.row}</td>
                  <td className="mono">{r.record_id}</td>
                  <td>{r.counterparty}</td>
                  <td className="nowrap">{formatDate(r.occurred_on)}</td>
                  <td className="num nowrap">{r.amount.display}</td>
                  {source === "payments" ? <td className="mono">{r.reference ?? "–"}</td> : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
