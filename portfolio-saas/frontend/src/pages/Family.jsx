import { valuation } from "../api.js";
import { usePortfolio } from "../components/PortfolioContext.jsx";
import { Async, Card, Empty, PageHeader, StatTile, Table } from "../components/ui.jsx";
import { pct, toman } from "../format.js";
import { useApi } from "../useApi.js";

export default function Family() {
  const { basis } = usePortfolio();
  const state = useApi(() => valuation(null, basis), [basis], { pollMs: 60000 });

  return (
    <div>
      <PageHeader
        title="Family"
        subtitle="Each named portfolio is one person. Totals use the same live valuation as the dashboard."
      />
      <Async {...state} testId="family-body">
        {(data) => {
          const rows = data.accounts || [];
          const grand = Number(data.total) || 0;
          if (!rows.length) {
            return <Empty testId="family-empty">No portfolios yet.</Empty>;
          }
          return (
            <>
              <StatTile label="Family total" value={toman(grand)} testId="family-total" />
              <Card title="By person" className="mt-5" testId="family-table-card">
                <Table
                  testId="family-table"
                  rowKey={(r) => r.id}
                  rows={rows}
                  columns={[
                    { key: "name", header: "Person", render: (r) => r.name },
                    { key: "total", header: "Net worth", align: "right", render: (r) => toman(r.total) },
                    {
                      key: "share",
                      header: "Share",
                      align: "right",
                      render: (r) => pct(grand ? Number(r.total) / grand : 0),
                    },
                    {
                      key: "holdings",
                      header: "Holdings",
                      align: "right",
                      render: (r) => String((r.items || []).length),
                    },
                  ]}
                />
              </Card>
            </>
          );
        }}
      </Async>
    </div>
  );
}
