// Lattice top-bar portfolio selector state.
//
// `activeId` is the currently selected portfolio: null = "All portfolios"
// (the aggregate view across every account the user owns), or an account id.
// It persists to localStorage so a refresh keeps the selected portfolio, and
// every page reads it through `usePortfolio()` to scope its API calls via
// `?account=`.
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { listAccounts } from "../api.js";

const PortfolioContext = createContext(null);
const ACTIVE_KEY = "lattice_active_account";

export function PortfolioProvider({ children, enabled }) {
  const [accounts, setAccounts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [activeId, setActiveId] = useState(() => {
    const stored = localStorage.getItem(ACTIVE_KEY);
    const id = stored ? Number(stored) : null;
    return Number.isFinite(id) ? id : null;
  });

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const list = await listAccounts();
      setAccounts(list);
      // If the active portfolio was deleted (or is stale), fall back to All.
      setActiveId((cur) =>
        cur != null && list.some((a) => a.id === cur) ? cur : null
      );
      return list;
    } catch (requestError) {
      setError(requestError.message || "Could not load portfolios.");
      return [];
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) reload();
    else {
      setAccounts([]);
      setActiveId(null);
      setError("");
      setLoading(false);
    }
  }, [enabled, reload]);

  const setActive = useCallback((id) => {
    setActiveId(id);
    if (id == null) localStorage.removeItem(ACTIVE_KEY);
    else localStorage.setItem(ACTIVE_KEY, String(id));
  }, []);

  const [basis, setBasisState] = useState(() => {
    const saved = localStorage.getItem("lattice_basis");
    if (saved === "usd_real") return "usd_denominated";
    if (saved === "nominal") return "nominal_toman";
    return saved || "nominal_toman";
  });

  const setBasis = useCallback((newBasis) => {
    setBasisState(newBasis);
    localStorage.setItem("lattice_basis", newBasis);
  }, []);

  return (
    <PortfolioContext.Provider
      value={{ accounts, activeId, setActive, reload, loading, error, basis, setBasis }}
    >
      {children}
    </PortfolioContext.Provider>
  );
}

export function usePortfolio() {
  const ctx = useContext(PortfolioContext);
  if (!ctx) throw new Error("usePortfolio must be used within PortfolioProvider");
  return ctx;
}
