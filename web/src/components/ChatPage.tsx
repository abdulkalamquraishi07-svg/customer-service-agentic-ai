import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { AgentClient, ApiError, BusinessApi, newSessionId, type ShipmentView } from "../lib/api";
import { formatDate, renderAssistantText } from "../lib/format";

interface Message {
  id: string;
  role: "customer" | "assistant" | "error";
  text: string;
  escalate?: boolean;
}

const MAX_CHARS = 2000;
const TIMEOUT_MS = 120_000;
const HUMAN_REQUEST = "I'd like to talk to a human representative, please.";
const SUGGESTIONS = [
  "Where is my laptop? It hasn't arrived.",
  "I want to return my headphones.",
  "What is your return policy for electronics?",
];
const WELCOME: Message = {
  id: "welcome",
  role: "assistant",
  text: "Hi! I can check your orders and deliveries, help with returns and refunds, answer policy questions, or connect you with a person. How can I help?",
};

export function ChatPage({ agent, api }: { agent: AgentClient; api: BusinessApi }) {
  const [sessionId, setSessionId] = useState(newSessionId);
  const [messages, setMessages] = useState<Message[]>([WELCOME]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [orders, setOrders] = useState<ShipmentView[] | null>(null);
  const [ordersError, setOrdersError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const loadOrders = useCallback(() => {
    api
      .listOrders()
      .then((r) => {
        setOrders(r.orders);
        setOrdersError(null);
      })
      .catch((e: unknown) => setOrdersError(e instanceof Error ? e.message : "Could not load"));
  }, [api]);

  useEffect(loadOrders, [loadOrders]);
  // Block body on purpose: newer browsers return a Promise from scrollIntoView, and an effect
  // that returns a non-function makes React crash the whole tree on the next update.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pending]);
  useEffect(() => {
    if (!pending) return;
    setElapsed(0);
    const t = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [pending]);

  const send = async (text: string) => {
    const prompt = text.trim();
    if (!prompt || pending) return;
    setDraft("");
    setMessages((m) => [...m, { id: crypto.randomUUID(), role: "customer", text: prompt }]);
    setPending(true);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const reply = await agent.send(sessionId, prompt, controller.signal);
      setMessages((m) => [
        ...m,
        {
          id: reply.request_id,
          role: "assistant",
          text: reply.response,
          escalate: reply.escalation_suggested,
        },
      ]);
      loadOrders(); // actions (returns) may have changed what the customer sees
    } catch (e) {
      const text =
        e instanceof DOMException && e.name === "AbortError"
          ? "The assistant is taking too long. Please try again."
          : e instanceof ApiError
            ? e.message
            : "Something went wrong. Please try again.";
      setMessages((m) => [...m, { id: crypto.randomUUID(), role: "error", text }]);
    } finally {
      clearTimeout(timer);
      setPending(false);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void send(draft);
  };

  const newConversation = () => {
    setSessionId(newSessionId());
    setMessages([WELCOME]);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col md:flex-row">
      <section className="flex min-h-0 flex-1 flex-col" aria-label="Conversation">
        <div className="flex-1 space-y-3 overflow-y-auto p-4" aria-live="polite">
          {messages.map((m) => (
            <div key={m.id} className={m.role === "customer" ? "flex justify-end" : "flex"}>
              <div
                className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-[15px] leading-relaxed ${
                  m.role === "customer"
                    ? "bg-slate-900 text-white"
                    : m.role === "error"
                      ? "border border-red-200 bg-red-50 text-red-800"
                      : "border border-slate-200 bg-white"
                }`}
              >
                {m.role === "assistant" ? renderAssistantText(m.text) : m.text}
                {m.escalate && (
                  <button
                    type="button"
                    onClick={() => void send(HUMAN_REQUEST)}
                    disabled={pending}
                    className="mt-2 block rounded-md bg-amber-100 px-3 py-1.5 text-sm font-medium text-amber-900 hover:bg-amber-200"
                  >
                    Connect me with a person
                  </button>
                )}
              </div>
            </div>
          ))}
          {pending && (
            <div className="flex items-center gap-2 text-sm text-slate-500" role="status">
              <span className="flex gap-1" aria-hidden="true">
                <span className="typing-dot h-2 w-2 rounded-full bg-slate-400" />
                <span className="typing-dot h-2 w-2 rounded-full bg-slate-400" />
                <span className="typing-dot h-2 w-2 rounded-full bg-slate-400" />
              </span>
              {elapsed < 6 ? "Thinking…" : "Checking your orders and our policies…"} ({elapsed}s)
            </div>
          )}
          <div ref={bottom} />
        </div>

        {messages.length === 1 && (
          <div className="flex flex-wrap gap-2 px-4 pb-2">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => void send(s)}
                className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-100"
              >
                {s}
              </button>
            ))}
          </div>
        )}

        <form onSubmit={submit} className="flex gap-2 border-t border-slate-200 bg-white p-3">
          <label htmlFor="message" className="sr-only">
            Message
          </label>
          <textarea
            id="message"
            rows={1}
            maxLength={MAX_CHARS}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send(draft);
              }
            }}
            placeholder="Type your message…"
            className="flex-1 resize-none rounded-md border border-slate-300 px-3 py-2"
          />
          <button
            type="submit"
            disabled={pending || !draft.trim()}
            className="rounded-md bg-slate-900 px-4 py-2 font-medium text-white disabled:opacity-40"
          >
            Send
          </button>
        </form>
        <div className="flex gap-3 bg-white px-3 pb-3 text-sm">
          <button
            type="button"
            onClick={() => void send(HUMAN_REQUEST)}
            disabled={pending}
            className="text-slate-600 underline-offset-2 hover:underline disabled:opacity-40"
          >
            Talk to a human
          </button>
          <button
            type="button"
            onClick={newConversation}
            disabled={pending}
            className="text-slate-600 underline-offset-2 hover:underline disabled:opacity-40"
          >
            New conversation
          </button>
        </div>
      </section>

      <aside
        className="border-t border-slate-200 bg-white p-4 md:w-80 md:border-l md:border-t-0"
        aria-label="Your orders"
      >
        <h2 className="mb-3 font-semibold">Your orders</h2>
        {ordersError && <p className="text-sm text-red-700">{ordersError}</p>}
        {!orders && !ordersError && <p className="text-sm text-slate-500">Loading…</p>}
        {orders?.length === 0 && <p className="text-sm text-slate-500">No orders yet.</p>}
        <ul className="space-y-3">
          {orders?.map((o) => (
            <li key={o.order_id} className="rounded-lg border border-slate-200 p-3 text-sm">
              <div className="flex items-center justify-between">
                <span className="font-medium">{o.item_names.join(", ") || o.order_id}</span>
                <StatusBadge order={o} />
              </div>
              <p className="mt-1 text-slate-500">{o.order_id}</p>
              <p className="text-slate-500">
                {o.delivered_at
                  ? `Delivered ${formatDate(o.delivered_at)}`
                  : `Promised ${formatDate(o.promised_delivery_at)}`}
              </p>
            </li>
          ))}
        </ul>
      </aside>
    </div>
  );
}

function StatusBadge({ order }: { order: ShipmentView }) {
  const [label, tone] = order.is_overdue
    ? [`${order.days_overdue}d late`, "bg-red-100 text-red-800"]
    : order.status === "DELIVERED"
      ? ["Delivered", "bg-emerald-100 text-emerald-800"]
      : order.status === "SHIPPED"
        ? ["In transit", "bg-sky-100 text-sky-800"]
        : [order.status.toLowerCase(), "bg-slate-100 text-slate-700"];
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${tone}`}>{label}</span>;
}
