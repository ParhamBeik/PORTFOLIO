import { useSearchParams } from "react-router-dom";
import Dashboard from "./Dashboard.jsx";
import Family from "./Family.jsx";
import Ledger from "./Ledger.jsx";
import Onboarding from "./Onboarding.jsx";
import AssetHistory from "./AssetHistory.jsx";
import Comparison from "./Comparison.jsx";
import Guidance from "./Guidance.jsx";
import Risk from "./Risk.jsx";
import { Tabs } from "../components/ui.jsx";

function Destination({ choices, initial, testId, user }) {
  const [params, setParams] = useSearchParams();
  const requested = params.get("view");
  const value = choices.some((choice) => choice.value === requested) ? requested : initial;
  const Current = choices.find((choice) => choice.value === value).component;

  return (
    <div>
      <Tabs
        options={choices.map(({ value: item, label }) => ({ value: item, label }))}
        value={value}
        onChange={(next) => setParams({ view: next })}
        label="Section"
        testId={testId}
      />
      <div className="mt-6"><Current user={user} /></div>
    </div>
  );
}

export function PortfolioDestination({ user }) {
  const choices = [
    { value: "summary", label: "Summary", component: Dashboard },
    { value: "breakdown", label: "Breakdown", component: Family },
    { value: "risk", label: "Risk", component: Risk },
  ];
  return <Destination choices={choices} initial="summary" testId="portfolio-section" user={user} />;
}

export function ActivityDestination() {
  const choices = [
    { value: "transactions", label: "Transactions", component: Ledger },
    { value: "holdings", label: "Add holdings", component: Onboarding },
  ];
  return <Destination choices={choices} initial="transactions" testId="activity-section" />;
}

export function MarketsDestination() {
  const choices = [
    { value: "prices", label: "Price history", component: AssetHistory },
    { value: "comparison", label: "Comparison", component: Comparison },
  ];
  return <Destination choices={choices} initial="prices" testId="markets-section" />;
}

export function GuidanceDestination({ user, onUserChange }) {
  return <Guidance user={user} onUserChange={onUserChange} />;
}
