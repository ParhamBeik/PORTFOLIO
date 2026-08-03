import { Link } from "react-router-dom";

export default function Pricing() {
  return (
    <div className="pricing-page" style={{
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      minHeight: "80vh",
      padding: "3rem 2rem",
      background: "radial-gradient(circle at top, var(--panel-3) 0%, transparent 60%)"
    }}>
      <div style={{ maxWidth: "1000px", width: "100%", textAlign: "center" }}>
        <h2 style={{ fontSize: "2.5rem", fontWeight: "800", marginBottom: "1rem" }}>
          Simple, Transparent Pricing
        </h2>
        <p className="muted" style={{ fontSize: "1.15rem", marginBottom: "3rem" }}>
          Choose the plan that fits your portfolio complexity and risk appetite.
        </p>

        {/* Pricing Cards */}
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
          gap: "2rem",
          marginBottom: "4rem"
        }}>
          {/* Free Tier */}
          <div className="card" style={{ padding: "2.5rem 2rem", textAlign: "left", display: "flex", flexDirection: "column" }}>
            <h3 style={{ margin: 0, fontSize: "1.5rem" }}>Free Tier</h3>
            <p className="muted small" style={{ marginTop: "0.5rem", minHeight: "40px" }}>Essential tracking for casual investors starting out.</p>
            <div style={{ margin: "1.5rem 0", display: "flex", alignItems: "baseline" }}>
              <span style={{ fontSize: "2.5rem", fontWeight: "800" }}>$0</span>
              <span className="muted" style={{ marginLeft: "4px" }}>/ month</span>
            </div>
            <ul style={{ paddingLeft: "1.2rem", margin: "0 0 2rem 0", lineHeight: "2", color: "var(--muted)", flexGrow: 1 }}>
              <li>Track up to 2 accounts</li>
              <li>Track up to 10 holdings</li>
              <li>Real-time asset valuations</li>
              <li>Basic transactions history</li>
            </ul>
            <Link to="/signup" className="btn-secondary" style={{ textAlign: "center", textDecoration: "none", width: "100%", display: "block" }}>
              Get Started
            </Link>
          </div>

          {/* Pro Tier */}
          <div className="card" style={{ padding: "2.5rem 2rem", textAlign: "left", display: "flex", flexDirection: "column", border: "2px solid var(--accent)", position: "relative" }}>
            <div style={{
              position: "absolute",
              top: "-12px",
              left: "50%",
              transform: "translateX(-50%)",
              background: "var(--accent)",
              color: "#fff",
              padding: "2px 12px",
              borderRadius: "12px",
              fontSize: "0.75rem",
              fontWeight: "700",
              textTransform: "uppercase"
            }}>
              Most Popular
            </div>
            <h3 style={{ margin: 0, fontSize: "1.5rem" }}>Pro Trader</h3>
            <p className="muted small" style={{ marginTop: "0.5rem", minHeight: "40px" }}>Advanced analytics and efficient frontier optimization tools.</p>
            <div style={{ margin: "1.5rem 0", display: "flex", alignItems: "baseline" }}>
              <span style={{ fontSize: "2.5rem", fontWeight: "800" }}>$29</span>
              <span className="muted" style={{ marginLeft: "4px" }}>/ month</span>
            </div>
            <ul style={{ paddingLeft: "1.2rem", margin: "0 0 2rem 0", lineHeight: "2", flexGrow: 1 }}>
              <li><strong>Unlimited</strong> accounts</li>
              <li><strong>Unlimited</strong> holdings</li>
              <li>Efficient Frontier Optimization</li>
              <li>Time Machine Backtesting</li>
              <li>Mortgage & Liabilities Netting</li>
              <li>Priority support</li>
            </ul>
            <Link to="/signup" className="btn-accent" style={{ textAlign: "center", textDecoration: "none", width: "100%", display: "block" }}>
              Upgrade to Pro
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
