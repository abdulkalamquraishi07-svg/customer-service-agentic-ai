interface Props {
  email: string;
  environment: string;
  view: "chat" | "ops";
  canChat: boolean;
  canOps: boolean;
  onView: (view: "chat" | "ops") => void;
  onSignOut: () => void;
}

export function Header({ email, environment, view, canChat, canOps, onView, onSignOut }: Props) {
  const tab = (id: "chat" | "ops", label: string) => (
    <button
      type="button"
      onClick={() => onView(id)}
      aria-current={view === id ? "page" : undefined}
      className={`rounded-md px-3 py-1.5 text-sm font-medium ${
        view === id ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100"
      }`}
    >
      {label}
    </button>
  );
  return (
    <header className="flex items-center gap-3 border-b border-slate-200 bg-white px-4 py-3">
      <span className="font-semibold">Support Assistant</span>
      {environment !== "prod" && (
        <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">
          {environment}
        </span>
      )}
      <nav className="ml-4 flex gap-1" aria-label="Main">
        {canChat && tab("chat", "Chat")}
        {canOps && tab("ops", "Ops console")}
      </nav>
      <span className="ml-auto hidden text-sm text-slate-500 sm:inline">{email}</span>
      <button
        type="button"
        onClick={onSignOut}
        className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50"
      >
        Sign out
      </button>
    </header>
  );
}
