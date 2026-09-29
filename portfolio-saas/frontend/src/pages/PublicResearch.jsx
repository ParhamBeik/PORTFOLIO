import { Link } from "react-router-dom";
import { publicResearchCatalog } from "../api.js";
import { Card, Async } from "../components/ui.jsx";
import { useApi } from "../useApi.js";

export default function PublicResearch() {
  const catalog = useApi(() => publicResearchCatalog(), []);
  return (
    <main className="mx-auto max-w-5xl space-y-8 px-4 py-10 sm:py-16">
      <header className="space-y-4">
        <p className="text-sm font-semibold uppercase tracking-wide text-accent">Holdings research</p>
        <h1 className="max-w-3xl text-4xl font-semibold tracking-tight text-text sm:text-5xl">
          Understand a company before you invest.
        </h1>
        <p className="max-w-2xl text-lg text-muted">
          Explore Iranian listed companies through filings, context, and clearly identified data gaps.
          Public dossiers will appear here as their sources and display rights are certified.
        </p>
        <div className="flex flex-wrap gap-3">
          <Link className="rounded-lg bg-[var(--c-accent-fill)] px-5 py-3 font-medium text-white" to="/signup">Create an account</Link>
          <Link className="rounded-lg border border-border px-5 py-3 font-medium text-text" to="/login">Sign in</Link>
        </div>
      </header>
      <div className="grid gap-4 md:grid-cols-3">
        <Card title="Filing-backed research"><p className="text-sm text-muted">Statements, sales, and company context with source dates and units.</p></Card>
        <Card title="Your portfolios"><p className="text-sm text-muted">Private holdings, activity, allocation, and performance in one place.</p></Card>
        <Card title="Visible coverage"><p className="text-sm text-muted">Missing or unverified history is shown as a gap, never as a measured return.</p></Card>
      </div>
      <Card title="Public company dossiers">
        <Async {...catalog} testId="public-research-catalog">
          {(data) => data.results?.length ? <ul className="grid gap-2 sm:grid-cols-2">
            {data.results.map((company) => <li key={company.symbol}>
              <Link to={`/research/stocks/${encodeURIComponent(company.symbol)}`} className="text-accent underline">{company.symbol} · {company.name}</Link>
              {company.sector && <span className="ml-2 text-xs text-muted">{company.sector}</span>}
            </li>)}
          </ul> : <p className="text-sm text-muted">Public dossiers are being certified. Approved companies will appear here.</p>}
        </Async>
      </Card>
      <footer className="flex gap-4 text-sm text-muted"><Link to="/privacy">Privacy</Link><Link to="/terms">Terms</Link></footer>
    </main>
  );
}
