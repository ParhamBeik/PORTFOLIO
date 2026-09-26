-- Read-only production snapshot audit. Run against the portfolio database.
-- Counts are current catalog observations, not historical industry membership.

SELECT source, category, count(*) AS instruments,
       count(*) FILTER (WHERE btrim(provider_group) <> '') AS grouped
FROM marketdata_marketinstrument
GROUP BY source, category
ORDER BY source, category;

SELECT i.category, count(*) AS detailed_metadata_rows
FROM marketdata_stocksymbolmetadata AS m
JOIN marketdata_marketinstrument AS i
  ON i.source = 'tsetmc' AND i.symbol = m.l18
GROUP BY i.category
ORDER BY i.category;

WITH codal_stocks AS (
  SELECT DISTINCT c.symbol
  FROM marketdata_codalannouncement AS c
  JOIN marketdata_marketinstrument AS i
    ON i.source = 'tsetmc' AND i.category = 'stock' AND i.symbol = c.symbol
)
SELECT count(*) AS codal_stock_symbols,
       count(m.l18) AS symbols_with_detailed_metadata
FROM codal_stocks AS c
LEFT JOIN marketdata_stocksymbolmetadata AS m ON m.l18 = c.symbol;

SELECT count(*) AS catalog_groups_with_multiple_detailed_sectors
FROM (
  SELECT i.provider_group
  FROM marketdata_marketinstrument AS i
  JOIN marketdata_stocksymbolmetadata AS m ON m.l18 = i.symbol
  WHERE i.source = 'tsetmc' AND i.category = 'stock' AND i.provider_group <> ''
  GROUP BY i.provider_group
  HAVING count(DISTINCT m.sector) > 1
) AS ambiguous;
