import { NavLink, Outlet } from "react-router-dom";
import ProGate from "./ProGate.jsx";

export default function OptimizationLayout({ user }) {
  return (
    <div className="pro-area">
      <ProGate
        user={user}
        pitch="Upgrade to Pro to access our portfolio scenario optimizer, risk-adjusted metrics, and advanced asset allocation insights."
      >
        <div className="pro-container">
          <nav className="tabs sub-tabs">
            <NavLink to="/optimization" end className={({ isActive }) => (isActive ? "active" : "")}>
              Optimizer
            </NavLink>
            <NavLink to="/optimization/insights" className={({ isActive }) => (isActive ? "active" : "")}>
              Advanced Insights
            </NavLink>
            <NavLink to="/optimization/analytics" className={({ isActive }) => (isActive ? "active" : "")}>
              Risk Analytics
            </NavLink>
          </nav>
          <div className="pro-content">
            <Outlet />
          </div>
        </div>
      </ProGate>
    </div>
  );
}
