// The account surface. Everything a signed-in person can ask or do about their
// own account, reached from the chip that used to only print their email.
//
// Before this the app could not answer "who am I signed in as, am I an
// administrator, when does this session end, how do I change my password" —
// every one of those was decided server-side and none of it was ever shown.
// Log out was the only account action on the page.
//
// Shape: a popover for reading and for the two safe writes (name, sign-out
// everywhere), and a modal for each of the two that are not safe (password,
// deletion). A destructive action inside a menu that closes when you click
// past it is a destructive action waiting to be half-completed.
import { useEffect, useRef, useState } from "react";
import {
  changePassword,
  deleteAccount,
  downloadExport,
  logoutSession,
  sessionExpiry,
  updateProfile,
} from "../api.js";
import { dateTime } from "../format.js";
import { Badge, Button, ErrorState, Input, Modal } from "./ui.jsx";

/** Staff is a real capability here — it is what puts Ops in the nav. */
function roleOf(user) {
  if (user?.is_superuser) return { label: "Owner", variant: "good", hint: "Full administrative access" };
  if (user?.is_staff) return { label: "Admin", variant: "good", hint: "Can reach the operator console" };
  return { label: "Member", variant: "neutral", hint: "Standard account" };
}

const fullName = (user) =>
  [user?.first_name, user?.last_name].filter(Boolean).join(" ").trim();

/** "in 6 days" / "in 3 hours" — a session end is only useful as a distance. */
function untilLabel(iso) {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  if (!Number.isFinite(ms)) return null;
  if (ms <= 0) return "expired";
  const hours = Math.round(ms / 3600000);
  if (hours < 1) return `in ${Math.max(1, Math.round(ms / 60000))} min`;
  if (hours < 48) return `in ${hours}h`;
  return `in ${Math.round(hours / 24)} days`;
}

function Row({ label, children }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="text-xs text-muted">{label}</span>
      <span className="text-xs text-text">{children}</span>
    </div>
  );
}

/** One tappable line in the menu. Hover and focus look the same on purpose. */
function MenuItem({ onClick, children, danger, testId, disabled }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      data-testid={testId}
      className={`flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
        danger
          ? "text-[var(--c-critical-text)] hover:bg-[var(--c-critical)]/10"
          : "text-text hover:bg-panel-2 focus-visible:bg-panel-2"
      }`}
    >
      {children}
    </button>
  );
}

