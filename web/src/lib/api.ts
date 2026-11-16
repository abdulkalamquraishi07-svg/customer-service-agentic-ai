/** Clients for the business API (API Gateway) and the agent runtime (AgentCore). Both send the
 * user's Cognito ID token; both endpoints validate it independently. */

import type { AppConfig } from "./config";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
  ) {
    super(message);
  }
}

export async function toApiError(resp: Response): Promise<ApiError> {
  let code = "http_error";
  let message = `Request failed (${resp.status})`;
  try {
    const body = await resp.json();
    if (body?.error?.message) {
      message = String(body.error.message);
      code = String(body.error.code ?? code);
    } else if (typeof body?.message === "string") {
      message = body.message;
    }
  } catch {
    /* non-JSON error body: keep the generic message */
  }
  if (resp.status === 401) message = "Your session has expired. Please sign in again.";
  return new ApiError(message, resp.status, code);
}

type TokenSource = () => Promise<string>;

export interface ShipmentView {
  order_id: string;
  status: string;
  shipment_status: string;
  carrier: string | null;
  tracking_number: string | null;
  promised_delivery_at: string;
  delivered_at: string | null;
  is_overdue: boolean;
  days_overdue: number;
  category: string;
  item_names: string[];
}

export interface PendingReturn {
  customer_id: string;
  return_id: string;
  order_id: string;
  reason: string;
  refund_amount: string;
  currency: string;
  created_at: string;
  approval_expires_at: string | null;
}

export interface Ticket {
  customer_id: string;
  ticket_id: string;
  status: string;
  category: string;
  subject: string;
  description: string;
  related_order_id: string | null;
  escalation_summary: string | null;
  created_at: string;
}

export class BusinessApi {
  constructor(
    private readonly cfg: AppConfig,
    private readonly token: TokenSource,
  ) {}

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const resp = await fetch(this.cfg.apiUrl + path, {
      method,
      headers: {
        Authorization: await this.token(),
        "Content-Type": "application/json",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!resp.ok) throw await toApiError(resp);
    return (await resp.json()) as T;
  }

  listOrders(): Promise<{ orders: ShipmentView[] }> {
    return this.request("GET", "v1/orders");
  }

  listApprovals(): Promise<{ approvals: PendingReturn[] }> {
    return this.request("GET", "v1/ops/approvals");
  }

  decide(r: PendingReturn, approve: boolean, note: string): Promise<{ status: string }> {
    const path = `v1/ops/returns/${encodeURIComponent(r.customer_id)}/${encodeURIComponent(
      r.return_id,
    )}/decision`;
    return this.request("POST", path, { approve, note });
  }

  listTickets(status = "ESCALATED"): Promise<{ tickets: Ticket[] }> {
    return this.request("GET", `v1/ops/tickets?status=${encodeURIComponent(status)}`);
  }

  resolveTicket(t: Ticket): Promise<Ticket> {
    const path = `v1/ops/tickets/${encodeURIComponent(t.customer_id)}/${encodeURIComponent(
      t.ticket_id,
    )}/resolve`;
    return this.request("POST", path);
  }
}

export interface AgentReply {
  response: string;
  request_id: string;
  escalation_suggested: boolean;
}

/** AgentCore requires session ids of at least 33 characters. */
export function newSessionId(): string {
  return `chat-${crypto.randomUUID()}`;
}

export class AgentClient {
  constructor(
    private readonly cfg: AppConfig,
    private readonly token: TokenSource,
  ) {}

  get endpoint(): string {
    const arn = encodeURIComponent(this.cfg.agentRuntimeArn);
    return `https://bedrock-agentcore.${this.cfg.region}.amazonaws.com/runtimes/${arn}/invocations?qualifier=DEFAULT`;
  }

  async send(sessionId: string, prompt: string, signal?: AbortSignal): Promise<AgentReply> {
    const resp = await fetch(this.endpoint, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${await this.token()}`,
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sessionId,
      },
      body: JSON.stringify({ prompt }),
      signal,
    });
    if (!resp.ok) throw await toApiError(resp);
    const body = await resp.json();
    if (body?.error) {
      throw new ApiError(String(body.error.message), 200, String(body.error.code));
    }
    if (typeof body?.response !== "string") {
      throw new ApiError("The assistant returned an unexpected response.", 200, "bad_response");
    }
    return body as AgentReply;
  }
}
