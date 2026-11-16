import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentClient, BusinessApi } from "../lib/api";
import { ChatPage } from "./ChatPage";

const REPLY = {
  response: "Your laptop (ProBook 15 Laptop) with order ID ORD-1001 is in transit but delayed.",
  request_id: "req-1",
  escalation_suggested: false,
};
const ORDERS = {
  orders: [
    {
      order_id: "ORD-1001",
      status: "SHIPPED",
      shipment_status: "IN_TRANSIT_DELAYED",
      carrier: "BlueDart",
      tracking_number: "BD123456789",
      promised_delivery_at: "2026-11-15T09:00:00Z",
      delivered_at: null,
      is_overdue: true,
      days_overdue: 1,
      category: "electronics",
      item_names: ["ProBook 15 Laptop"],
    },
  ],
};

describe("ChatPage", () => {
  afterEach(() => vi.restoreAllMocks());

  // Regression: recent Chrome returns a Promise from scrollIntoView. An effect that returned it
  // made React call the Promise as a cleanup function on the first send, blanking the page.
  it("keeps rendering after a reply when scrollIntoView returns a Promise", async () => {
    Element.prototype.scrollIntoView = vi.fn(() => Promise.resolve()) as never;
    const agent = { send: vi.fn().mockResolvedValue(REPLY) } as unknown as AgentClient;
    const api = { listOrders: vi.fn().mockResolvedValue(ORDERS) } as unknown as BusinessApi;

    render(<ChatPage agent={agent} api={api} />);
    const box = screen.getByLabelText("Message");
    fireEvent.change(box, { target: { value: "Where is my laptop?" } });
    await act(async () => {
      fireEvent.submit(box.closest("form")!);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(screen.getByText("Where is my laptop?")).toBeTruthy();
    expect(screen.getByText(/with order ID ORD-1001 is in transit/)).toBeTruthy();
  });
});
