import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { BarChart } from "./BarChart";

const data = [
  { key: "a", label: "Missing payment", value: 20, display: "20" },
  { key: "b", label: "Amount mismatch", value: 5, display: "5" },
];

describe("BarChart", () => {
  it("shows every value at the end of its bar", () => {
    render(<BarChart title="Problems" valueHeading="Cases" data={data} />);

    expect(screen.getByLabelText("Missing payment: 20")).toBeInTheDocument();
    expect(screen.getByLabelText("Amount mismatch: 5")).toBeInTheDocument();
  });

  it("scales bars to the largest value", () => {
    const { container } = render(<BarChart title="Problems" valueHeading="Cases" data={data} />);

    const [first, second] = [...container.querySelectorAll<HTMLElement>(".bar")];
    expect(parseFloat(first!.style.width)).toBeCloseTo(78);
    expect(parseFloat(second!.style.width)).toBeCloseTo(19.5);
  });

  it("has a table view with the same numbers", async () => {
    render(<BarChart title="Problems" valueHeading="Cases" data={data} />);

    await userEvent.click(screen.getByRole("button", { name: "Table" }));

    const table = screen.getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Cases" })).toBeInTheDocument();
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.textContent)).toEqual(["Missing payment20", "Amount mismatch5"]);
  });

  it("shows a tooltip on keyboard focus as well as on hover", () => {
    render(<BarChart title="Problems" valueHeading="Cases" data={data} />);

    fireEvent.focus(screen.getByLabelText("Missing payment: 20"));

    expect(screen.getAllByText("20").length).toBeGreaterThan(1);
    fireEvent.blur(screen.getByLabelText("Missing payment: 20"));
    expect(screen.getAllByText("20")).toHaveLength(1);
  });

  it("says so when there is nothing to show", () => {
    render(<BarChart title="Problems" valueHeading="Cases" data={[]} emptyText="Nothing needs attention." />);

    expect(screen.getByText("Nothing needs attention.")).toBeInTheDocument();
  });

  it("draws a zero value as a hairline, not nothing", () => {
    const { container } = render(
      <BarChart title="Net" valueHeading="Net" data={[{ key: "z", label: "Wrong reference", value: 0, display: "0.00 USD" }]} />,
    );

    expect(container.querySelector(".bar")).toBeInTheDocument();
    expect(screen.getByText("0.00 USD")).toBeInTheDocument();
  });
});
