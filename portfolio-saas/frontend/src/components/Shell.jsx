import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import AccountMenu from "./AccountMenu.jsx";
import LanguageToggle from "./LanguageToggle.jsx";
import Logo from "./Logo.jsx";
import { usePortfolio } from "./PortfolioContext.jsx";
import { useT } from "../i18n.js";
import { createAccount } from "../api.js";
import { Button, ErrorState, Field, Input, Modal, Select } from "./ui.jsx";

const APP_NAME = "Holdings";

const PAGES = [
  { to: "/", label: "Home", end: true },
  { to: "/activity", label: "Activity" },
  { to: "/research", label: "Research" },
  { to: "/compare", label: "Compare" },
  // Beta: its models have not passed the source checks the rest of the app
  // is held to, and the label says so wherever the link appears.
  { to: "/risk", label: "Risk", beta: true },
];

// Nominal Toman and verified USD only. Real Toman (CPI) and USDT read as two
// more currencies to choose between, and neither answers a question the other
// two do not; the server still accepts both for old links.
export const BASES = [
  ["nominal_toman", "Toman"],
  ["usd_denominated", "USD"],
];

function MenuIcon({ open }) {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      {open ? (
        <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
      ) : (
        <path d="M3 6h14M3 10h14M3 14h14" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
      )}
    </svg>
  );
}

function NavItem({ to, end, testId, children, onClick, beta }) {
  const t = useT();
  return (
    <NavLink
      to={to}
      end={end}
      data-testid={testId}
      onClick={onClick}
      className={({ isActive }) => `app-nav-link${isActive ? " is-active" : ""}`}
    >
      {t(children)}
      {beta && <BetaTag />}
    </NavLink>
  );
}

function BetaTag() {
  const t = useT();
  return (
    <span className="ms-1.5 rounded border border-border px-1 py-px text-[10px] font-medium tracking-wide text-muted uppercase">
      {t("Beta")}
    </span>
  );
}

/**
 * The page links, rendered twice: inline from `xl`, in the drawer below it.
 *
 * Two copies means two sets of test ids, and only one can be the canonical
 * `nav-activity` — a duplicate id resolves to whichever the DOM happens to hold
 * and a hidden one is not clickable. The inline rail keeps the plain names
 * because that is the one on screen at the width the suites run at; the drawer
 * suffixes `-mobile`, the same convention `AccountMenu` already uses.
 */
function NavLinks({ admin, suffix = "", onNavigate }) {
  const id = (label) => `nav-${label.toLowerCase().replace(/\s+/g, "-")}${suffix}`;
  return (
    <>
      {PAGES.map((p) => (
        <NavItem key={p.to} to={p.to} end={p.end} beta={p.beta} testId={id(p.label)} onClick={onNavigate}>
          {p.label}
        </NavItem>
      ))}
      {admin && (
        <NavItem to="/ops" testId={id("Operations")} onClick={onNavigate}>Operations</NavItem>
      )}
    </>
  );
}

/**
 * The small-screen navigation, as a panel that slides in from the right.
 *
 * It used to be an inline block in the header flow, so opening it PUSHED the
 * whole page down and closing it snapped the page back up — on a phone that
 * reads as the content jumping, not as a menu. A drawer sits over the page
 * instead: nothing below it moves, and the thing that moves is the thing the
 * tap was about.
 *
 * Overlay rules are the ones `Modal` already establishes, for the same reasons:
 * Escape closes it, a click on the scrim closes it, the page behind must not
 * scroll under it, and focus goes in on open and back to the opener on close.
 * A menu that leaves focus behind on <body> restarts a keyboard user at the top
 * of the document every time they open it.
 */
