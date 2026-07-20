import { useState } from "react";
import { createZarinpalPayment } from "../api.js";

// The upgrade()/busy/error trio previously copy-pasted into Insights,
// Analytics, Optimization and Billing. One hook, one flow.
export function useUpgrade() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function upgrade() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const { redirect_url } = await createZarinpalPayment();
      window.location.href = redirect_url; // hand off to Zarinpal's hosted page
    } catch (e) {
      setError(e.message || "Could not start the payment.");
      setBusy(false);
    }
  }

  return { upgrade, busy, error };
}
