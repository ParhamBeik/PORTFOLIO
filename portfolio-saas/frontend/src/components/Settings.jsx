import { useEffect, useState } from "react";

export default function Settings({ user, setUser }) {
  const [basis, setBasis] = useState(localStorage.getItem("lattice:default-basis") || "nominal_toman");
  const [refreshInterval, setRefreshInterval] = useState(localStorage.getItem("lattice:refresh-interval") || "30");
  const [darkMode, setDarkMode] = useState(localStorage.getItem("lattice:dark-mode") !== "false");
  const [msg, setMsg] = useState("");

  const handleSave = (e) => {
    e.preventDefault();
    localStorage.setItem("lattice:default-basis", basis);
    localStorage.setItem("lattice:refresh-interval", refreshInterval);
    localStorage.setItem("lattice:dark-mode", darkMode);
    
    // Dispatch storage event to notify other components (like Shell / App)
    window.dispatchEvent(new Event("storage"));
    
    setMsg("Settings saved successfully.");
    setTimeout(() => setMsg(""), 3000);
  };

  return (
    <div className="settings-page dashboard" style={{ padding: "2rem" }}>
      <div className="card" style={{ maxWidth: "600px", margin: "0 auto" }}>
        <h2>⚙️ User Preferences & Settings</h2>
        <p className="muted small">Customize your default dashboard view, refresh cadence, and look-and-feel</p>

        {msg && <div className="pos margin-top small" style={{ padding: "8px 12px", background: "var(--panel-2)", borderLeft: "4px solid var(--pos)", borderRadius: "4px" }}>{msg}</div>}

        <form onSubmit={handleSave} className="margin-top" style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
          <div>
            <label htmlFor="settings-default-basis" style={{ fontWeight: 600, display: "block", marginBottom: "0.5rem" }}>
              Default Valuation Basis:
            </label>
            <select
              id="settings-default-basis"
              className="portfolio-select"
              value={basis}
              onChange={(e) => setBasis(e.target.value)}
              style={{ width: "100%", padding: "8px" }}
            >
              <option value="nominal_toman">Nominal Toman (IRT)</option>
              <option value="usd_denominated">USD-denominated (USD)</option>
              <option value="usdt_denominated">USDT-denominated (USDT)</option>
            </select>
            <span className="muted small" style={{ display: "block", marginTop: "4px" }}>
              The currency basis used automatically when loading your dashboard.
            </span>
          </div>

          <div>
            <label htmlFor="settings-refresh" style={{ fontWeight: 600, display: "block", marginBottom: "0.5rem" }}>
              Auto-Refresh Cadence (seconds):
            </label>
            <select
              id="settings-refresh"
              className="portfolio-select"
              value={refreshInterval}
              onChange={(e) => setRefreshInterval(e.target.value)}
              style={{ width: "100%", padding: "8px" }}
            >
              <option value="10">Every 10 seconds</option>
              <option value="30">Every 30 seconds</option>
              <option value="60">Every 60 seconds</option>
              <option value="0">Disabled (Manual only)</option>
            </select>
            <span className="muted small" style={{ display: "block", marginTop: "4px" }}>
              How frequently live prices and valuations are updated in the browser.
            </span>
          </div>

          <div>
            <label style={{ fontWeight: 600, display: "flex", alignItems: "center", gap: "8px", cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={darkMode}
                onChange={(e) => setDarkMode(e.target.checked)}
              />
              Enable High-Contrast Dark Mode
            </label>
            <span className="muted small" style={{ display: "block", marginTop: "4px", paddingLeft: "1.5rem" }}>
              Use the curated dark theme design to reduce eye strain.
            </span>
          </div>

          <div style={{ marginTop: "1rem" }}>
            <button type="submit" className="btn-accent" style={{ padding: "10px 20px" }}>
              Save Settings
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
