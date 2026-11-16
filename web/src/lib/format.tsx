/** Safe rendering of assistant text: only **bold** and line breaks become markup.
 * Everything else is rendered as text by React, so model output can never inject HTML. */

import type { ReactNode } from "react";

export function renderAssistantText(text: string): ReactNode[] {
  return text.split("\n").map((line, lineIdx) => {
    const parts = line.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
      part.startsWith("**") && part.endsWith("**") && part.length > 4 ? (
        <strong key={i}>{part.slice(2, -2)}</strong>
      ) : (
        part.replace(/^#{1,6}\s+/, "").replace(/^[-*]\s+/, "• ")
      ),
    );
    return (
      <span key={lineIdx} className="block min-h-[1em]">
        {parts}
      </span>
    );
  });
}

export function formatMoney(amount: string, currency: string): string {
  const value = Number(amount);
  if (!Number.isFinite(value)) return `${amount} ${currency}`;
  return new Intl.NumberFormat("en-IN", { style: "currency", currency }).format(value);
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

export function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}
