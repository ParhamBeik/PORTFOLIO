import { Link } from "react-router-dom";


export default function Legal({ kind, authed = false }) {
  const privacy = kind === "privacy";
  return (
    <main data-testid="legal-page" className="mx-auto max-w-2xl px-4 py-10">
      <h1 data-testid="legal-heading" className="text-xl font-semibold text-text">
        {privacy ? "Privacy Notice" : "Terms of Use"}
      </h1>
      <p
        data-testid="legal-warning"
        className="mt-4 rounded-lg border border-[var(--c-warn-text)] bg-panel-2 px-4 py-3 text-sm text-[var(--c-warn-text)]"
      >
        Draft for closed beta. Iranian counsel approval is pending; paid public
        launch remains blocked until that review is recorded.
      </p>
      <div className="mt-4 space-y-3 text-sm leading-relaxed text-muted">
        {privacy ? (
          <>
            <h2 className="text-xl font-semibold text-text">Data We Use</h2>
            <p>We use account, portfolio, ledger, import, payment, and diagnostic data to operate Holdings.</p>
            <h2 className="text-xl font-semibold text-text">Your Rights</h2>
            <p>You can export your account data, revoke sessions, or delete your account from Profile.</p>
            <h2 className="text-xl font-semibold text-text">Retention</h2>
            <p>Account-owned data is deleted with the account. Payment audit records are detached and pseudonymized.</p>
          </>
        ) : (
          <>
            <h2 className="text-xl font-semibold text-text">Informational Use</h2>
            <p>Holdings provides informational analytics, not investment, tax, or legal advice.</p>
            <h2 className="text-xl font-semibold text-text">Data Quality</h2>
            <p>Figures may be delayed, incomplete, estimated, or excluded; trust labels and provenance remain part of the result.</p>
            <h2 className="text-xl font-semibold text-text">Closed Beta</h2>
            <p>Access requires an invitation and may change while the service is evaluated.</p>
          </>
        )}
      </div>
      {/* A signed-in reader reaches these from the footer of every page, and
          "Back to sign in" pointed them at /login -- not a route in the
          authenticated tree, so it fell through to `*` and bounced them to the
          dashboard without explanation. */}
      <Link
        to={authed ? "/" : "/login"}
        data-testid="legal-back-link"
        className="mt-6 inline-block rounded-md border border-border bg-panel-2 px-3 py-1.5 text-sm font-medium text-text hover:bg-border"
      >
        {authed ? "Back to portfolio" : "Back to sign in"}
      </Link>
    </main>
  );
}
