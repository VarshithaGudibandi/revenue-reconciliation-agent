import { expect, test } from "@playwright/test";
import { fileURLToPath } from "node:url";

const fixture = (name: string) => fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url));

// The fixtures come from:
//   python -m recon.datagen --out DIR --seed 5 --invoices 120 --first-days 12 \
//     --invoice-style spanish --payment-style bank_statement --no-corrections
// Only invoices from days 1-12 are kept, so the date order of the invoice file cannot be told
// from its values and the mapper has to ask. This test creates a profile with a fixed name, so
// it needs a server that has not run it before.

test("sets up a reconciliation from two files and answers what the values cannot settle", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto("/");
  await expect(page.getByText("of 400 invoices")).toBeVisible();

  await page.getByRole("button", { name: "New reconciliation" }).click();
  await page.getByLabel("Invoice export (CSV)").setInputFiles(fixture("spanish-invoices.csv"));
  await page.getByLabel("Payment export (CSV)").setInputFiles(fixture("bank-statement-payments.csv"));
  await page.getByLabel("Month").fill("2026-01");
  await page.getByRole("button", { name: "Read the files" }).click();

  await expect(page.getByText("Invoice file", { exact: true })).toBeVisible();
  await expect(page.getByText("Needs your answer").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Create and reconcile" })).toBeDisabled();
  await expect(page.getByLabel("Invoice number")).toHaveValue("N.º de factura");
  await expect(page.getByLabel("Payment date")).toHaveValue("Booking Date");

  // the two things the data cannot settle: which amount is the total, and the date order
  await page.locator("#invoices-amount").selectOption("Importe total");
  await expect(page.getByRole("button", { name: "Create and reconcile" })).toBeDisabled();
  await page.locator("#invoices-date_format").selectOption("%d/%m/%Y");

  await expect(page.getByText("48 records read, 0 rows rejected.")).toBeVisible();
  await expect(page.getByText("125 records read, 0 rows rejected.")).toBeVisible();
  await page.getByLabel("Name of this reconciliation").fill("Spanish ERP and bank");
  await page.getByRole("button", { name: "Create and reconcile" }).click();

  await expect(page.getByText("of 48 invoices")).toBeVisible();
  await expect(page.getByLabel("Profile").locator("option:checked")).toHaveText("Spanish ERP and bank");
  expect(errors).toEqual([]);
});

test("a wrong choice shows up as rejected rows before anything is saved", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New reconciliation" }).click();
  await page.getByLabel("Invoice export (CSV)").setInputFiles(fixture("spanish-invoices.csv"));
  await page.getByLabel("Payment export (CSV)").setInputFiles(fixture("bank-statement-payments.csv"));
  await page.getByLabel("Month").fill("2026-02");
  await page.getByRole("button", { name: "Read the files" }).click();
  await expect(page.getByText("Invoice file", { exact: true })).toBeVisible();

  await page.locator("#invoices-amount").selectOption("Importe total");
  await page.locator("#invoices-date_format").selectOption("%d/%m/%Y");
  await expect(page.getByText("48 records read, 0 rows rejected.")).toBeVisible();
  await page.locator("#invoices-decimal_separator").selectOption(".");

  await expect(page.getByText("0 records read, 48 rows rejected.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Create and reconcile" })).toBeDisabled();
});

test("the setup screen fits a phone without sideways scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "New reconciliation" }).click();
  await page.getByLabel("Invoice export (CSV)").setInputFiles(fixture("spanish-invoices.csv"));
  await page.getByLabel("Payment export (CSV)").setInputFiles(fixture("bank-statement-payments.csv"));
  await page.getByLabel("Month").fill("2026-04");
  await page.getByRole("button", { name: "Read the files" }).click();
  await expect(page.getByText("Invoice file", { exact: true })).toBeVisible();
  await page.locator("#invoices-amount").selectOption("Importe total");
  await page.locator("#invoices-date_format").selectOption("%d/%m/%Y");
  await expect(page.getByText("48 records read, 0 rows rejected.")).toBeVisible();

  const overflows = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  expect(overflows).toBe(false);
});
