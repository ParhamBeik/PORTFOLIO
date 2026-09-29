import { Link, useParams } from "react-router-dom";
import { publicStockDossier } from "../api.js";
import { useApi } from "../useApi.js";
import { Async, Card, PageHeader, Table } from "../components/ui.jsx";

export default function PublicDossier() {
  const { symbol } = useParams();
  const state = useApi(() => publicStockDossier(symbol), [symbol]);
  return <main className="mx-auto max-w-5xl space-y-5 px-4 py-8">
    <Link to="/" className="text-sm text-accent underline">Holdings research</Link>
    <Async {...state} testId="public-dossier">
      {(data) => <>
        <PageHeader title={`${data.company.name} · ${data.company.symbol}`} subtitle={[data.company.sector, data.company.isin].filter(Boolean).join(" · ")} />
        <Card title="Verified income statements" subtitle="Amounts are million Rials. Standalone and consolidated reports remain separate.">
          <Table caption="Filed income statements" mobileCards rows={data.financial_metrics.points || []} rowKey={(row) => `${row.period_end_jalali}:${row.scope}`} columns={[
            { key: "period", header: "Period", render: (row) => `${row.period_start_jalali}–${row.period_end_jalali}` },
            { key: "scope", header: "Scope", render: (row) => row.scope },
            { key: "audit", header: "Audit", render: (row) => row.audited ? "Audited" : "Unaudited" },
            { key: "profit", header: "Net profit · million Rial", render: (row) => row.net_profit },
            { key: "margin", header: "Net margin", render: (row) => `${row.net_margin_pct}%` },
            { key: "source", header: "Filing", render: (row) => row.source_url ? <a href={row.source_url} rel="noopener noreferrer" target="_blank" className="text-accent underline">Codal · {row.published_jalali}</a> : "Source unavailable" },
          ]} />
          <p className="mt-2 text-xs text-muted">{data.financial_metrics.verified_periods} verified periods; {data.financial_metrics.withheld_periods} withheld. Missing periods are gaps.</p>
        </Card>
        <Card title="Monthly sales" subtitle="Verified filing totals in million Rials.">
          <Table caption="Monthly sales" mobileCards rows={data.monthly_sales.points || []} rowKey={(row) => row.period_end_jalali} columns={[
            { key: "period", header: "Period", render: (row) => row.period_end_jalali },
            { key: "sales", header: "Sales · million Rial", render: (row) => row.value },
            { key: "source", header: "Filing", render: (row) => row.source_url ? <a href={row.source_url} rel="noopener noreferrer" target="_blank" className="text-accent underline">Codal</a> : "Source unavailable" },
          ]} />
          <p className="mt-2 text-xs text-muted">{data.monthly_sales.verified_periods} verified periods; {data.monthly_sales.withheld_periods} withheld.</p>
        </Card>
        <Card title="Balance sheets" subtitle="Verified point-in-time statements in million Rials; standalone and consolidated figures stay separate.">
          <Table caption="Filed balance sheets" mobileCards rows={data.balance_sheet.points || []} rowKey={(row) => `${row.period_end_jalali}:${row.scope}`} columns={[
            { key: "period", header: "Period", render: (row) => row.period_end_jalali },
            { key: "scope", header: "Scope", render: (row) => row.scope },
            { key: "audit", header: "Audit", render: (row) => row.audited ? "Audited" : "Unaudited" },
            { key: "assets", header: "Assets · million Rial", render: (row) => row.values.total_assets },
            { key: "liabilities", header: "Liabilities · million Rial", render: (row) => row.values.total_liabilities },
            { key: "equity", header: "Equity · million Rial", render: (row) => row.values.total_equity },
            { key: "source", header: "Filing", render: (row) => row.source_url ? <a href={row.source_url} rel="noopener noreferrer" target="_blank" className="text-accent underline">Codal · {row.published_jalali}</a> : "Source unavailable" },
          ]} />
          <p className="mt-2 text-xs text-muted">{data.balance_sheet.verified_periods} verified periods; {data.balance_sheet.withheld_periods} withheld. Missing periods are gaps.</p>
        </Card>
        <p className="text-sm text-muted">Price and FX data are withheld pending redistribution rights. Total investor returns require certified dividends and corporate actions.</p>
      </>}
    </Async>
  </main>;
}
