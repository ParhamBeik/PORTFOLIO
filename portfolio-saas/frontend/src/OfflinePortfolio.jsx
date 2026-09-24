import { useEffect, useState } from "react";
import { App as NativeApp } from "@capacitor/app";
import { holdingLabel, money, pct, quantity } from "./format.js";
import { unlockOfflineSnapshot } from "./mobile.js";

function Trend({ series, basis }) {
  const values = (series || []).map((row) => Number(row.total)).filter(Number.isFinite);
  if (values.length < 2) return <p className="text-sm text-muted">No saved chart for this view.</p>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const points = values.map((value, i) =>
    `${(i / (values.length - 1)) * 100},${42 - ((value - min) / span) * 36}`
  ).join(" ");
  return (
    <div>
      <svg viewBox="0 0 100 48" role="img" aria-label="Saved 30-day net-worth trend" className="h-44 w-full" preserveAspectRatio="none">
        <polyline points={points} fill="none" stroke="var(--c-accent)" strokeWidth="1.3" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="flex justify-between text-xs text-muted">
        <span>{money(values[0], basis)}</span><span>{money(values.at(-1), basis)}</span>
      </div>
    </div>
  );
}

export default function OfflinePortfolio({ available, onReconnect, onSignOut }) {
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [scope, setScope] = useState("");

  useEffect(() => {
    let live = true;
    let listener;
    let pauseListener;
    const lock = () => {
      setSnapshot(null);
      setScope("");
    };
    const onVisibilityChange = () => {
      if (document.hidden) lock();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    NativeApp.addListener("appStateChange", ({ isActive }) => {
      if (!isActive) lock();
    }).then((handle) => { if (live) listener = handle; else handle.remove(); });
    NativeApp.addListener("pause", lock).then((handle) => { if (live) pauseListener = handle; else handle.remove(); });
    return () => {
      live = false;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      listener?.remove();
      pauseListener?.remove();
    };
  }, []);

  const unlock = async () => {
    setBusy(true);
    setError("");
    try {
      setSnapshot(await unlockOfflineSnapshot());
    } catch (caught) {
      setError(caught.message || "Device unlock was cancelled.");
    } finally {
      setBusy(false);
    }
  };

  const reconnect = async () => {
    setError("");
    try {
      await onReconnect();
    } catch (caught) {
      setError(caught.message || "Still offline.");
    }
  };

  const scopes = Object.entries(snapshot?.scopes || {}).filter(([, value]) => value.valuation);
  const selectedKey = scopes.some(([key]) => key === scope) ? scope : scopes[0]?.[0];
  const selected = snapshot?.scopes?.[selectedKey];
  const [accountId, requestedBasis = "nominal_toman"] = selectedKey?.split(":") || [];
  const basis = selected?.valuation?.basis || requestedBasis;
  const classes = new Map();
  for (const item of selected?.valuation?.items || []) {
    const cls = item.class || "Other";
    classes.set(cls, (classes.get(cls) || 0) + Number(item.value || 0));
  }
  const total = Number(selected?.valuation?.total || 0);

  return (
    <main className="mx-auto min-h-[100svh] max-w-3xl space-y-5 px-4 py-6 pb-20" data-testid="offline-portfolio">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Holdings</h1>
        <p role="status" className="rounded-lg border border-[var(--c-warn-text)] px-3 py-2 text-sm text-[var(--c-warn-text)]">
          Offline · saved data only. Changes are unavailable until you reconnect.
        </p>
      </header>
      {!snapshot && available ? (
        <section className="rounded-xl border border-border bg-panel p-5">
          <h2 className="font-semibold">Unlock your saved portfolio</h2>
          <p className="my-3 text-sm text-muted">Use your device screen lock to view the last saved figures.</p>
          <button className="rounded-lg bg-[var(--c-accent-fill)] px-4 py-3 font-medium text-white" disabled={busy} onClick={unlock} data-testid="offline-unlock">
            {busy ? "Unlocking…" : "Unlock saved data"}
          </button>
          {error && <p role="alert" className="mt-3 text-sm text-[var(--c-critical-text)]">{error}</p>}
        </section>
      ) : snapshot ? (
        <>
          <label className="block text-sm">Saved view
            <select className="mt-1 w-full rounded-lg border border-border bg-panel p-3" value={selectedKey} onChange={(event) => setScope(event.target.value)}>
              {scopes.map(([key]) => {
                const [id, viewBasis] = key.split(":");
                const name = id === "all" ? "All portfolios" : snapshot.accounts.find((a) => String(a.id) === id)?.name || `Portfolio ${id}`;
                return <option key={key} value={key}>{name} · {viewBasis.replaceAll("_", " ")}</option>;
              })}
            </select>
          </label>
          <p className="text-xs text-muted" data-testid="offline-updated">Saved {new Date(selected.updatedAt).toLocaleString()} · {accountId === "all" ? "All portfolios" : "Selected portfolio"}</p>
          <section className="rounded-xl border border-border bg-panel p-5">
            <h2 className="text-sm text-muted">Total value</h2>
            <p className="break-words text-3xl font-semibold" data-testid="offline-total">{money(selected.valuation.total, basis)}</p>
          </section>
          <section className="rounded-xl border border-border bg-panel p-5">
            <h2 className="mb-3 font-semibold">Net worth · 30 days</h2>
            <Trend series={selected.history?.series} basis={basis} />
          </section>
          <section className="rounded-xl border border-border bg-panel p-5">
            <h2 className="mb-3 font-semibold">Allocation</h2>
            <ul>{[...classes].sort((a, b) => b[1] - a[1]).map(([name, value]) =>
              <li key={name} className="flex justify-between gap-3 border-b border-border py-2 text-sm"><span>{name}</span><span>{total ? pct(value / total) : "—"}</span></li>
            )}</ul>
          </section>
          <section className="rounded-xl border border-border bg-panel p-5">
            <h2 className="mb-3 font-semibold">Holdings</h2>
            <ul>{(selected.valuation.items || []).map((item, index) =>
              <li key={`${item.account_id || "all"}:${item.asset_key || item.asset || index}`} className="flex justify-between gap-3 border-b border-border py-3 text-sm">
                <span dir="auto">{holdingLabel(item)}<small className="block text-muted">{quantity(item.quantity, item.quantity_step)}</small></span>
                <span className="text-right">{money(item.value, basis)}</span>
              </li>
            )}</ul>
          </section>
        </>
      ) : <p className="rounded-xl border border-border bg-panel p-5 text-sm text-muted">No saved portfolio is available on this device. Reconnect to load your account.</p>}
      <button className="rounded-lg border border-border bg-panel px-4 py-3 text-sm" onClick={reconnect} data-testid="offline-retry">Try online again</button>
      <button className="ml-3 rounded-lg border border-border bg-panel px-4 py-3 text-sm" onClick={onSignOut}>Sign out on this device</button>
      {snapshot && error && <p role="alert" className="text-sm text-[var(--c-critical-text)]">{error}</p>}
    </main>
  );
}
