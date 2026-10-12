import { KIND_LABELS, STATUS_LABELS, label } from "../lib/labels";
import { AlertIcon, CheckCircleIcon, CircleIcon, ClockIcon, DashIcon } from "./Icons";

/** Status colors always come with an icon and a word, never color alone. */
export function StatusBadge({ status, unverified = false }: { status: string; unverified?: boolean }) {
  if (unverified && status === "open") {
    return (
      <span className="badge badge-critical">
        <AlertIcon />
        Open · fix not confirmed
      </span>
    );
  }
  const [icon, tone] =
    status === "resolved"
      ? [<CheckCircleIcon key="i" />, "badge-good"]
      : status === "fix_reported"
        ? [<ClockIcon key="i" />, "badge-warning"]
        : status === "accepted"
          ? [<CheckCircleIcon key="i" />, ""]
          : status === "withdrawn"
            ? [<DashIcon key="i" />, ""]
            : [<CircleIcon key="i" />, ""];
  return (
    <span className={`badge ${tone}`}>
      {icon}
      {label(STATUS_LABELS, status)}
    </span>
  );
}

export function KindLabel({ kind }: { kind: string }) {
  return <span className="kind">{label(KIND_LABELS, kind)}</span>;
}
