import { lazy, Suspense } from "react";
import { useSearchParams } from "react-router-dom";
import { Loading, Tabs } from "../components/ui.jsx";

// One chunk per page. These used to be static imports, which folded every page
// -- and through them charts.jsx and echarts -- into the single chunk every
// signed-in route loads, so opening the ledger downloaded the risk page too.
const Dashboard = lazy(() => import("./Dashboard.jsx"));
const DataHealth = lazy(() => import("./DataHealth.jsx"));
const Family = lazy(() => import("./Family.jsx"));
const Ledger = lazy(() => import("./Ledger.jsx"));
const Onboarding = lazy(() => import("./Onboarding.jsx"));
const AssetHistory = lazy(() => import("./AssetHistory.jsx"));
const Comparison = lazy(() => import("./Comparison.jsx"));
const Guidance = lazy(() => import("./Guidance.jsx"));
const Explore = lazy(() => import("./Explore.jsx"));
const Watchlist = lazy(() => import("./Watchlist.jsx"));

const page = (element) => <Suspense fallback={<Loading />}>{element}</Suspense>;

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
      <div className="mt-6">{page(<Current user={user} />)}</div>
    </div>
  );
}

export function PortfolioDestination({ user }) {
  const choices = [
    { value: "summary", label: "Summary", component: Dashboard },
    { value: "breakdown", label: "Breakdown", component: Family },
    { value: "health", label: "Data health", component: DataHealth },
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

export function ResearchDestination() {
  const choices = [
    { value: "companies", label: "Companies", component: Explore },
    { value: "watchlist", label: "Watchlist", component: Watchlist },
    { value: "prices", label: "Price history", component: AssetHistory },
  ];
  return <Destination choices={choices} initial="companies" testId="research-section" />;
}

export function CompareDestination() {
  return page(<Comparison />);
}

export function GuidanceDestination({ user, onUserChange }) {
  return page(<Guidance user={user} onUserChange={onUserChange} />);
}
