import { useCallback, useEffect, useMemo, useState } from "react";

import { ChatPage } from "./components/ChatPage";
import { Header } from "./components/Header";
import { LoginPage } from "./components/LoginPage";
import { OpsPage } from "./components/OpsPage";
import { AgentClient, BusinessApi } from "./lib/api";
import {
  configureAuth,
  currentSession,
  endSession,
  idToken,
  rolesFor,
  type Session,
} from "./lib/auth";
import { loadConfig, type AppConfig } from "./lib/config";

type View = "chat" | "ops";

export default function App() {
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [view, setView] = useState<View>("chat");

  const refreshSession = useCallback(async () => {
    try {
      setSession(await currentSession());
    } catch {
      setSession(null);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    loadConfig()
      .then((c) => {
        configureAuth(c);
        setCfg(c);
        return refreshSession();
      })
      .catch((e: unknown) => {
        setBootError(e instanceof Error ? e.message : String(e));
        setChecking(false);
      });
  }, [refreshSession]);

  const roles = useMemo(() => rolesFor(session?.groups ?? []), [session]);
  const isStaff = roles.includes("support") || roles.includes("approver");
  const isCustomer = roles.includes("customer");

  useEffect(() => {
    if (isStaff && !isCustomer) setView("ops");
  }, [isStaff, isCustomer]);

  const clients = useMemo(
    () => (cfg ? { api: new BusinessApi(cfg, idToken), agent: new AgentClient(cfg, idToken) } : null),
    [cfg],
  );

  if (bootError) {
    return (
      <main className="mx-auto max-w-lg p-8">
        <h1 className="text-xl font-semibold">The app could not start</h1>
        <p className="mt-2 text-slate-600">{bootError}</p>
      </main>
    );
  }
  if (checking || !cfg || !clients) {
    return <p className="p-8 text-slate-500">Loading…</p>;
  }
  if (!session) {
    return <LoginPage onSignedIn={refreshSession} allowSignUp={cfg.selfSignUp} />;
  }

  const signOut = async () => {
    await endSession();
    setSession(null);
  };

  return (
    <div className="flex h-dvh flex-col">
      <Header
        email={session.email}
        environment={cfg.environment}
        view={view}
        canChat={isCustomer}
        canOps={isStaff}
        onView={setView}
        onSignOut={signOut}
      />
      {!isCustomer && !isStaff ? (
        <p className="p-8 text-slate-600">
          Your account has no access to this application. Please contact your administrator.
        </p>
      ) : view === "chat" && isCustomer ? (
        <ChatPage agent={clients.agent} api={clients.api} />
      ) : (
        <OpsPage api={clients.api} roles={roles} />
      )}
    </div>
  );
}
