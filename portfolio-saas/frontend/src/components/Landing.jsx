import { Link } from "react-router-dom";

export default function Landing() {
  return (
    <div className="landing-page" style={{
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      minHeight: "80vh",
      padding: "2rem",
      textAlign: "center",
      background: "radial-gradient(circle at top, var(--panel-3) 0%, transparent 70%)"
    }}>
      <div style={{ maxWidth: "800px" }}>
        <div style={{
          display: "inline-block",
          padding: "6px 12px",
          background: "var(--panel-2)",
          border: "1px solid var(--border)",
          borderRadius: "20px",
          fontSize: "0.85rem",
          fontWeight: "600",
          color: "var(--accent)",
          marginBottom: "1.5rem"
        }}>
          Introducing Lattice 2.0
        </div>
        
        <h1 style={{
          fontSize: "3.5rem",
          fontWeight: "800",
          lineHeight: "1.15",
          letterSpacing: "-0.03em",
          marginBottom: "1.5rem",
          background: "linear-gradient(135deg, var(--foreground) 30%, var(--accent) 100%)",
          WebkitBackgroundClip: "text",
          WebkitTextFillColor: "transparent"
        }}>
          The Intelligent Operating System for Modern Portfolios
        </h1>
        
        <p style={{
          fontSize: "1.25rem",
          color: "var(--muted)",
          lineHeight: "1.6",
          marginBottom: "2.5rem"
        }}>
          Track net worth in real-time, compute optimal asset allocations, net active liabilities, and analyze risk metrics using verified institutional data.
        </p>

        <div style={{ display: "flex", gap: "1rem", justifyContent: "center", marginBottom: "4rem" }}>
          <Link to="/signup" className="btn-accent" style={{ padding: "12px 24px", fontSize: "1.05rem", borderRadius: "8px", textDecoration: "none" }}>
            Get Started Free
          </Link>
          <Link to="/login" className="btn-secondary" style={{ padding: "12px 24px", fontSize: "1.05rem", borderRadius: "8px", textDecoration: "none" }}>
            Sign In
          </Link>
        </div>

        {/* Feature Grid */}
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
          gap: "1.5rem",
          marginTop: "2rem"
        }}>
          <div className="card" style={{ padding: "1.5rem", textAlign: "left" }}>
            <div style={{ fontSize: "2rem", marginBottom: "0.5rem" }}>⚡</div>
            <h4>Real-Time Valuations</h4>
            <p className="muted small">Instantaneous portfolio updates across multiple asset classes including Gold, Crypto, Stocks, and Real Estate.</p>
          </div>

          <div className="card" style={{ padding: "1.5rem", textAlign: "left" }}>
            <div style={{ fontSize: "2rem", marginBottom: "0.5rem" }}>📊</div>
            <h4>Efficient Frontier</h4>
            <p className="muted small">Advanced portfolio optimization using modern portfolio theory (MPT) to maximize returns per unit of risk.</p>
          </div>

          <div className="card" style={{ padding: "1.5rem", textAlign: "left" }}>
            <div style={{ fontSize: "2rem", marginBottom: "0.5rem" }}>🛡️</div>
            <h4>Liabilities Netting</h4>
            <p className="muted small">Seamlessly deduct active mortgages and generic liabilities to track true, net liquid wealth accurately.</p>
          </div>
        </div>
      </div>
    </div>
  );
}
