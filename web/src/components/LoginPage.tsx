import { useState, type FormEvent, type ReactNode } from "react";

import {
  confirmRegistration,
  continueSignIn,
  registerCustomer,
  resendRegistrationCode,
  startSignIn,
  type SignInStep,
} from "../lib/auth";

const MESSAGES: Record<string, string> = {
  NotAuthorizedException: "Incorrect email or password.",
  UserNotFoundException: "Incorrect email or password.", // no account enumeration
  PasswordResetRequiredException: "A password reset is required. Contact support.",
  InvalidPasswordException: "That password does not meet the requirements.",
  CodeMismatchException: "That code is not correct.",
  ExpiredCodeException: "That code has expired. Send a new one.",
  LimitExceededException: "Too many attempts. Please wait and try again.",
  TooManyRequestsException: "Too many attempts. Please wait and try again.",
  UsernameExistsException: "An account with this email already exists. Sign in instead.",
  InvalidParameterException: "Please check the email address and password.",
  UserNotConfirmedException: "Please confirm your email address first.",
  CodeDeliveryFailureException: "We could not send the code. Please try again later.",
};

const PASSWORD_RULES =
  "At least 12 characters with upper and lower case letters, a number and a symbol.";

function describe(e: unknown, fallback: string): string {
  const name = (e as { name?: string })?.name ?? "";
  return MESSAGES[name] ?? fallback;
}

type Mode = "sign_in" | "sign_up" | "confirm";

export function LoginPage({
  onSignedIn,
  allowSignUp = false,
}: {
  onSignedIn: () => void;
  allowSignUp?: boolean;
}) {
  const [mode, setMode] = useState<Mode>("sign_in");
  const [step, setStep] = useState<SignInStep | "credentials">("credentials");
  const [email, setEmail] = useState("");
  const [secret, setSecret] = useState("");
  // Kept only between "create account" and "confirm code", to sign in automatically afterwards.
  const [newPassword, setNewPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const switchMode = (next: Mode) => {
    setMode(next);
    setStep("credentials");
    setSecret("");
    setCode("");
    setError(null);
    setNotice(null);
  };

  const run = async (action: () => Promise<void>, fallback: string) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(describe(err, fallback));
    } finally {
      setBusy(false);
    }
  };

  const finishSignIn = (next: SignInStep) => {
    if (next === "done") onSignedIn();
    else if (next === "unsupported") setError("This sign-in step is not supported here.");
    else setStep(next);
  };

  const submitSignIn = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const next =
        step === "credentials" ? await startSignIn(email, secret) : await continueSignIn(secret);
      setSecret("");
      finishSignIn(next);
    }, "Sign-in failed. Please try again.");
  };

  const submitSignUp = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const password = secret;
      const next = await registerCustomer(email, password);
      setSecret("");
      if (next === "done") {
        finishSignIn(await startSignIn(email, password));
        return;
      }
      setNewPassword(password);
      setMode("confirm");
      setNotice(`We sent a 6-digit code to ${email.trim()}. It can take a minute to arrive.`);
    }, "Could not create the account. Please try again.");
  };

  const submitConfirm = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      await confirmRegistration(email, code);
      setCode("");
      if (newPassword) {
        const password = newPassword;
        setNewPassword("");
        finishSignIn(await startSignIn(email, password));
      } else {
        switchMode("sign_in");
        setNotice("Your email is confirmed. Please sign in.");
      }
    }, "Could not confirm the code. Please try again.");
  };

  const resend = () =>
    void run(async () => {
      await resendRegistrationCode(email);
      setNotice(`A new code was sent to ${email.trim()}.`);
    }, "Could not send a new code. Please try again.");

  const emailField = (
    <Field label="Email">
      <input
        type="email"
        autoComplete="username"
        required
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
      />
    </Field>
  );
  const messages = (
    <>
      {notice && (
        <p role="status" className="text-sm text-emerald-800">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      )}
    </>
  );

  if (mode === "sign_up") {
    return (
      <Card title="Create an account" onSubmit={submitSignUp}>
        {emailField}
        <Field label="Password" hint={PASSWORD_RULES}>
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={12}
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </Field>
        {messages}
        <Submit busy={busy} label="Create account" busyLabel="Creating account…" />
        <SwitchLink text="Already have an account?" action="Sign in" onClick={() => switchMode("sign_in")} />
      </Card>
    );
  }

  if (mode === "confirm") {
    return (
      <Card title="Confirm your email" onSubmit={submitConfirm}>
        {messages}
        <Field label="Verification code">
          <input
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            required
            value={code}
            onChange={(e) => setCode(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </Field>
        <Submit busy={busy} label="Confirm and sign in" busyLabel="Confirming…" />
        <div className="flex justify-between text-sm">
          <button type="button" onClick={resend} disabled={busy} className="text-slate-600 hover:underline">
            Send a new code
          </button>
          <button
            type="button"
            onClick={() => switchMode("sign_in")}
            className="text-slate-600 hover:underline"
          >
            Back to sign in
          </button>
        </div>
      </Card>
    );
  }

  const label =
    step === "new_password"
      ? "Choose a new password"
      : step === "totp"
        ? "Authenticator code"
        : "Password";

  return (
    <Card title="Sign in" onSubmit={submitSignIn}>
      {step === "credentials" && emailField}
      {step === "new_password" && (
        <p className="text-sm text-slate-600">Your account needs a new password. {PASSWORD_RULES}</p>
      )}
      <Field label={label}>
        <input
          type={step === "totp" ? "text" : "password"}
          inputMode={step === "totp" ? "numeric" : undefined}
          autoComplete={
            step === "totp" ? "one-time-code" : step === "new_password" ? "new-password" : "current-password"
          }
          required
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
        />
      </Field>
      {messages}
      <Submit busy={busy} label="Continue" busyLabel="Signing in…" />
      {allowSignUp && step === "credentials" && (
        <SwitchLink text="New customer?" action="Create an account" onClick={() => switchMode("sign_up")} />
      )}
    </Card>
  );
}

function Card({
  title,
  onSubmit,
  children,
}: {
  title: string;
  onSubmit: (e: FormEvent) => void;
  children: ReactNode;
}) {
  return (
    <main className="flex min-h-dvh items-center justify-center p-4">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm"
      >
        <h1 className="text-xl font-semibold">{title}</h1>
        {children}
      </form>
    </main>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="font-medium">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  );
}

function Submit({ busy, label, busyLabel }: { busy: boolean; label: string; busyLabel: string }) {
  return (
    <button
      type="submit"
      disabled={busy}
      className="w-full rounded-md bg-slate-900 px-3 py-2 font-medium text-white disabled:opacity-50"
    >
      {busy ? busyLabel : label}
    </button>
  );
}

function SwitchLink({ text, action, onClick }: { text: string; action: string; onClick: () => void }) {
  return (
    <p className="text-center text-sm text-slate-600">
      {text}{" "}
      <button type="button" onClick={onClick} className="font-medium text-slate-900 hover:underline">
        {action}
      </button>
    </p>
  );
}
