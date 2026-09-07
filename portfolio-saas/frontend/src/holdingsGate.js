/** Shared onboarding gate: does any account carry at least one holding? */
export function hasAnyHoldings(accounts) {
  return accounts.some((a) => a.holdings?.length);
}
