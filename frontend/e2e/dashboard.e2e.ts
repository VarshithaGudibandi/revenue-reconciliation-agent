import { expect, test, type Page } from "@playwright/test";

test.describe.configure({ mode: "serial" });

let page: Page;
const consoleErrors: string[] = [];

test.beforeAll(async ({ browser }) => {
  page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  await page.goto("/");
});

test.afterAll(async () => {
  expect(consoleErrors).toEqual([]);
  await page.close();
});

test("shows how much of the month reconciled before anything is fixed", async () => {
  await expect(page.getByText("of 400 invoices")).toBeVisible();
  await expect(page.getByRole("tab", { name: /Needs attention/ })).toBeVisible();
  await expect(page.locator("tbody tr").first()).toBeVisible();
});

test("explains a case from its records and lets the user report a fix and add a note", async () => {
  await page.locator("tbody tr").first().click();
  await expect(page.getByText("What to check")).toBeVisible();

  await page.getByRole("button", { name: "I fixed this in the source" }).click();
  await expect(page.getByText(/You reported this as fixed/)).toBeVisible();

  await page.getByLabel("Add a note").fill("Asked billing to correct the reference.");
  await page.getByRole("button", { name: "Add note" }).click();
  await expect(page.getByText("Asked billing to correct the reference.")).toBeVisible();
});

test("will not accept a difference without a note, and accepts it with one", async () => {
  await page.getByRole("button", { name: "Close" }).first().click();
  await page.locator("tbody tr").nth(1).click();

  await page.getByRole("button", { name: "Accept this difference…" }).click();
  await expect(page.getByRole("button", { name: "Accept difference" })).toBeDisabled();

  await page.getByLabel("Note (required)").fill("Customer refunded on 3 Feb.");
  await page.getByRole("button", { name: "Accept difference" }).click();
  await expect(page.getByText(/Accepted as/)).toBeVisible();
  await page.getByRole("button", { name: "Close" }).first().click();
});

test("changes the needs-attention count when the corrected export arrives, and still lists the cases it did not fix", async () => {
  const before = await page.getByRole("tab", { name: /Needs attention/ }).innerText();

  await page.getByRole("button", { name: "Load corrected export" }).click();
  await expect(page.getByText("Corrected export loaded")).toBeVisible();

  await expect(page.getByRole("tab", { name: /Needs attention/ })).not.toHaveText(before);
  await page.getByRole("tab", { name: /Reconciled/ }).click();
  await expect(page.getByText("Invoice amount")).toBeVisible();
  await page.getByRole("tab", { name: /Needs attention/ }).click();
  await expect(page.locator("tbody tr").first()).toBeVisible();
});

test("works in dark theme", async () => {
  await page.getByLabel("Theme").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.getByText(/of 400 invoices/)).toBeVisible();
  await page.getByLabel("Theme").selectOption("light");
});

test("fits a phone screen without sideways scrolling, including with a case open", async () => {
  await page.setViewportSize({ width: 390, height: 844 });
  const overflows = () => page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);

  expect(await overflows()).toBe(false);
  await page.locator("tbody tr").first().click();
  await expect(page.getByText("What to check")).toBeVisible();
  expect(await overflows()).toBe(false);
});
