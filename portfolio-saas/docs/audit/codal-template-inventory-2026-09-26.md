# Codal statement template inventory — 2026-09-26 08:43 UTC

This is a read-only scan of the running warehouse and MinIO object store, not a claim of complete financial coverage. The selection was stored HTML artifacts where the old report category is `2` **or** the announcement `doc_type` is `financial_statements`. The legacy category has mixed provenance, so this is a candidate pool, not a complete or certified universe. At scan time it contained 5,913 artifacts across 763 symbols; 3,506 artifacts across 760 symbols had titles without the literal `(شرکت` subsidiary marker.

```sql
SELECT count(*), count(DISTINCT n.symbol),
       count(*) FILTER (WHERE n.title NOT LIKE '%(شرکت%') AS parent_like,
       count(DISTINCT n.symbol) FILTER (WHERE n.title NOT LIKE '%(شرکت%') AS parent_like_symbols
FROM marketdata_codalartifact a
JOIN marketdata_codalreport r ON r.id = a.report_id
JOIN marketdata_codalannouncement n ON n.id = r.announcement_id
WHERE a.kind = 'html' AND a.fetch_status = 'stored'
  AND (r.category = 2 OR n.doc_type = 'financial_statements');
```

All 5,913 selected MinIO objects were read and SHA-256 checked against their artifact rows; none mismatched. Their total response bytes were 474,062,392. Of these, 1,453 contained an embedded `var datasource =` declaration and 4,460 did not. The embedded `title_En` field spans many issuer-industry and template versions. A title without the subsidiary marker is only *parent-like*; the parser separately verifies issuer and symbol in the source HTML.

| 1405 parent-like V9 income template | Stored candidate pages | Certified income pages | Balance-sheet option advertised |
| --- | ---: | ---: | ---: |
| Listed, standalone | 37 | 34 | 37 |
| Listed, consolidated | 77 | 72 | 77 |
| OTC, standalone | 31 | 25 | 31 |
| OTC, consolidated | 37 | 33 | 37 |
| Registered, standalone | 26 | 14 | 25 |
| Registered, consolidated | 14 | 12 | 14 |
| **Total** | **222** | **190** | **221** |

The certification count ran the draft V9 income parser against each original stored page and its announcement identity, period, scope, and audit status. It tests exact table versions, labeled cells, million-Rial unit, and gross/net arithmetic. The 190 pages are *possible* source-backed historical income readings after a controlled backfill, not currently published production facts. Corrections can further reduce displayed coverage, and these counts do not establish unique-company or full-stock-universe coverage. The 221 balance options are advertised URLs, not retrieved or certified balance sheets.

The first 114 listed-template pages yielded 106 certified income pages. Six rejected pages had no source HTML symbol; two `خچرخش` pages had current-period cells dated `1404/03/31` while the filing and datasource were dated `1404/12/29`. The parser correctly withheld all eight. Remaining OTC/registered refusals need row-level review before changing validation rules.

Four additional public source pairs were captured for regression tests. Each original income page and its separately fetched balance sheet used the same V9 sheet/table IDs and passed exact-cell and arithmetic checks. The SHA-256 columns identify the exact local fixture responses, not every response Codal may generate for a URL.

| Venue and scope | Original income filing | Separate balance sheet | Income HTML SHA-256 | Balance HTML SHA-256 |
| --- | --- | --- | --- | --- |
| OTC standalone, `بجهرم` | [Codal](https://codal.ir/Reports/Decision.aspx?LetterSerial=mnKmuErJuqPJH4lQQQaQQQvCwnOg%3d%3d&rt=0&let=6&ct=0&ft=-1) | [sheet 0](https://codal.ir/Reports/Decision.aspx?LetterSerial=mnKmuErJuqPJH4lQQQaQQQvCwnOg%3d%3d&rt=0&let=6&ct=0&ft=-1&sheetId=0) | `9b1b4d9b6355fd04c276e2da0bc23cb620b040d5761a8db95a2c415840308141` | `dbe205dd5a50db1e9f73cd774304a8543bc77ace0207957753129da14d44f264` |
| OTC consolidated, `کرومیت` | [Codal](https://codal.ir/Reports/Decision.aspx?LetterSerial=r9mC89jB39KVhOadOOObOOOKQIXg%3d%3d&rt=0&let=6&ct=0&ft=-1) | [sheet 14](https://codal.ir/Reports/Decision.aspx?LetterSerial=r9mC89jB39KVhOadOOObOOOKQIXg%3d%3d&rt=0&let=6&ct=0&ft=-1&sheetId=14) | `0a96748040ea7617100b011a88bd83d043dfaad5191bab9f36d3a08b8526ea28` | `542d1744c93474103f7d4a1b4f5d61da7c54fdd6d14d5f5fdd6bed7a97a6044e` |
| Registered standalone, `لکما` | [Codal](https://codal.ir/Reports/Decision.aspx?LetterSerial=NXh0zzf8btuBRc7NnlYrdg%3d%3d&rt=0&let=6&ct=0&ft=-1) | [sheet 0](https://codal.ir/Reports/Decision.aspx?LetterSerial=NXh0zzf8btuBRc7NnlYrdg%3d%3d&rt=0&let=6&ct=0&ft=-1&sheetId=0) | `7ed861e8345829745fce019f520d8a0728700a686b7819f3c6c507ee4391f350` | `ec32efe80a32e9e7896915df42e47e468d4952b50dd680d4829d94bd284c6197` |
| Registered consolidated, `دحاوی` | [Codal](https://codal.ir/Reports/Decision.aspx?LetterSerial=i8COOObOOOXrPKfcWIiu70yhDpMA%3d%3d&rt=0&let=6&ct=0&ft=-1) | [sheet 14](https://codal.ir/Reports/Decision.aspx?LetterSerial=i8COOObOOOXrPKfcWIiu70yhDpMA%3d%3d&rt=0&let=6&ct=0&ft=-1&sheetId=14) | `4b6c89d9f27330839d45a3be050905c1c2434907aeeefcabf327817ddfbd22fa` | `715c502ec3992098b24def60ee7d22e34a99889851d0bf98991ce74d6ef15cb1` |

Codal generates a different ASP.NET `__VIEWSTATE` between some requests; the registered standalone balance page had stable datasource JSON across two responses but different raw SHA-256 values. Each stored response therefore retains **its own** hash; byte equality across separate HTTP requests is not presumed.

No Codal requests, database writes, or object-store writes were made during the 5,913-object inventory or 1,286-page certification pass. The four balance-sheet fixture pairs were separately fetched from public Codal URLs through the VPS. Production has not run the draft backfill or enabled a Codal worker for this change.
