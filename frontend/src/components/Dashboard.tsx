import { useDeferredValue, useMemo, useState } from "react";
import type { CaseQuery } from "../api/client";
import { useCases, useMatches, useSummary } from "../api/hooks";
import { useSearchParam } from "../lib/browser";
import { formatCount, formatPeriod } from "../lib/formatters";
import { KIND_LABELS, STATUS_LABELS, label } from "../lib/labels";
import { LabelsProvider, capitalise, useTypeLabels } from "../lib/typeLabels";
import { BarChart, type BarDatum } from "./BarChart";
import { CaseDrawer } from "./CaseDrawer";
import { CaseTable } from "./CaseTable";
import { Hero } from "./Hero";
import { MatchTable } from "./MatchTable";
import { Pagination } from "./Pagination";
import { StatTile } from "./StatTile";

const PAGE_SIZE = 25;
const ATTENTION_STATUSES = ["open", "fix_reported", "accepted"];

type Tab = "attention" | "reconciled";

function Tabs({ tab, onChange, attention, reconciled }: { tab: Tab; onChange: (t: Tab) => void; attention: number; reconciled: number }) {
  const items: [Tab, string, number][] = [
    ["attention", "Needs attention", attention],
    ["reconciled", "Reconciled", reconciled],
  ];
  return (
    <div className="tabs" role="tablist" aria-label="Records">
      {items.map(([id, text, count]) => (
        <button
          key={id}
          type="button"
          role="tab"
          className="tab"
          aria-selected={tab === id}
          onClick={() => onChange(id)}
        >
          {text}
          <span className="count-pill">{formatCount(count)}</span>
        </button>
      ))}
    </div>
  );
}

export function Dashboard({ profileId, period }: { profileId: string; period: string }) {
  const summaryQuery = useSummary(profileId, period);
  const summary = summaryQuery.data;
  const [tabParam, setTabParam] = useSearchParam("tab");
  const [caseId, setCaseId] = useSearchParam("case");
  const tab: Tab = tabParam === "reconciled" ? "reconciled" : "attention";

  if (!summary) {
    return <p className="muted">Loading…</p>;
  }
  if (summary.state !== "reconciled") {
    const message =
      summary.state === "no_files"
        ? `There are no files for ${formatPeriod(period)} yet. Add the ${summary.labels.left_plural} and ${summary.labels.right_plural} exports to start.`
        : summary.state === "waiting_for_files"
          ? "One of the two exports is still missing. Add it and the period is reconciled automatically."
          : "The latest files have not been reconciled yet.";
    return (
      <section className="card">
        <p>{message}</p>
      </section>
    );
  }

  return (
    <LabelsProvider value={summary.labels}>
      <Hero summary={summary} />
      <Tiles summary={summary} />
      <Charts summary={summary} refreshing={summaryQuery.isFetching} />
      <Tabs
        tab={tab}
        onChange={(t) => setTabParam(t === "attention" ? null : t)}
        attention={summary.needs_attention}
        reconciled={summary.matched_invoices}
      />
      {tab === "attention" ? (
        <AttentionPanel profileId={profileId} period={period} kinds={summary.by_kind.map((k) => k.kind)} caseId={caseId} onOpen={setCaseId} />
      ) : (
        <ReconciledPanel profileId={profileId} period={period} caseId={caseId} onOpen={setCaseId} />
      )}
      {caseId ? <CaseDrawer caseId={caseId} onClose={() => setCaseId(null)} onOpenCase={setCaseId} /> : null}
    </LabelsProvider>
  );
}

