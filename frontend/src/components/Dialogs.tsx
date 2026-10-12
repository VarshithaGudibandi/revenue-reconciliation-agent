import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, type AcceptReason, type Upload } from "../api/client";
import { useRefreshAfterChange } from "../api/hooks";
import { REASON_LABELS } from "../lib/labels";
import { CloseIcon } from "./Icons";

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div
      className="overlay center"
      onClick={onClose}
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
    >
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="drawer-head">
          <h2>{title}</h2>
          <button type="button" className="btn btn-quiet" aria-label="Close" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function AcceptDialog({
  onSubmit,
  onClose,
  busy,
}: {
  onSubmit: (reason: AcceptReason, note: string) => void;
  onClose: () => void;
  busy: boolean;
}) {
  const [reason, setReason] = useState<AcceptReason>("timing_difference");
  const [note, setNote] = useState("");
  return (
    <Modal title="Accept this difference" onClose={onClose}>
      <p className="secondary">
        Use this for a difference that is real and needs no fix. The case comes back if the data
        behind it changes, and the data resolves it if it starts to reconcile.
      </p>
      <form
        style={{ display: "grid", gap: 14 }}
        onSubmit={(e) => {
          e.preventDefault();
          if (note.trim()) onSubmit(reason, note.trim());
        }}
      >
        <label className="field">
          <span>Reason</span>
          <select className="select" value={reason} onChange={(e) => setReason(e.target.value as AcceptReason)}>
            {Object.entries(REASON_LABELS).map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Note (required)</span>
          <textarea className="input" value={note} onChange={(e) => setNote(e.target.value)} autoFocus />
        </label>
        <div className="actions">
          <button type="submit" className="btn btn-primary" disabled={busy || !note.trim()}>
            Accept difference
          </button>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function UploadDialog({
  profileId,
  period,
  onClose,
}: {
  profileId: string;
  period: string;
  onClose: () => void;
}) {
  const [source, setSource] = useState("payments");
  const [file, setFile] = useState<File | null>(null);
  const refresh = useRefreshAfterChange();
  const upload = useMutation<Upload, Error, void>({
    mutationFn: () => api.upload(profileId, period, source, file!),
    onSuccess: () => void refresh(),
  });
  const result = upload.data;

  return (
    <Modal title="Add a file" onClose={onClose}>
      <p className="secondary">
        Upload a new export of the source. Records that did not change keep their result; only the
        customers whose records changed are matched again.
      </p>
      <label className="field">
        <span>Source</span>
        <select className="select" value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="invoices">Invoices</option>
          <option value="payments">Payments</option>
        </select>
      </label>
      <label className="field">
        <span>CSV file</span>
        <input
          className="input"
          type="file"
          accept=".csv,text/csv"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      </label>
      {upload.error ? (
        <p className="error-text" role="alert">
          {upload.error.message}
        </p>
      ) : null}
      {result ? (
        <p role="status">
          {result.created ? `Registered version ${result.file.version_number}.` : "This is the same content as the latest version; nothing changed."}
          {result.cases?.applied
            ? ` ${result.cases.resolved} resolved, ${result.cases.opened} opened, ${result.cases.updated} updated.`
            : ""}
        </p>
      ) : null}
      <div className="actions">
        <button
          type="button"
          className="btn btn-primary"
          disabled={!file || upload.isPending}
          onClick={() => upload.mutate()}
        >
          {upload.isPending ? "Uploading…" : "Upload"}
        </button>
        <button type="button" className="btn" onClick={onClose}>
          {result ? "Done" : "Cancel"}
        </button>
      </div>
    </Modal>
  );
}
