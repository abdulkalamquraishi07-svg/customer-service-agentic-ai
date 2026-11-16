import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({
  startSignIn: vi.fn(),
  continueSignIn: vi.fn(),
  registerCustomer: vi.fn(),
  confirmRegistration: vi.fn(),
  resendRegistrationCode: vi.fn(),
}));
vi.mock("../lib/auth", () => auth);

import { LoginPage } from "./LoginPage";

const type = (label: string | RegExp, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

async function submit(button: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: button }));
  });
}

describe("LoginPage sign-up", () => {
  beforeEach(() => vi.resetAllMocks());
  afterEach(cleanup);

  it("hides account creation unless sign-up is enabled", () => {
    render(<LoginPage onSignedIn={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Create an account" })).toBeNull();
  });

  it("registers, confirms the emailed code and signs in", async () => {
    auth.registerCustomer.mockResolvedValue("confirm");
    auth.confirmRegistration.mockResolvedValue(undefined);
    auth.startSignIn.mockResolvedValue("done");
    const onSignedIn = vi.fn();
    render(<LoginPage onSignedIn={onSignedIn} allowSignUp />);

    fireEvent.click(screen.getByRole("button", { name: "Create an account" }));
    type("Email", "new@example.com");
    type(/^Password/, "Str0ng!Passw0rd");
    await submit("Create account");
    expect(auth.registerCustomer).toHaveBeenCalledWith("new@example.com", "Str0ng!Passw0rd");
    expect(screen.getByText(/We sent a 6-digit code to new@example.com/)).toBeTruthy();

    type("Verification code", "123456");
    await submit("Confirm and sign in");
    expect(auth.confirmRegistration).toHaveBeenCalledWith("new@example.com", "123456");
    expect(auth.startSignIn).toHaveBeenCalledWith("new@example.com", "Str0ng!Passw0rd");
    expect(onSignedIn).toHaveBeenCalledOnce();
  });

  it("explains an existing account and a wrong code", async () => {
    auth.registerCustomer.mockRejectedValueOnce(
      Object.assign(new Error("x"), { name: "UsernameExistsException" }),
    );
    render(<LoginPage onSignedIn={vi.fn()} allowSignUp />);
    fireEvent.click(screen.getByRole("button", { name: "Create an account" }));
    type("Email", "taken@example.com");
    type(/^Password/, "Str0ng!Passw0rd");
    await submit("Create account");
    expect(screen.getByRole("alert").textContent).toMatch(/already exists/);

    auth.registerCustomer.mockResolvedValueOnce("confirm");
    auth.confirmRegistration.mockRejectedValueOnce(
      Object.assign(new Error("x"), { name: "CodeMismatchException" }),
    );
    await submit("Create account");
    type("Verification code", "000000");
    await submit("Confirm and sign in");
    expect(screen.getByRole("alert").textContent).toMatch(/code is not correct/);
    expect(auth.startSignIn).not.toHaveBeenCalled();
  });
});
