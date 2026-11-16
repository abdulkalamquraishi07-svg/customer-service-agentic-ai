import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  error: Error | null;
}

/** Without this, any render error unmounts the whole app and the page goes blank (seen in the
 * browser after a chat reply). Shows what failed and lets the user recover. */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("UI crashed", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <main className="mx-auto max-w-lg p-8" role="alert">
        <h1 className="text-xl font-semibold">Something went wrong on this page</h1>
        <p className="mt-2 text-slate-600">
          Please reload. If it keeps happening, send this message to support:
        </p>
        <pre className="mt-3 whitespace-pre-wrap rounded bg-slate-100 p-3 text-sm text-red-800">
          {error.name}: {error.message}
        </pre>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="mt-4 rounded-md bg-slate-900 px-4 py-2 font-medium text-white"
        >
          Reload
        </button>
      </main>
    );
  }
}
