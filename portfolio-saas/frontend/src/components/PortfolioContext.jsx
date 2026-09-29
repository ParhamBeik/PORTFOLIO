// Lattice top-bar portfolio selector state.
//
// `activeId` is the currently selected portfolio: null = "All portfolios"
// (the aggregate view across every account the user owns), or an account id.
// Every signed-in entry begins at All portfolios. Selection lasts for this
// session; every page reads it through `usePortfolio()` to scope API calls.
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { listAccounts } from "../api.js";

const PortfolioContext = createContext(null);

export function PortfolioProvider({ children, enabled }) {
  const [accounts, setAccounts] = useState([]);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState("");
  const [activeId, setActiveId] = useState(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const list = await listAccounts();
      setAccounts(list);
      setRevision((cur) => cur + 1);
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
  }, []);

  const [basis, setBasisState] = useState("nominal_toman");

  const setBasis = useCallback((newBasis) => {
    setBasisState(newBasis);
  }, []);

  return (
    <PortfolioContext.Provider
      value={{ accounts, activeId, setActive, reload, loading, error, basis, setBasis, revision }}
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