function NavDrawer({ open, onClose, user, onLogout, onUserChange }) {
  const t = useT();
  const panel = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const opener = document.activeElement;
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panel.current?.querySelector("a, button")?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
      if (opener instanceof HTMLElement && document.contains(opener)) opener.focus();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="app-drawer-scrim xl:hidden"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        id="app-nav-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Menu"
        data-testid="nav-drawer"
        className="app-drawer"
      >
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <span className="text-sm font-semibold">{t("Menu")}</span>
          <Button
            variant="ghost"
            onClick={onClose}
            aria-label="Close menu"
            data-testid="nav-close"
          >
            ✕
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-4">
          <nav aria-label="Primary" className="app-nav-stack" data-testid="nav-mobile">
            <NavLinks admin={user?.role === "admin"} suffix="-mobile" onNavigate={onClose} />
          </nav>

          <div className="mt-4 flex items-center justify-between border-t border-border pt-4">
            <span className="text-sm text-muted">{t("Language")}</span>
            <LanguageToggle />
          </div>

          {/* Account settings that only exist above the md breakpoint are
              account settings most people never find. */}
          <div className="mt-4 border-t border-border pt-4 md:hidden">
            <AccountMenu
              user={user}
              onLogout={onLogout}
              onUserChange={onUserChange}
              triggerClass="flex w-full"
              panelFill
              testId="user-email-mobile"
            />
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * Phone navigation: the four destinations a thumb reaches for, plus a raised
 * "+ trade" button -- recording a trade right after the broker fills it is the
 * one task that has to take seconds. The drawer still holds everything else.
 */
const TABS = PAGES.filter((p) => p.to !== "/risk");

function BottomTabs() {
  const t = useT();
  return (
    <nav aria-label="Quick" className="app-bottom-tabs" data-testid="nav-bottom">
      {TABS.slice(0, 2).map((p) => (
        <NavLink key={p.to} to={p.to} end={p.end} data-testid={`tab-${p.label.toLowerCase()}`}
          className={({ isActive }) => `app-bottom-tab${isActive ? " is-active" : ""}`}>
          {t(p.label)}
        </NavLink>
      ))}
      <NavLink to="/activity?add=trade" aria-label={t("Record a trade")} data-testid="tab-add-trade"
        className="app-bottom-add">+</NavLink>
      {TABS.slice(2).map((p) => (
        <NavLink key={p.to} to={p.to} data-testid={`tab-${p.label.toLowerCase()}`}
          className={({ isActive }) => `app-bottom-tab${isActive ? " is-active" : ""}`}>
          {t(p.label)}
        </NavLink>
      ))}
    </nav>
  );
}

/**
 * Toman / USD. Two options behind a dropdown cost two taps and hid the other
 * choice; a segmented switch shows both and changes in one. Symbols on a phone,
 * words from `sm` up; the full name is always the accessible label.
 */
function BasisToggle({ basis, setBasis }) {
  const t = useT();
  const current = BASES.some(([v]) => v === basis) ? basis : "nominal_toman";
  return (
    <div role="group" aria-label={t("Valuation basis")} className="app-basis" data-testid="scope-basis">
      {BASES.map(([v, l]) => (
        <button
          key={v}
          type="button"
          aria-pressed={current === v}
          aria-label={t(l)}
          data-testid={`scope-basis-${v}`}
          onClick={() => setBasis(v)}
          className="app-basis-btn"
        >
          <span aria-hidden="true" className="sm:hidden">{v === "usd_denominated" ? "$" : "T"}</span>
          <span aria-hidden="true" className="hidden sm:inline">{t(l)}</span>
        </button>
      ))}
    </div>
  );
}

const NEW_PORTFOLIO = "__new__";

/**
 * Name, and optionally what it is for. Onboarding was the only place a
 * portfolio could be made, and it redirects anyone who already holds
 * something -- so after the first, there was no way to make a second.
 */
function NewPortfolioDialog({ onClose, onCreated }) {
  const t = useT();
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const submit = async (e) => {
    e.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      onCreated(await createAccount(name.trim(), "", goal.trim()));
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  };
  return (
    <Modal
      title="New portfolio"
      onClose={onClose}
      testId="new-portfolio"
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button
            variant="primary"
            type="submit"
            form="new-portfolio-form"
            disabled={!name.trim() || busy}
            data-testid="new-portfolio-create"
          >
            {busy ? t("Creating…") : t("Create")}
          </Button>
        </>
      }
    >
      <form id="new-portfolio-form" onSubmit={submit} className="space-y-4">
        <Field label={t("Name")}>
          <Input
            label="Portfolio name"
            className="w-full"
            value={name}
            maxLength={120}
            data-autofocus
            placeholder={t("e.g. Retirement, Kids, Trading")}
            onChange={(e) => setName(e.target.value)}
            data-testid="new-portfolio-name"
          />
        </Field>
        <Field label={t("Goal (optional)")}>
          <Input
            label="Goal"
            className="w-full"
            value={goal}
            maxLength={40}
            onChange={(e) => setGoal(e.target.value)}
            data-testid="new-portfolio-goal"
          />
        </Field>
        {error && <ErrorState error={error} testId="new-portfolio-error" />}
      </form>
    </Modal>
  );
}