function Tiles({ summary }: { summary: NonNullable<ReturnType<typeof useSummary>["data"]> }) {
  return (
    <div className="tiles">
      <StatTile label="Needs attention" value={formatCount(summary.needs_attention)} note="open or fix reported" />
      <StatTile label="Fixes not confirmed" value={formatCount(summary.unverified_fixes)} note="reported fixed, still wrong" />
      <StatTile label="Accepted" value={formatCount(summary.status_counts.accepted ?? 0)} note="real differences" />
      <StatTile label="Resolved" value={formatCount(summary.status_counts.resolved ?? 0)} note="reconciled by the data" />
      <StatTile label={capitalise(summary.labels.left_plural)} value={formatCount(summary.invoices)} />
      <StatTile label={capitalise(summary.labels.right_plural)} value={formatCount(summary.payments)} />
    </div>
  );
}

function Charts({ summary, refreshing }: { summary: NonNullable<ReturnType<typeof useSummary>["data"]>; refreshing: boolean }) {
  // The currency with the most at stake comes first, so the chart opens on the one that matters.
  const currencies = useMemo(() => {
    const totals = new Map<string, number>();
    for (const kind of summary.by_kind)
      for (const a of kind.amounts) totals.set(a.currency, (totals.get(a.currency) ?? 0) + a.minor);
    return [...totals.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([c]) => c);
  }, [summary]);
  const [chosen, setChosen] = useState<string | null>(null);
  const currency = chosen && currencies.includes(chosen) ? chosen : (currencies[0] ?? "USD");

  const counts: BarDatum[] = summary.by_kind.map((k) => ({
    key: k.kind,
    label: label(KIND_LABELS, k.kind),
    value: k.count,
    display: formatCount(k.count),
  }));
  const amounts: BarDatum[] = summary.by_kind
    .map((k) => ({ kind: k.kind, amount: k.amounts.find((a) => a.currency === currency) }))
    .filter((row): row is { kind: string; amount: NonNullable<typeof row.amount> } => !!row.amount)
    .sort((a, b) => b.amount.minor - a.amount.minor)
    .map(({ kind, amount }) => ({
      key: kind,
      label: label(KIND_LABELS, kind),
      value: amount.minor,
      display: amount.display,
    }));

  return (
    <>
      {currencies.length > 1 ? (
        <div className="filter-row">
          <label className="field">
            <span>Currency for net difference</span>
            <select className="select" value={currency} onChange={(e) => setChosen(e.target.value)}>
              {currencies.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
        </div>
      ) : null}
      <div className="charts">
        <BarChart
          title="Problems by kind"
          subtitle="Cases that still need attention"
          valueHeading="Cases"
          data={counts}
          refreshing={refreshing}
          emptyText="Nothing needs attention."
        />
        <BarChart
          title={`Net difference by kind (${currency})`}
          subtitle="Absolute amounts. A wrong reference has no net difference."
          valueHeading={`Net difference (${currency})`}
          data={amounts}
          refreshing={refreshing}
          emptyText="No differences in this currency."
        />
      </div>
    </>
  );
}

function AttentionPanel({
  profileId,
  period,
  kinds,
  caseId,
  onOpen,
}: {
  profileId: string;
  period: string;
  kinds: string[];
  caseId: string | null;
  onOpen: (id: string) => void;
}) {
  const labels = useTypeLabels();
  const [statuses, setStatuses] = useState<string[]>(["open", "fix_reported"]);
  const [kind, setKind] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<NonNullable<CaseQuery["sort"]>>("amount");
  const [offset, setOffset] = useState(0);
  const deferred = useDeferredValue(search);

  const params: CaseQuery = {
    status: statuses,
    kind: kind ? [kind] : undefined,
    q: deferred.trim() || undefined,
    sort,
    limit: PAGE_SIZE,
    offset,
  };
  const query = useCases(profileId, period, params);

  function toggle(status: string) {
    setStatuses((current) => (current.includes(status) ? current.filter((s) => s !== status) : [...current, status]));
    setOffset(0);
  }

  return (
    <section className="card" aria-label="Cases that need attention">
      <div className="filter-row" style={{ marginBottom: 16 }}>
        <div className="field">
          <span id="status-filter">Status</span>
          <div className="chips" role="group" aria-labelledby="status-filter">
            {ATTENTION_STATUSES.map((status) => (
              <button
                key={status}
                type="button"
                className="chip"
                aria-pressed={statuses.includes(status)}
                onClick={() => toggle(status)}
              >
                {label(STATUS_LABELS, status)}
              </button>
            ))}
          </div>
        </div>
        <label className="field">
          <span>Problem</span>
          <select className="select" value={kind} onChange={(e) => { setKind(e.target.value); setOffset(0); }}>
            <option value="">All problems</option>
            {kinds.map((k) => (
              <option key={k} value={k}>
                {label(KIND_LABELS, k)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Search</span>
          <input
            className="input"
            type="search"
            placeholder={`Customer, ${labels.left.toLowerCase()}, ${labels.right.toLowerCase()} or #case`}
            value={search}
            onChange={(e) => { setSearch(e.target.value); setOffset(0); }}
          />
        </label>
        <label className="field">
          <span>Sort by</span>
          <select className="select" value={sort} onChange={(e) => { setSort(e.target.value as typeof sort); setOffset(0); }}>
            <option value="amount">Largest difference</option>
            <option value="updated">Recently updated</option>
            <option value="number">Case number</option>
          </select>
        </label>
      </div>
      <div className={query.isFetching ? "refreshing" : undefined}>
        {query.error ? (
          <p className="error-text" role="alert">
            {query.error.message}
          </p>
        ) : (
          <CaseTable rows={query.data?.items ?? []} selectedId={caseId} onOpen={onOpen} />
        )}
      </div>
      <Pagination total={query.data?.total ?? 0} offset={offset} limit={PAGE_SIZE} onChange={setOffset} />
    </section>
  );
}

function ReconciledPanel({
  profileId,
  period,
  caseId,
  onOpen,
}: {
  profileId: string;
  period: string;
  caseId: string | null;
  onOpen: (id: string) => void;
}) {
  const labels = useTypeLabels();
  const [view, setView] = useState<"matched" | "resolved">("matched");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const deferred = useDeferredValue(search);

  const matches = useMatches(profileId, period, { q: deferred.trim() || undefined, limit: PAGE_SIZE, offset }, view === "matched");
  const resolved = useCases(profileId, period, {
    status: ["resolved"],
    q: deferred.trim() || undefined,
    sort: "updated",
    limit: PAGE_SIZE,
    offset,
  });
  const active = view === "matched" ? matches : resolved;
  const total = active.data?.total ?? 0;

  return (
    <section className="card" aria-label="Reconciled records">
      <div className="filter-row" style={{ marginBottom: 16 }}>
        <div className="field">
          <span id="reconciled-view">Show</span>
          <div className="chips" role="group" aria-labelledby="reconciled-view">
            <button type="button" className="chip" aria-pressed={view === "matched"} onClick={() => { setView("matched"); setOffset(0); }}>
              Matched {labels.left_plural}
            </button>
            <button type="button" className="chip" aria-pressed={view === "resolved"} onClick={() => { setView("resolved"); setOffset(0); }}>
              Resolved cases
            </button>
          </div>
        </div>
        <label className="field">
          <span>Search</span>
          <input
            className="input"
            type="search"
            placeholder="Customer or id"
            value={search}
            onChange={(e) => { setSearch(e.target.value); setOffset(0); }}
          />
        </label>
      </div>
      <div className={active.isFetching ? "refreshing" : undefined}>
        {active.isPending ? (
          <p className="muted">Loading…</p>
        ) : view === "matched" ? (
          <MatchTable rows={matches.data?.items ?? []} />
        ) : (
          <CaseTable rows={resolved.data?.items ?? []} selectedId={caseId} onOpen={onOpen} />
        )}
      </div>
      <Pagination total={total} offset={offset} limit={PAGE_SIZE} onChange={setOffset} />
    </section>
  );
}
