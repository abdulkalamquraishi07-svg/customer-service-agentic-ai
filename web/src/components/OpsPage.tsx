import { useCallback, useEffect, useState } from "react";

import { ApiError, BusinessApi, type PendingReturn, type Ticket } from "../lib/api";
import type { Role } from "../lib/auth";
import { formatDate, formatMoney, shortId } from "../lib/format";

type Tab = "approvals" | "tickets";

export function OpsPage({ api, roles }: { api: BusinessApi; roles: Role[] }) {
  const tabs: Tab[] = [
    ...(roles.includes("approver") ? (["approvals"] as Tab[]) : []),
    ...(roles.includes("support") ? (["tickets"] as Tab[]) : []),
  ];
  const [tab, setTab] = useState<Tab>(tabs[0] ?? "tickets");

  return (
    <main className="flex-1 overflow-y-auto p-4">
      <div className="mb-4 flex gap-2" role="tablist">
        {tabs.map((t) => (
          <button
            key={t}
            role="tab"
            type="button"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={`rounded-md px-3 py-1.5 text-sm font-medium ${
              tab === t ? "bg-slate-900 text-white" : "border border-slate-300 bg-white"
            }`}
          >
            {t === "approvals" ? "Refund approvals" : "Escalated tickets"}
          </button>
        ))}
      </div>
      {tab === "approvals" ? <Approvals api={api} /> : <Tickets api={api} />}
    </main>
  );
}

function useList<T>(load: () => Promise<T[]>) {
  const [items, setItems] = useState<T[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(() => {
    setError(null);
    load()
      .then(setItems)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Could not load"));
  }, [load]);
  useEffect(refresh, [refresh]);
  return { items, error, refresh };
}

function Approvals({ api }: { api: BusinessApi }) {
  const load = useCallback(() => api.listApprovals().then((r) => r.approvals), [api]);
  const { items, error, refresh } = useList<PendingReturn>(load);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const decide = async (r: PendingReturn, approve: boolean) => {
    setBusy(r.return_id);
    setMessage(null);
    try {
      const result = await api.decide(r, approve, notes[r.return_id] ?? "");
      setMessage(`${r.return_id}: ${result.status.toLowerCase().replace("_", " ")}`);
      refresh();
    } catch (e) {
      setMessage(e instanceof ApiError ? `${r.return_id}: ${e.message}` : "Decision failed");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section aria-label="Refund approvals">
      <ListHeader title="Refunds waiting for approval" onRefresh={refresh} count={items?.length} />
      {message && <p className="mb-3 rounded bg-slate-100 p-2 text-sm" role="status">{message}</p>}
      {error && <p className="text-sm text-red-700">{error}</p>}
      {items?.length === 0 && <p className="text-sm text-slate-500">Nothing to approve.</p>}
      <ul className="space-y-3">
        {items?.map((r) => (
          <li key={r.return_id} className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="font-medium">
                {formatMoney(r.refund_amount, r.currency)} · {r.order_id}
              </span>
              <span className="text-xs text-slate-500">
                {r.return_id} · customer {shortId(r.customer_id)}
              </span>
            </div>
            <p className="mt-1 text-sm">Reason: {r.reason}</p>
            <p className="text-xs text-slate-500">
              Requested {formatDate(r.created_at)} · expires {formatDate(r.approval_expires_at)}
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <input
                aria-label={`Note for ${r.return_id}`}
                placeholder="Decision note (optional)"
                maxLength={500}
                value={notes[r.return_id] ?? ""}
                onChange={(e) => setNotes((n) => ({ ...n, [r.return_id]: e.target.value }))}
                className="min-w-48 flex-1 rounded-md border border-slate-300 px-3 py-1.5 text-sm"
              />
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => void decide(r, true)}
                className="rounded-md bg-emerald-700 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40"
              >
                Approve
              </button>
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => void decide(r, false)}
                className="rounded-md border border-red-300 px-3 py-1.5 text-sm font-medium text-red-800 disabled:opacity-40"
              >
                Reject
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Tickets({ api }: { api: BusinessApi }) {
  const load = useCallback(() => api.listTickets("ESCALATED").then((r) => r.tickets), [api]);
  const { items, error, refresh } = useList<Ticket>(load);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const resolve = async (t: Ticket) => {
    setBusy(t.ticket_id);
    setMessage(null);
    try {
      await api.resolveTicket(t);
      setMessage(`${t.ticket_id} resolved`);
      refresh();
    } catch (e) {
      setMessage(e instanceof ApiError ? `${t.ticket_id}: ${e.message}` : "Could not resolve");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section aria-label="Escalated tickets">
      <ListHeader title="Escalated to a human" onRefresh={refresh} count={items?.length} />
      {message && <p className="mb-3 rounded bg-slate-100 p-2 text-sm" role="status">{message}</p>}
      {error && <p className="text-sm text-red-700">{error}</p>}
      {items?.length === 0 && <p className="text-sm text-slate-500">The queue is empty.</p>}
      <ul className="space-y-3">
        {items?.map((t) => (
          <li key={t.ticket_id} className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="font-medium">{t.subject}</span>
              <span className="text-xs text-slate-500">
                {t.ticket_id} · {t.category}
                {t.related_order_id ? ` · ${t.related_order_id}` : ""}
              </span>
            </div>
            {t.escalation_summary && (
              <p className="mt-2 whitespace-pre-line rounded bg-amber-50 p-2 text-sm">
                {t.escalation_summary}
              </p>
            )}
            <p className="mt-1 text-sm text-slate-600">{t.description}</p>
            <div className="mt-3 flex items-center justify-between">
              <span className="text-xs text-slate-500">
                Opened {formatDate(t.created_at)} · customer {shortId(t.customer_id)}
              </span>
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => void resolve(t)}
                className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40"
              >
                Mark resolved
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function ListHeader({
  title,
  count,
  onRefresh,
}: {
  title: string;
  count: number | undefined;
  onRefresh: () => void;
}) {
  return (
    <div className="mb-3 flex items-center gap-3">
      <h2 className="font-semibold">
        {title}
        {count !== undefined && <span className="ml-2 text-slate-500">({count})</span>}
      </h2>
      <button
        type="button"
        onClick={onRefresh}
        className="ml-auto rounded-md border border-slate-300 bg-white px-3 py-1 text-sm hover:bg-slate-50"
      >
        Refresh
      </button>
    </div>
  );
}
