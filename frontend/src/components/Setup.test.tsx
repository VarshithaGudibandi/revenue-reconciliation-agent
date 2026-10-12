import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { mockApi, previewFixture, proposalFixture, renderWithClient } from "../test/helpers";
import { Setup } from "./Setup";

const confirmation = {
  profile: { id: "profile-9", name: "Spanish ERP", reconciliation_type: "invoice_payment" },
  period: "2026-01",
  created: true,
  records: { invoices: 48, payments: 125 },
  rejected: { invoices: 0, payments: 0 },
  run: null,
  cases: null,
};

function routes(extra: Parameters<typeof mockApi>[0] = {}) {
  return mockApi({
    "POST /api/setup/proposals": { body: proposalFixture() },
    "POST /api/setup/proposals/proposal-1/preview": { body: previewFixture() },
    "POST /api/setup/proposals/proposal-1/confirm": { body: confirmation },
    ...extra,
  });
}

async function readFiles() {
  await userEvent.upload(
    screen.getByLabelText("Invoice export (CSV)"),
    new File(["a,b\n1,2\n"], "invoices.csv", { type: "text/csv" }),
  );
  await userEvent.upload(
    screen.getByLabelText("Payment export (CSV)"),
    new File(["a,b\n1,2\n"], "payments.csv", { type: "text/csv" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "Read the files" }));
}

const create = () => screen.getByRole("button", { name: "Create and reconcile" });

