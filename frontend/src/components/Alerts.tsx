import { useState } from "react";
import type { Channel, Delivery } from "../api/client";
import {
  useChannels,
  useCreateChannel,
  useDeleteChannel,
  useDeliveries,
  useTestChannel,
  useUpdateChannel,
} from "../api/hooks";
import { formatDateTime } from "../lib/formatters";
import { AlertIcon, CheckCircleIcon } from "./Icons";

const KINDS = { slack: "Slack", teams: "Teams", email: "Email" } as const;
type Kind = keyof typeof KINDS;

const hourLabel = (hour: number) => `${String(hour).padStart(2, "0")}:00 UTC`;

function DeliveryStatus({ delivery }: { delivery: Delivery }) {
  if (delivery.status === "sent") {
    return (
      <span className="badge badge-good">
        <CheckCircleIcon />
        Sent
      </span>
    );
  }
  return (
    <span className="badge badge-critical">
      <AlertIcon />
      {delivery.status === "failed" ? "Failed" : "Sending"}
    </span>
  );
}

function ChannelRow({ channel }: { channel: Channel }) {
  const update = useUpdateChannel();
  const test = useTestChannel();
  const remove = useDeleteChannel();
  const [confirming, setConfirming] = useState(false);
  const last = test.data ?? channel.last_delivery;
  const error = update.error ?? test.error ?? remove.error;
  return (
    <li className="channel">
      <div className="channel-head">
        <strong>{channel.name}</strong>
        <span className="badge">{KINDS[channel.kind]}</span>
        <span className="mono secondary">{channel.target}</span>
      </div>
      <div className="channel-settings">
        <label className="check">
          <input
            type="checkbox"
            checked={channel.enabled}
            onChange={(e) => update.mutate({ id: channel.id, patch: { enabled: e.target.checked } })}
          />
          <span>On</span>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={channel.on_new_cases}
            onChange={(e) => update.mutate({ id: channel.id, patch: { on_new_cases: e.target.checked } })}
          />
          <span>Tell me when a run finds new exceptions</span>
        </label>
        <label className="field-inline">
          <span>Daily digest</span>
          <select
            className="select"
            aria-label={`Daily digest for ${channel.name}`}
            value={channel.digest_hour ?? ""}
            onChange={(e) =>
              update.mutate({
                id: channel.id,
                patch: { digest_hour: e.target.value === "" ? null : Number(e.target.value) },
              })
            }
          >
            <option value="">Off</option>
            {Array.from({ length: 24 }, (_, hour) => (
              <option key={hour} value={hour}>
                {hourLabel(hour)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="actions">
        <button type="button" className="btn" disabled={test.isPending} onClick={() => test.mutate(channel.id)}>
          {test.isPending ? "Sending…" : "Send a test"}
        </button>
        {confirming ? (
          <>
            <button type="button" className="btn btn-danger" onClick={() => remove.mutate(channel.id)}>
              Delete {channel.name}
            </button>
            <button type="button" className="btn btn-quiet" onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </>
        ) : (
          <button type="button" className="btn btn-quiet" onClick={() => setConfirming(true)}>
            Delete…
          </button>
        )}
        {last ? (
          <span className="secondary" role="status">
            Last message: {last.status === "sent" ? "sent" : "failed"}
            {last.sent_at ? ` ${formatDateTime(last.sent_at)}` : ""}
            {last.error ? ` — ${last.error}` : ""}
          </span>
        ) : (
          <span className="secondary">Nothing sent yet.</span>
        )}
      </div>
      {error ? (
        <p className="error-text" role="alert">
          {error.message}
        </p>
      ) : null}
    </li>
  );
}

function NewChannel() {
  const create = useCreateChannel();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<Kind>("slack");
  const [target, setTarget] = useState("");
  const [onNew, setOnNew] = useState(true);
  const [digest, setDigest] = useState("");
  return (
    <form
      className="channel-form"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate(
          {
            name: name.trim(),
            kind,
            target: target.trim(),
            enabled: true,
            on_new_cases: onNew,
            digest_hour: digest === "" ? null : Number(digest),
          },
          {
            onSuccess: () => {
              setName("");
              setTarget("");
            },
          },
        );
      }}
    >
      <h3 className="section-title">Add a channel</h3>
      <label className="field">
        <span>Name</span>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} required />
      </label>
      <label className="field">
        <span>Where</span>
        <select className="select" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
          {(Object.keys(KINDS) as Kind[]).map((k) => (
            <option key={k} value={k}>
              {KINDS[k]}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>{kind === "email" ? "Email address" : "Webhook URL"}</span>
        <input
          className="input"
          type={kind === "email" ? "email" : "url"}
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          required
        />
      </label>
      <label className="check">
        <input type="checkbox" checked={onNew} onChange={(e) => setOnNew(e.target.checked)} />
        <span>Tell me when a run finds new exceptions</span>
      </label>
      <label className="field">
        <span>Daily digest of what is open</span>
        <select className="select" value={digest} onChange={(e) => setDigest(e.target.value)}>
          <option value="">Off</option>
          {Array.from({ length: 24 }, (_, hour) => (
            <option key={hour} value={hour}>
              {hourLabel(hour)}
            </option>
          ))}
        </select>
      </label>
      <p className="chart-sub">
        A webhook URL is a secret. It is stored as typed, not encrypted, and shown here with its path hidden.
        It must be https and must not point at an internal address.
      </p>
      {create.error ? (
        <p className="error-text" role="alert">
          {create.error.message}
        </p>
      ) : null}
      <div className="actions">
        <button type="submit" className="btn btn-primary" disabled={create.isPending || !name.trim() || !target.trim()}>
          {create.isPending ? "Adding…" : "Add channel"}
        </button>
      </div>
    </form>
  );
}

export function Alerts({ onClose }: { onClose: () => void }) {
  const channels = useChannels();
  const deliveries = useDeliveries();
  return (
    <section className="card alerts" aria-label="Alerts">
      <div className="chart-head">
        <h2 className="chart-title">Alerts</h2>
        <button type="button" className="btn btn-quiet" onClick={onClose}>
          Close
        </button>
      </div>
      <p className="secondary">
        A message goes to each channel that asked for it after a run that opens or reopens exceptions, and
        once a day as a digest of what is open. Each message is sent once, even if the job that sends it
        is retried; a failure is kept with its reason.
      </p>
      {channels.error ? (
        <p className="error-text" role="alert">
          {channels.error.message}
        </p>
      ) : null}
      {channels.data && channels.data.length === 0 ? <p>No channels yet.</p> : null}
      <ul className="channels">
        {channels.data?.map((c) => (
          <ChannelRow key={c.id} channel={c} />
        ))}
      </ul>
      <NewChannel />
      {deliveries.data && deliveries.data.length > 0 ? (
        <>
          <h3 className="section-title">Recent messages</h3>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Channel</th>
                  <th scope="col">Message</th>
                  <th scope="col">Result</th>
                </tr>
              </thead>
              <tbody>
                {deliveries.data.map((d) => (
                  <tr key={d.id}>
                    <td>{formatDateTime(d.sent_at ?? d.created_at)}</td>
                    <td>{d.channel_name}</td>
                    <td>
                      {d.subject}
                      {d.error ? <div className="secondary">{d.error}</div> : null}
                    </td>
                    <td>
                      <DeliveryStatus delivery={d} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </section>
  );
}
