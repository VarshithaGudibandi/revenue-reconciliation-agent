import { expect, test } from "@playwright/test";
import { fileURLToPath } from "node:url";

const fixture = (name: string) => fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url));

// The path of someone who starts with no sample data (RECON_DEMO=false) and brings their own
// exports. It needs a server with an empty database, so it only runs when asked:
//   E2E_EMPTY=1 E2E_BASE_URL=http://127.0.0.1:8000 npx playwright test e2e/empty-start.e2e.ts
test.skip(!process.env.E2E_EMPTY, "needs a server started with RECON_DEMO=false and an empty database");

test("opens on setup with no sample data, and a first reconciliation from the user's own files lands on the dashboard", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto("/");

  await expect(page.getByText("Set up a reconciliation")).toBeVisible();
  await expect(page.getByText("Demo data.")).toHaveCount(0);
  await expect(page.getByLabel("What to reconcile")).toHaveValue("invoice_payment");

  await page.getByLabel("Invoice export (CSV)").setInputFiles(fixture("spanish-invoices.csv"));
  await page.getByLabel("Payment export (CSV)").setInputFiles(fixture("bank-statement-payments.csv"));
  await page.getByLabel("Month").fill("2026-01");
  await page.getByRole("button", { name: "Read the files" }).click();
  await page.locator("#invoices-amount").selectOption("Importe total");
  await page.locator("#invoices-date_format").selectOption("%d/%m/%Y");
  await page.getByLabel("Name of this reconciliation").fill("My own exports");
  await page.getByRole("button", { name: "Create and reconcile" }).click();

  await expect(page.getByText("of 48 invoices")).toBeVisible();
  await expect(page.getByText("My own exports")).toBeVisible();
  await expect(page.getByText("Demo data.")).toHaveCount(0);
  await expect(page.locator("tbody tr").first()).toBeVisible();
  expect(errors).toEqual([]);
});