function ChangePasswordModal({ onClose, onDone }) {
  const [form, setForm] = useState({ oldPassword: "", newPassword: "", confirmPassword: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const mismatch =
    form.confirmPassword !== "" && form.newPassword !== form.confirmPassword;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await changePassword(form);
      onDone("Password changed. Every other device has been signed out.");
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="Change password"
      subtitle="Changing it signs out every other device, including this one's other browsers."
      onClose={onClose}
      testId="account-password-modal"
      footer={
        <div className="flex justify-end gap-2">
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button
            variant="primary"
            onClick={submit}
            disabled={busy || mismatch || !form.oldPassword || !form.newPassword || !form.confirmPassword}
            data-testid="account-password-submit"
          >
            {busy ? "Saving…" : "Change password"}
          </Button>
        </div>
      }
    >
      <div className="space-y-3">
        <Input
          label="Current password"
          type="password"
          autoComplete="current-password"
          placeholder="Current password"
          className="w-full"
          value={form.oldPassword}
          onChange={set("oldPassword")}
          data-testid="account-password-current"
        />
        <Input
          label="New password"
          type="password"
          autoComplete="new-password"
          placeholder="New password"
          className="w-full"
          value={form.newPassword}
          onChange={set("newPassword")}
          data-testid="account-password-new"
        />
        <Input
          label="Confirm new password"
          type="password"
          autoComplete="new-password"
          placeholder="Repeat the new password"
          className="w-full"
          value={form.confirmPassword}
          onChange={set("confirmPassword")}
          data-testid="account-password-confirm"
        />
        {mismatch && (
          <p className="text-xs text-[var(--c-critical-text)]" data-testid="account-password-mismatch">
            The two new passwords do not match.
          </p>
        )}
        {error && <ErrorState error={error} testId="account-password-error" />}
      </div>
    </Modal>
  );
}

function DeleteAccountModal({ onClose, onDeleted }) {
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await deleteAccount(password);
      onDeleted();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  return (
    <Modal
      title="Delete this account"
      subtitle="Every portfolio, holding and ledger entry goes with it. This cannot be undone."
      onClose={onClose}
      testId="account-delete-modal"
      footer={
        <div className="flex justify-end gap-2">
          <Button onClick={onClose} disabled={busy}>Keep my account</Button>
          <Button
            variant="danger"
            onClick={submit}
            // The server checks both of these too; requiring them here is what
            // keeps the button from being clickable by accident.
            disabled={busy || confirmation !== "DELETE" || !password}
            data-testid="account-delete-submit"
          >
            {busy ? "Deleting…" : "Delete permanently"}
          </Button>
        </div>
      }
    >
      <div className="space-y-3">
        <p className="text-sm text-muted">
          Download your data first if you want to keep it — once this is done
          there is nothing left to export.
        </p>
        <Input
          label="Your password"
          type="password"
          autoComplete="current-password"
          placeholder="Your password"
          className="w-full"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          data-testid="account-delete-password"
        />
        <Input
          label="Type DELETE to confirm"
          placeholder="DELETE"
          className="w-full"
          value={confirmation}
          onChange={(e) => setConfirmation(e.target.value)}
          data-testid="account-delete-confirm"
        />
        {error && <ErrorState error={error} testId="account-delete-error" />}
      </div>
    </Modal>
  );
}

export default function AccountMenu({
  user,
  onLogout,
  onUserChange,
  triggerClass = "hidden md:flex",
  testId = "user-email",
}) {
  const [open, setOpen] = useState(false);
  const [modal, setModal] = useState(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState({
    firstName: user?.first_name || "",
    lastName: user?.last_name || "",
  });
  const wrap = useRef(null);

  // Close on Escape or on a click that lands outside. Both listeners are only
  // mounted while the menu is open, so a closed menu costs the page nothing.
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => e.key === "Escape" && setOpen(false);
    const onDown = (e) => {
      if (wrap.current && !wrap.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  const role = roleOf(user);
  const initial = (user?.email || "?").charAt(0).toUpperCase();
  const displayName = fullName(user) || user?.email;
  const expires = untilLabel(sessionExpiry.value);
  const nameChanged =
    name.firstName !== (user?.first_name || "") ||
    name.lastName !== (user?.last_name || "");

  const run = async (fn, message) => {
    setBusy(true);
    setError(null);
    try {
      const result = await fn();
      if (message) setNotice(message);
      return result;
    } catch (e) {
      setError(e);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const saveName = async () => {
    const updated = await run(() => updateProfile(name), "Name saved.");
    if (updated) onUserChange?.(updated);
  };

  const signOutEverywhere = async () => {
    // `logoutSession(true)` blacklists every refresh token this user holds,
    // including the one in this tab, so there is no signed-in state left to
    // return to — hence the same exit as a plain log out.
    await logoutSession(true);
    onLogout();
  };

  return (
    <div className="relative" ref={wrap}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="Account"
        data-testid={testId}
        className={`app-user-chip cursor-pointer transition-all ${triggerClass} ${
          open
            ? "border-accent/60 bg-accent/10"
            : "hover:border-accent/40 hover:bg-accent/5"
        }`}
      >
        <span className="app-user-avatar" aria-hidden="true">{initial}</span>
        <span className="max-w-[11rem] truncate text-sm text-muted">{user.email}</span>
        <svg
          width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"
          className={`shrink-0 text-muted transition-transform ${open ? "rotate-180" : ""}`}
        >
          <path d="M2 4.5L6 8.5L10 4.5" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" />
        </svg>
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Account"
          data-testid="account-menu"
          className="absolute right-0 z-40 mt-2 w-80 origin-top-right rounded-xl border border-border bg-panel p-3 shadow-2xl"
        >
          <div className="flex items-center gap-3 border-b border-border pb-3">
            <span className="app-user-avatar !h-10 !w-10 !text-base" aria-hidden="true">{initial}</span>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-text">{displayName}</p>
              <p className="truncate text-xs text-muted">{user.email}</p>
            </div>
            <span className="ml-auto">
              <Badge variant={role.variant} title={role.hint} testId="account-role">
                {role.label}
              </Badge>
            </span>
          </div>

          <div className="border-b border-border py-2">
            <Row label="Member since">{user.date_joined ? dateTime(user.date_joined) : "—"}</Row>
            <Row label="Signed in">{expires ? `this session ends ${expires}` : "active"}</Row>
            <Row label="Operator console">{user.is_staff ? "Available" : "Not available"}</Row>
          </div>

          <div className="border-b border-border py-3">
            <p className="mb-2 text-xs text-muted">Your name, as it appears here</p>
            <div className="flex gap-2">
              <Input
                label="First name"
                placeholder="First"
                className="w-1/2"
                value={name.firstName}
                onChange={(e) => setName((n) => ({ ...n, firstName: e.target.value }))}
                data-testid="account-first-name"
              />
              <Input
                label="Last name"
                placeholder="Last"
                className="w-1/2"
                value={name.lastName}
                onChange={(e) => setName((n) => ({ ...n, lastName: e.target.value }))}
                data-testid="account-last-name"
              />
            </div>
            <div className="mt-2 flex justify-end">
              <Button
                variant="success"
                disabled={!nameChanged || busy}
                onClick={saveName}
                data-testid="account-save-name"
              >
                Save
              </Button>
            </div>
          </div>

          <div className="py-1">
            <MenuItem onClick={() => setModal("password")} testId="account-change-password">
              Change password
            </MenuItem>
            <MenuItem onClick={signOutEverywhere} disabled={busy} testId="account-logout-all">
              Sign out on every device
            </MenuItem>
            <MenuItem
              onClick={() => run(downloadExport, "Export downloaded.")}
              disabled={busy}
              testId="account-export"
            >
              Download my data
            </MenuItem>
            <MenuItem onClick={() => run(() => logoutSession().then(onLogout))} testId="account-logout">
              Log out
            </MenuItem>
            <MenuItem onClick={() => setModal("delete")} danger testId="account-delete">
              Delete account
            </MenuItem>
          </div>

          {notice && (
            <p className="mt-2 text-xs text-[var(--c-good-text)]" data-testid="account-notice">{notice}</p>
          )}
          {error && (
            <div className="mt-2">
              <ErrorState error={error} testId="account-error" />
            </div>
          )}
        </div>
      )}

      {modal === "password" && (
        <ChangePasswordModal onClose={() => setModal(null)} onDone={setNotice} />
      )}
      {modal === "delete" && (
        <DeleteAccountModal onClose={() => setModal(null)} onDeleted={onLogout} />
      )}
    </div>
  );
}