export default function Shell({ user, onLogout, onUserChange }) {
  const t = useT();
  const { accounts, activeId, setActive, basis, setBasis, error, reload } = usePortfolio();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [creating, setCreating] = useState(false);

  // Stable, because the drawer's focus-and-scroll-lock effect lists it as a
  // dependency. A fresh closure per render tore that effect down and rebuilt it
  // on every `usePortfolio()` change -- a price poll, an account switch -- and
  // the teardown restores focus to the opener while the re-run sends it back to
  // the first nav link. A keyboard user tabbed three links deep was thrown to
  // the top mid-poll. `setMobileOpen` is itself stable, so there is nothing to
  // depend on.
  const closeMobile = useCallback(() => setMobileOpen(false), []);

  return (
    <div className="flex min-h-full flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:m-2 focus:rounded focus:bg-[var(--c-accent-fill)] focus:px-3 focus:py-2 focus:text-white"
      >
        {t("Skip to content")}
      </a>

      <header className="app-header sticky top-0 z-20 border-b border-border bg-panel/95 backdrop-blur-md">
        <div className="flex min-h-[3.5rem] items-center gap-x-3 px-4 sm:gap-x-4 py-2 lg:min-h-[4.25rem] lg:gap-x-5 lg:gap-y-3 lg:px-6 lg:py-3">
          <NavLink
            to="/"
            // Below `sm` the bottom bar's Home tab is the way home, and the
            // logo's 44px is the difference between "All portfolios" and
            // "All portfo" in the scope picker beside it.
            className="app-brand group order-1 shrink-0"
            aria-label={`${APP_NAME} home`}
            data-testid="app-brand"
          >
            <span className="flex size-8 items-center justify-center rounded-lg border border-border bg-panel-2 text-accent shadow-sm transition group-hover:border-accent/40 group-hover:bg-[var(--c-accent-fill)]/10 lg:size-9">
              <Logo size={20} title={APP_NAME} />
            </span>
            <span className="flex flex-col leading-tight">
              <span className="hidden text-sm font-semibold tracking-tight text-text sm:block lg:text-base">{APP_NAME}</span>
              {/* Below lg the header is fighting for every pixel of height and
                  the strapline is the one thing on it that says nothing. */}
              <span className="hidden text-[11px] text-muted lg:block">{t("Portfolio tracker")}</span>
            </span>
          </NavLink>

          {/* Which portfolio, priced in what: these say what every number on the
              page MEANS, so they stay on screen at every width. On a phone they
              share the brand row -- a second row of labelled selects cost 100px
              of a sticky header and still cut "All portfolios" to "All portfo".
              Basis is a two-way switch, so it is a toggle, not a dropdown. */}
          <div
            className="app-toolbar order-2 xl:order-3 xl:ml-auto"
            data-testid="header-toolbar"
          >
            <Select
              label="Active portfolio"
              data-testid="scope-account"
              className="app-toolbar-select"
              value={activeId ?? ""}
              onChange={(e) => {
                // Not a scope: an action that lives where portfolios are picked,
                // so the picker keeps showing the current one until it exists.
                if (e.target.value === NEW_PORTFOLIO) {
                  setCreating(true);
                  return;
                }
                setActive(e.target.value === "" ? null : Number(e.target.value));
              }}
            >
              <option value="">{t("All portfolios")}</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                  {a.goal ? ` · ${a.goal}` : ""}
                </option>
              ))}
              <option value={NEW_PORTFOLIO} data-testid="scope-account-new">+ {t("New portfolio")}…</option>
            </Select>
            <BasisToggle basis={basis} setBasis={setBasis} />
          </div>

          {/* From `xl` up there is room for the links inline, and a drawer on
              desktop would be hiding navigation for no reason.
              The breakpoint lives on `.app-nav-rail` in index.css, not in a
              `hidden lg:flex` here: that utility and the component class are
              both single-class selectors, so the later stylesheet wins and the
              rail rendered stacked inside a phone's header. */}
          <nav
            aria-label="Primary"
            className="app-nav-rail xl:order-2"
            data-testid="nav"
          >
            <NavLinks admin={user?.role === "admin"} />
          </nav>

          <div className="order-3 ml-auto flex shrink-0 items-center gap-2 sm:gap-3 xl:order-4 xl:ml-0">
            {/* Log out lives INSIDE this menu, next to the rest of the account
                actions it belongs with — it was the only one that had a home.
                The language switch is a once-ever setting: below xl it lives
                in the drawer rather than taking header width from the scope. */}
            <div className="hidden xl:block">
              <LanguageToggle />
            </div>
            <AccountMenu user={user} onLogout={onLogout} onUserChange={onUserChange} />

            <button
              type="button"
              className="app-header-btn inline-flex items-center justify-center rounded-md border border-border bg-panel-2 p-2 text-text xl:hidden"
              aria-label={mobileOpen ? "Close menu" : "Open menu"}
              aria-expanded={mobileOpen}
              aria-haspopup="dialog"
              aria-controls="app-nav-panel"
              data-testid="nav-toggle"
              onClick={() => setMobileOpen((v) => !v)}
            >
              <MenuIcon open={mobileOpen} />
            </button>
          </div>
        </div>
      </header>

      {creating && (
        <NewPortfolioDialog
          onClose={() => setCreating(false)}
          onCreated={async (account) => {
            setCreating(false);
            await reload();
            setActive(account.id);
          }}
        />
      )}

      <NavDrawer
        open={mobileOpen}
        onClose={closeMobile}
        user={user}
        onLogout={onLogout}
        onUserChange={onUserChange}
      />

      {/* `flex-1` alone makes main fill the space LEFT OVER, which lands the
          footer exactly on the fold while the route is still loading. Every
          data route then grows past a screen and pushes the footer out of view
          — one move, no content of its own, and the whole of this app's
          cumulative layout shift (0.05 on Breakdown, Best Overall and My
          Optimal; the footer was the only node either the browser or Lighthouse
          ever reported). Reserving a screen of content puts the footer below
          the fold from the first paint, so its move is no longer a shift a
          reader can see. The pages genuinely shorter than this — the legal
          text, onboarding — pay one header's worth of extra scroll to reach
          it. */}
      <main id="main" tabIndex={-1} className="mx-auto min-h-[100svh] w-full max-w-7xl flex-1 px-4 py-6 lg:px-6">
        {error && (
          <div className="mb-4">
            <ErrorState error={{ message: `Could not load portfolios: ${error}` }} onRetry={reload} />
          </div>
        )}
        <Outlet />
      </main>

      <BottomTabs />

      <footer className="border-t border-border px-4 py-3 text-center text-xs text-muted lg:px-6">
        <NavLink to="/privacy" className="hover:text-text">{t("Privacy")}</NavLink>
        <span className="mx-2">·</span>
        <NavLink to="/terms" className="hover:text-text">{t("Terms")}</NavLink>
        <span className="mx-2">·</span>
        <span>{t("Informational use only. Not investment advice.")}</span>
      </footer>
    </div>
  );
}
