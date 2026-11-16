import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentClient, ApiError, newSessionId, toApiError } from "./api";
import { rolesFor } from "./auth";
import { validateConfig, type AppConfig } from "./config";
import { formatMoney, renderAssistantText } from "./format";

const CFG: AppConfig = {
  environment: "dev",
  region: "us-east-1",
  userPoolId: "us-east-1_abc",
  userPoolClientId: "client",
  apiUrl: "https://api.example.com/dev/",
  agentRuntimeArn: "arn:aws:bedrock-agentcore:us-east-1:111:runtime/rt-1",
  selfSignUp: false,
};

describe("renderAssistantText", () => {
  it("never turns model output into HTML", () => {
    const evil = '<img src=x onerror="alert(1)"> <script>alert(2)</script>';
    const { container } = render(<div>{renderAssistantText(evil)}</div>);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
  });

  it("supports bold and line breaks only", () => {
    const { container } = render(<div>{renderAssistantText("**ORD-1001** is late\nTrack it")}</div>);
    expect(container.querySelector("strong")?.textContent).toBe("ORD-1001");
    expect(container.querySelectorAll("span.block")).toHaveLength(2);
  });
});

describe("validateConfig", () => {
  it("accepts a complete config and normalizes the API url", () => {
    expect(validateConfig({ ...CFG, apiUrl: "https://api.example.com/dev" }).apiUrl).toBe(
      "https://api.example.com/dev/",
    );
  });
  it.each(["userPoolId", "apiUrl", "agentRuntimeArn"])("rejects missing %s", (key) => {
    expect(() => validateConfig({ ...CFG, [key]: "" })).toThrow(key);
  });
  it("rejects a non-https API", () => {
    expect(() => validateConfig({ ...CFG, apiUrl: "http://api.example.com/" })).toThrow("https");
  });
});

describe("rolesFor", () => {
  it("maps Cognito groups to UI roles", () => {
    expect(rolesFor(["customers"])).toEqual(["customer"]);
    expect(rolesFor(["support-agents", "approvers"])).toEqual(["support", "approver"]);
    expect(rolesFor(["admins"])).toEqual([]);
  });
});

describe("api errors", () => {
  it("uses the API's safe message", async () => {
    const resp = new Response(JSON.stringify({ error: { code: "forbidden", message: "approver role required" } }), { status: 403 });
    const err = await toApiError(resp);
    expect([err.status, err.code, err.message]).toEqual([403, "forbidden", "approver role required"]);
  });
  it("turns 401 into a sign-in message and survives non-JSON bodies", async () => {
    expect((await toApiError(new Response("nope", { status: 401 }))).message).toMatch(/sign in/);
    expect((await toApiError(new Response("<html>", { status: 502 }))).message).toMatch(/502/);
  });
});

describe("AgentClient", () => {
  afterEach(() => vi.restoreAllMocks());

  it("session ids satisfy AgentCore's 33-character minimum", () => {
    expect(newSessionId().length).toBeGreaterThanOrEqual(33);
    expect(newSessionId()).not.toBe(newSessionId());
  });

  it("sends the bearer token and session header to the runtime endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ response: "Hi", request_id: "r1", escalation_suggested: false })),
    );
    const client = new AgentClient(CFG, async () => "tok");
    const reply = await client.send("chat-" + "x".repeat(36), "hello");
    expect(reply.response).toBe("Hi");
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe(
      "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A111%3Aruntime%2Frt-1/invocations?qualifier=DEFAULT",
    );
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer tok");
    expect(headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]).toHaveLength(41);
  });

  it("surfaces agent-level errors returned with HTTP 200", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "rejected", message: "Message is too long." } })),
    );
    const client = new AgentClient(CFG, async () => "tok");
    await expect(client.send("s".repeat(40), "x")).rejects.toEqual(
      new ApiError("Message is too long.", 200, "rejected"),
    );
  });
});

describe("formatMoney", () => {
  it("formats INR", () => {
    expect(formatMoney("74999", "INR")).toMatch(/74,999/);
  });
});