describe("Setup", () => {
  it("cannot read the files until both are chosen", async () => {
    routes();
    renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);

    expect(screen.getByRole("button", { name: "Read the files" })).toBeDisabled();
    await userEvent.upload(
      screen.getByLabelText("Invoice export (CSV)"),
      new File(["x"], "i.csv", { type: "text/csv" }),
    );
    expect(screen.getByRole("button", { name: "Read the files" })).toBeDisabled();
  });

  it("sends both files and the month, and says who made the proposal", async () => {
    const { calls } = routes();
    renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);

    await userEvent.clear(screen.getByLabelText("Month"));
    await userEvent.type(screen.getByLabelText("Month"), "2026-01");
    await readFiles();

    expect(await screen.findByText(/No language model is configured/)).toBeInTheDocument();
    const body = calls[0]!.body as FormData;
    expect(body.get("period")).toBe("2026-01");
    expect((body.get("invoices") as File).name).toBe("invoices.csv");
    expect((body.get("payments") as File).name).toBe("payments.csv");
  });

  it("names the model when one took part, and says disagreements are left to the user", async () => {
    routes({
      "POST /api/setup/proposals": {
        body: proposalFixture({ mapper: "rules and model", model: "claude-opus-5-5" }),
      },
    });
    renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);

    await readFiles();

    expect(await screen.findByText(/claude-opus-5-5/)).toBeInTheDocument();
    expect(screen.getByText(/nothing is chosen for you/)).toBeInTheDocument();
  });

  it("will not create anything while a question is open, and counts what is left", async () => {
    const { calls } = routes();
    renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);
    await readFiles();

    await screen.findByText("2 items still need your answer.");
    expect(create()).toBeDisabled();
    expect(screen.getByText("Choose a column for every field to see how your files will be read.")).toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith("/preview"))).toBe(false);
  });

  it("previews the mapping as chosen, and enables creating once everything is answered", async () => {
    const { calls } = routes();
    renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);
    await readFiles();
    await screen.findByText("2 items still need your answer.");

    await userEvent.selectOptions(screen.getByLabelText("Invoice total", { selector: "#invoices-amount" }), "gross");
    expect(screen.getByText("1 item still needs your answer.")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Date format", { selector: "#invoices-date_format" }), "%d/%m/%Y");

    await screen.findByText(/48 records read, 0 rows rejected/);
    await waitFor(() => expect(create()).toBeEnabled());
    const preview = calls.filter((c) => c.path.endsWith("/preview")).at(-1)!;
    expect(preview.body).toMatchObject({
      invoices: {
        amount: "gross",
        date_format: "%d/%m/%Y",
        decimal_separator: ".",
        reference: null,
        text: "desc",
      },
      payments: { reference: "ref", date_format: "%m/%d/%Y", text: null },
    });
    expect(screen.getByText("INV-000001")).toBeInTheDocument();
    expect(screen.getByText("INV000001")).toBeInTheDocument();
  });

  it("creates the reconciliation with the confirmed mapping and the chosen name", async () => {
    const onDone = vi.fn();
    const { calls } = routes();
    renderWithClient(<Setup firstTime onDone={onDone} onCancel={() => {}} />);
    await readFiles();
    await screen.findByText("2 items still need your answer.");
    await userEvent.selectOptions(screen.getByLabelText("Invoice total", { selector: "#invoices-amount" }), "gross");
    await userEvent.selectOptions(screen.getByLabelText("Date format", { selector: "#invoices-date_format" }), "%d/%m/%Y");
    await waitFor(() => expect(create()).toBeEnabled());

    await userEvent.clear(screen.getByLabelText("Name of this reconciliation"));
    await userEvent.type(screen.getByLabelText("Name of this reconciliation"), "Spanish ERP");
    await userEvent.click(create());

    await waitFor(() => expect(onDone).toHaveBeenCalledOnce());
    expect(onDone.mock.calls[0]![0].profile.id).toBe("profile-9");
    const confirmed = calls.find((c) => c.path.endsWith("/confirm"))!;
    expect(confirmed.body).toMatchObject({ name: "Spanish ERP", invoices: { amount: "gross" } });
  });

  it("can accept a suggestion without changing it", async () => {
    const suggested = proposalFixture();
    const amount = (suggested.invoices as { fields: { field: string; column: string | null }[] }).fields.find(
      (f) => f.field === "amount",
    )!;
    amount.column = "gross";
    routes({ "POST /api/setup/proposals": { body: suggested } });
    renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);
    await readFiles();
    await screen.findByText("2 items still need your answer.");

    const row = screen.getByLabelText("Invoice total", { selector: "#invoices-amount" }).closest("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: "This is right" }));

    expect(screen.getByText("1 item still needs your answer.")).toBeInTheDocument();
    expect(within(row).getByText("Checked by you")).toBeInTheDocument();
  });

  it("shows a name that is already taken as an error and keeps the form", async () => {
    routes({
      "POST /api/setup/proposals/proposal-1/confirm": {
        status: 409,
        body: { error: { code: "profile_exists", message: "a profile named 'X' already exists" } },
      },
    });
    renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);
    await readFiles();
    await screen.findByText("2 items still need your answer.");
    await userEvent.selectOptions(screen.getByLabelText("Invoice total", { selector: "#invoices-amount" }), "gross");
    await userEvent.selectOptions(screen.getByLabelText("Date format", { selector: "#invoices-date_format" }), "%d/%m/%Y");
    await waitFor(() => expect(create()).toBeEnabled());

    await userEvent.click(create());

    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
    expect(create()).toBeEnabled();
  });

  it("shows why a file could not be read", async () => {
    routes({
      "POST /api/setup/proposals": {
        status: 422,
        body: { error: { code: "unreadable_file", message: "invoices.csv is empty" } },
      },
    });
    renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);

    await readFiles();

    expect(await screen.findByRole("alert")).toHaveTextContent("invoices.csv is empty");
    expect(screen.getByRole("button", { name: "Read the files" })).toBeEnabled();
  });

  it("explains a column that cannot be read, in the preview, without hiding the form", async () => {
    const broken = previewFixture(false, "columns not found in invoices.csv: gross");
    routes({ "POST /api/setup/proposals/proposal-1/preview": { body: broken } });
    renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);
    await readFiles();
    await screen.findByText("2 items still need your answer.");
    await userEvent.selectOptions(screen.getByLabelText("Invoice total", { selector: "#invoices-amount" }), "gross");
    await userEvent.selectOptions(screen.getByLabelText("Date format", { selector: "#invoices-date_format" }), "%d/%m/%Y");

    expect(await screen.findByText(/columns not found in invoices.csv: gross/)).toBeInTheDocument();
    expect(create()).toBeDisabled();
  });

  it("offers cancelling only when there is something to go back to", async () => {
    routes();
    const { unmount } = renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    unmount();

    renderWithClient(<Setup firstTime={false} onDone={() => {}} onCancel={() => {}} />);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("reads deposits and payouts when that is what is chosen, in their words, with the rules only", async () => {
    const { calls } = routes({
      "POST /api/setup/proposals": {
        body: proposalFixture({
          labels: {
            type: "payout_deposit",
            title: "Bank deposits and processor payouts",
            left: "Deposit",
            left_plural: "deposits",
            right: "Payout",
            right_plural: "payouts",
          },
        }),
      },
    });
    renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);

    await userEvent.selectOptions(screen.getByLabelText("What to reconcile"), "payout_deposit");
    expect(screen.getByText(/no language model is used for this type/)).toBeInTheDocument();
    await userEvent.upload(
      screen.getByLabelText("Deposit export (CSV)"),
      new File(["a,b\n1,2\n"], "deposits.csv", { type: "text/csv" }),
    );
    await userEvent.upload(
      screen.getByLabelText("Payout export (CSV)"),
      new File(["a,b\n1,2\n"], "payouts.csv", { type: "text/csv" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Read the files" }));

    expect(await screen.findByText("Deposit file")).toBeInTheDocument();
    expect(screen.getByText("Payout file")).toBeInTheDocument();
    expect(screen.getByLabelText("Name of this reconciliation")).toHaveValue("Bank deposits and processor payouts");
    expect((calls[0]!.body as FormData).get("type")).toBe("payout_deposit");
    expect(screen.getByText("Bank transaction id")).toBeInTheDocument();
    expect(screen.getByText("Deposit reference")).toBeInTheDocument();
  });

  it("asks for invoices and payments by default", async () => {
    routes();
    renderWithClient(<Setup firstTime onDone={() => {}} onCancel={() => {}} />);

    expect(screen.getByLabelText("What to reconcile")).toHaveValue("invoice_payment");
    expect(screen.queryByText(/no language model is used for this type/)).not.toBeInTheDocument();
  });
});
