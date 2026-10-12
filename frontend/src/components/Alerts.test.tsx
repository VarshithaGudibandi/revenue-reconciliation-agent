import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockApi, renderWithClient } from "../test/helpers";
import { Alerts } from "./Alerts";

const channel = (over: Record<string, unknown> = {}) => ({
  id: "c1",
  name: "ops",
  kind: "slack",
  target: "https://hooks.slack.com/…",
  enabled: true,
  on_new_cases: true,
  digest_hour: null,
  created_at: "2026-10-12T10:00:00Z",
  last_delivery: null,
  ...over,
});

const delivery = (over: Record<string, unknown> = {}) => ({
  id: "d1",
  channel_id: "c1",
  channel_name: "ops",
  kind: "test",
  subject: "Test message from the revenue reconciliation agent",
  status: "sent",
  attempts: 1,
  error: null,
  created_at: "2026-10-12T10:05:00Z",
  sent_at: "2026-10-12T10:05:01Z",
  ...over,
});

const view = () => renderWithClient(<Alerts onClose={() => {}} />);

describe("Alerts", () => {
  it("says when there are no channels and explains when messages are sent", async () => {
    mockApi({ "GET /api/alert-channels": { body: [] }, "GET /api/alert-deliveries": { body: [] } });
    view();

    expect(await screen.findByText("No channels yet.")).toBeInTheDocument();
    expect(screen.getByText(/Each message is sent once/)).toBeInTheDocument();
  });

  it("shows a channel with its masked target and what it has sent", async () => {
    mockApi({
      "GET /api/alert-channels": { body: [channel({ last_delivery: delivery({ status: "failed", error: "the webhook answered 404", sent_at: null }) })] },
      "GET /api/alert-deliveries": { body: [] },
    });
    view();

    expect(await screen.findByText("ops")).toBeInTheDocument();
    expect(screen.getByText("https://hooks.slack.com/…")).toBeInTheDocument();
    expect(screen.getByText(/Last message: failed — the webhook answered 404/)).toBeInTheDocument();
  });

  it("adds a channel with the options chosen, and says plainly what happens to the webhook", async () => {
    const { calls } = mockApi({
      "GET /api/alert-channels": { body: [] },
      "GET /api/alert-deliveries": { body: [] },
      "POST /api/alert-channels": { status: 201, body: channel() },
    });
    view();
    await screen.findByText("No channels yet.");

    await userEvent.type(screen.getByLabelText("Name"), "ops");
    await userEvent.type(screen.getByLabelText("Webhook URL"), "https://hooks.slack.com/services/T/B/x");
    await userEvent.selectOptions(screen.getByLabelText("Daily digest of what is open"), "8");
    await userEvent.click(screen.getByRole("button", { name: "Add channel" }));

    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "ops",
      kind: "slack",
      target: "https://hooks.slack.com/services/T/B/x",
      enabled: true,
      on_new_cases: true,
      digest_hour: 8,
    });
    expect(screen.getByText(/stored as typed, not encrypted/)).toBeInTheDocument();
  });

  it("asks for an email address, not a URL, when the channel is email", async () => {
    mockApi({ "GET /api/alert-channels": { body: [] }, "GET /api/alert-deliveries": { body: [] } });
    view();
    await screen.findByText("No channels yet.");

    await userEvent.selectOptions(screen.getByLabelText("Where"), "email");

    expect(screen.getByLabelText("Email address")).toHaveAttribute("type", "email");
    expect(screen.queryByLabelText("Webhook URL")).not.toBeInTheDocument();
  });

  it("shows the server's reason when a channel is refused", async () => {
    mockApi({
      "GET /api/alert-channels": { body: [] },
      "GET /api/alert-deliveries": { body: [] },
      "POST /api/alert-channels": {
        status: 422,
        body: { error: { code: "invalid_request", message: "the host 'x' resolves to 10.0.0.1, which is not a public address" } },
      },
    });
    view();
    await screen.findByText("No channels yet.");

    await userEvent.type(screen.getByLabelText("Name"), "ops");
    await userEvent.type(screen.getByLabelText("Webhook URL"), "https://x.example/hook");
    await userEvent.click(screen.getByRole("button", { name: "Add channel" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("not a public address");
  });

  it("turns the digest off by sending null, and switches a channel off", async () => {
    const { calls } = mockApi({
      "GET /api/alert-channels": { body: [channel({ digest_hour: 7 })] },
      "GET /api/alert-deliveries": { body: [] },
      "PATCH /api/alert-channels/c1": { body: channel() },
    });
    view();
    const row = (await screen.findByText("ops")).closest("li") as HTMLElement;

    await userEvent.selectOptions(within(row).getByLabelText("Daily digest for ops"), "");
    await userEvent.click(within(row).getByLabelText("On"));

    await waitFor(() => expect(calls.filter((c) => c.method === "PATCH")).toHaveLength(2));
    const bodies = calls.filter((c) => c.method === "PATCH").map((c) => c.body);
    expect(bodies).toEqual([{ digest_hour: null }, { enabled: false }]);
  });

  it("sends a test and shows how it went", async () => {
    mockApi({
      "GET /api/alert-channels": { body: [channel()] },
      "GET /api/alert-deliveries": { body: [] },
      "POST /api/alert-channels/c1/test": { body: delivery({ status: "failed", error: "the webhook answered 404", sent_at: null }) },
    });
    view();

    await userEvent.click(await screen.findByRole("button", { name: "Send a test" }));

    expect(await screen.findByText(/Last message: failed — the webhook answered 404/)).toBeInTheDocument();
  });

  it("asks twice before deleting", async () => {
    const { calls } = mockApi({
      "GET /api/alert-channels": { body: [channel()] },
      "GET /api/alert-deliveries": { body: [] },
      "DELETE /api/alert-channels/c1": { status: 204, body: null },
    });
    view();

    await userEvent.click(await screen.findByRole("button", { name: "Delete…" }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Delete ops" }));

    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
  });

  it("lists the recent messages with their result in words", async () => {
    mockApi({
      "GET /api/alert-channels": { body: [channel()] },
      "GET /api/alert-deliveries": { body: [delivery(), delivery({ id: "d2", status: "failed", error: "ConnectError", sent_at: null })] },
    });
    view();

    expect(await screen.findByText("Sent")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("ConnectError")).toBeInTheDocument();
  });
});
