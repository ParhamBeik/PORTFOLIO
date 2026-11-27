import { Link } from "react-router-dom";
import { registrationStatus } from "../api.js";
import { useT } from "../i18n.js";
import { useApi } from "../useApi.js";

// Bump when the wording below changes. Counsel review is required before any
// paid plan; the free service runs on this text.
const LAST_UPDATED = "2026-11-27";


export default function Legal({ kind, authed = false }) {
  const t = useT();
  const privacy = kind === "privacy";
  const status = useApi(registrationStatus, []);
  const contact = status.data?.contact_email || "";
  return (
    <main data-testid="legal-page" className="mx-auto max-w-2xl px-4 py-10">
      <h1 data-testid="legal-heading" className="text-xl font-semibold text-text">
        {t(privacy ? "Privacy Notice" : "Terms of Use")}
      </h1>
      <p data-testid="legal-updated" className="mt-2 text-xs text-muted">
        {t("Last updated: {date}", { date: LAST_UPDATED })}
      </p>
      <div className="mt-4 space-y-3 text-sm leading-relaxed text-muted">
        {privacy ? (
          <>
            <h2 className="text-xl font-semibold text-text">{t("Data We Use")}</h2>
            <p>{t("We use account, portfolio, ledger, import, and diagnostic data to operate Holdings.")}</p>
            <p>{t("Your email address is used to sign you in and to send confirmation and password-reset links. We do not send marketing email.")}</p>
            <h2 className="text-xl font-semibold text-text">{t("Your Rights")}</h2>
            <p>{t("You can export your account data, revoke sessions, or delete your account from the account menu.")}</p>
            <h2 className="text-xl font-semibold text-text">{t("Retention")}</h2>
            <p>{t("Account-owned data is deleted with the account. A sign-up that is never confirmed is deleted after 7 days. Shared market prices belong to no account and are kept.")}</p>
          </>
        ) : (
          <>
            <h2 className="text-xl font-semibold text-text">{t("Informational Use")}</h2>
            <p>{t("Holdings provides informational analytics, not investment, tax, or legal advice.")}</p>
            <h2 className="text-xl font-semibold text-text">{t("Data Quality")}</h2>
            <p>{t("Figures may be delayed, incomplete, estimated, or excluded; trust labels and provenance remain part of the result.")}</p>
            <h2 className="text-xl font-semibold text-text">{t("Free Service")}</h2>
            <p>{t("Holdings is free. Each member has limits on portfolios, holdings and daily imports, and features may change while the service grows.")}</p>
          </>
        )}
        {contact && (
          <>
            <h2 className="text-xl font-semibold text-text">{t("Contact")}</h2>
            <p data-testid="legal-contact">
              {t("Questions or requests:")}{" "}
              <a href={`mailto:${contact}`} className="underline">{contact}</a>
            </p>
          </>
        )}
      </div>
      {/* A signed-in reader reaches these from the footer of every page, so
          "Back to sign in" would bounce them off their own session. Both targets
          are now declared routes in their respective trees; /login used to
          resolve only by falling through to the signed-out catch-all. */}
      <Link
        to={authed ? "/" : "/login"}
        data-testid="legal-back-link"
        className="mt-6 inline-block rounded-md border border-border bg-panel-2 px-3 py-1.5 text-sm font-medium text-text hover:bg-border"
      >
        {t(authed ? "Back to portfolio" : "Back to sign in")}
      </Link>
    </main>
  );
}
