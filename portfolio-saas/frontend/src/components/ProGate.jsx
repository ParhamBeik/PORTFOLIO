import { useUpgrade } from "../hooks/useUpgrade.js";

// Pro gate: renders children for Pro users, the standard upsell block
// (same markup/classes as the old per-page copies) for everyone else.
export default function ProGate({ user, pitch, children }) {
  const { upgrade, busy, error } = useUpgrade();

  if (user?.is_pro) return children;

  return (
    <div className="upsell">
      <h2>🔒 This is a Pro feature</h2>
      <p>{pitch}</p>
      <button className="primary big" onClick={upgrade} disabled={busy}>
        {busy ? "Redirecting…" : "Upgrade to Pro"}
      </button>
      {error && <p className="error small">{error}</p>}
    </div>
  );
}
