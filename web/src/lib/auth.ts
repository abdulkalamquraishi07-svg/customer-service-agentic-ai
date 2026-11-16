/** Cognito sign-in (SRP) via Amplify Auth. Tokens stay in Amplify's storage; the app only asks
 * for a fresh ID token per request (Amplify refreshes it when needed). */

import { Amplify } from "aws-amplify";
import {
  confirmSignIn,
  confirmSignUp,
  fetchAuthSession,
  resendSignUpCode,
  signIn,
  signOut,
  signUp,
  type SignInOutput,
} from "aws-amplify/auth";

import type { AppConfig } from "./config";

export type Role = "customer" | "support" | "approver";

export interface Session {
  idToken: string;
  email: string;
  groups: string[];
}

export type SignInStep = "done" | "new_password" | "totp" | "unsupported";

export function configureAuth(cfg: AppConfig): void {
  Amplify.configure({
    Auth: { Cognito: { userPoolId: cfg.userPoolId, userPoolClientId: cfg.userPoolClientId } },
  });
}

export function nextStep(output: SignInOutput): SignInStep {
  if (output.isSignedIn) return "done";
  switch (output.nextStep.signInStep) {
    case "DONE":
      return "done";
    case "CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED":
      return "new_password";
    case "CONFIRM_SIGN_IN_WITH_TOTP_CODE":
      return "totp";
    default:
      return "unsupported";
  }
}

export async function startSignIn(email: string, password: string): Promise<SignInStep> {
  return nextStep(await signIn({ username: email.trim(), password }));
}

export async function continueSignIn(response: string): Promise<SignInStep> {
  return nextStep(await confirmSignIn({ challengeResponse: response }));
}

/** Self sign-up: Cognito emails a code; the account works once the code is confirmed. */
export async function registerCustomer(email: string, password: string): Promise<"confirm" | "done"> {
  const out = await signUp({
    username: email.trim(),
    password,
    options: { userAttributes: { email: email.trim() } },
  });
  return out.nextStep.signUpStep === "CONFIRM_SIGN_UP" ? "confirm" : "done";
}

export async function confirmRegistration(email: string, code: string): Promise<void> {
  await confirmSignUp({ username: email.trim(), confirmationCode: code.trim() });
}

export async function resendRegistrationCode(email: string): Promise<void> {
  await resendSignUpCode({ username: email.trim() });
}

export async function currentSession(): Promise<Session | null> {
  const session = await fetchAuthSession();
  const token = session.tokens?.idToken;
  if (!token) return null;
  const groups = token.payload["cognito:groups"];
  return {
    idToken: token.toString(),
    email: String(token.payload.email ?? ""),
    groups: Array.isArray(groups) ? groups.map(String) : [],
  };
}

export async function idToken(): Promise<string> {
  const session = await currentSession();
  if (!session) throw new Error("not signed in");
  return session.idToken;
}

export async function endSession(): Promise<void> {
  await signOut();
}

/** UI routing only; every action is authorized again by the backend. */
export function rolesFor(groups: string[]): Role[] {
  const roles: Role[] = [];
  if (groups.includes("customers")) roles.push("customer");
  if (groups.includes("support-agents")) roles.push("support");
  if (groups.includes("approvers")) roles.push("approver");
  return roles;
}
