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

The stored statement-candidate HTML is heavily recent. A read-only count by announcement **publication year** found 220 pages through 1402, 585 in 1403, 2,578 in 1404, and 2,530 in 1405; these sum to 5,913. Thus 5,108 pages (86.4%) were published in 1404–1405. Publication year is not the financial period, and these are mixed candidate pages rather than certified statements. Historical research will need a source-backed multi-year acquisition plan; old publication dates alone cannot establish five-year comparable coverage.

| 1405 parent-like V9 income template | Stored candidate pages | Certified income pages | Balance-sheet option advertised |
| --- | ---: | ---: | ---: |
| Listed, standalone | 37 | 34 | 37 |
| Listed, consolidated | 77 | 72 | 77 |
| OTC, standalone | 31 | 25 | 31 |
| OTC, consolidated | 37 | 33 | 37 |
| Registered, standalone | 26 | 15 | 25 |
| Registered, consolidated | 14 | 12 | 14 |
| **Total** | **222** | **191** | **221** |

The certification count reran the draft V9 income parser against all 222 original, SHA-256-checked stored pages and their announcement identity, period, scope, and audit status on 2026-09-26 at 10:17 UTC. It tests exact table versions, labeled cells, million-Rial unit, and gross/net arithmetic. The 191 pages are *possible* source-backed historical income readings after a controlled backfill, not currently published production facts. Corrections can further reduce displayed coverage, and these counts do not establish complete stock-universe coverage. The 221 balance options are advertised URLs, not retrieved or certified balance sheets.

The 191 accepted pages span 166 symbols and 173 distinct `(symbol, consolidated scope, period end, audit flag)` groups. Eighteen groups have two versions, and 18 accepted titles mark corrections; revision-aware *displayed* coverage still needs measurement. Only seven symbols have both standalone and consolidated accepted pages. **190 of 191 pages end on 1404/12/29; one ends on 1405/03/31.** This cohort cannot support a five-year trend or cross-universe ranking. A read-only production query found zero stored HTML artifacts with any `sheetId` URL, so the separate balance sheets will require explicit public Codal fetches before certification.

The draft `backfill_balance_sheets` selection and `classify_announcement` gates were separately replayed against the same 222 archived pages and read-only production announcement metadata. All 191 parser-accepted pages have an original filing URL, a statements category, a recognized period and audit flag, and an advertised balance-sheet link. The other 31 remain parser refusals. This checks *eligibility to attempt* a balance fetch; it does not certify all 191 balance responses or imply that the command has run on production.

The first 114 listed-template pages yielded 106 certified income pages. Across all six template variants, the remaining 31 refusals break down as 26 pages without an HTML symbol field, two `خچرخش` pages whose current-period cells say `1404/03/31` while the filing and datasource say `1404/12/29`, and three registered standalone pages with empty revenue cells that fail the positive-revenue or gross-profit check. These remain withheld. One registered standalone `کهرام` page had a UTF-8 declaration about 35 KB into the response; BeautifulSoup guessed a legacy encoding when passed raw bytes. Decoding its SHA-256-checked UTF-8 bytes before parsing restores six reconciled income facts, including revenue 2,089,753 and net profit −635,952 million Rial. The exact response (artifact 51262, SHA-256 `62711d9962bfaead32d26b6fbd3c1b291cabc3e4c69d862d15af3c80b2b7c1d3`) is a regression fixture. The other 190 accepted results were byte-for-byte unchanged by explicit UTF-8 decoding. A missing source symbol cannot be inferred from the database row without weakening issuer verification.

Four additional public source pairs were captured earlier for regression tests. On 2026-09-26, all six observed variants were fetched again through the VPS using public Codal URLs, without writing to the production database or object store. Each newly fetched balance response passed the draft parser's 11 exact-cell, identity, period, scope, unit, and arithmetic checks. The two listed responses below extend this live-source check beyond the four previously documented pairs. The SHA-256 columns identify either the earlier regression fixture (OTC and registered) or the newly fetched response (listed), not every response Codal may generate for a URL.

| Venue and scope | Original income filing | Separate balance sheet | Income HTML SHA-256 | Balance HTML SHA-256 |
| --- | --- | --- | --- | --- |
| Listed standalone, `خوساز` | [Codal](https://codal.ir/Reports/Decision.aspx?LetterSerial=caoytVYjjlOOObOOOYU9TgeEsFiw%3d%3d&rt=0&let=6&ct=0&ft=-1) | [sheet 0](https://codal.ir/Reports/Decision.aspx?LetterSerial=caoytVYjjlOOObOOOYU9TgeEsFiw%3d%3d&rt=0&let=6&ct=0&ft=-1&sheetId=0) | `03a57fec1790b1b6a4dfd78066e12702af9ef19762f29788942e82351664574a` | `99d7b8bfbf72f2c5006dfac2b9b95a618bf6106af4014bbae579e3d3519bf357` |
| Listed consolidated, `کاوه` | [Codal](https://codal.ir/Reports/Decision.aspx?LetterSerial=oCBQQQaQQQDCgHmc6eMlxC7ig7Xw%3d%3d&rt=0&let=6&ct=0&ft=-1) | [sheet 14](https://codal.ir/Reports/Decision.aspx?LetterSerial=oCBQQQaQQQDCgHmc6eMlxC7ig7Xw%3d%3d&rt=0&let=6&ct=0&ft=-1&sheetId=14) | `0a95fe07aea72c71dfa2685005b0af723c7afefa90705cd01999a581ef0a732c` | `79e577f1cc87af2a49f322d1998071cc5c74cf6bee75363308a1b5d680a57a11` |
| OTC standalone, `بجهرم` | [Codal](https://codal.ir/Reports/Decision.aspx?LetterSerial=mnKmuErJuqPJH4lQQQaQQQvCwnOg%3d%3d&rt=0&let=6&ct=0&ft=-1) | [sheet 0](https://codal.ir/Reports/Decision.aspx?LetterSerial=mnKmuErJuqPJH4lQQQaQQQvCwnOg%3d%3d&rt=0&let=6&ct=0&ft=-1&sheetId=0) | `9b1b4d9b6355fd04c276e2da0bc23cb620b040d5761a8db95a2c415840308141` | `dbe205dd5a50db1e9f73cd774304a8543bc77ace0207957753129da14d44f264` |
| OTC consolidated, `کرومیت` | [Codal](https://codal.ir/Reports/Decision.aspx?LetterSerial=r9mC89jB39KVhOadOOObOOOKQIXg%3d%3d&rt=0&let=6&ct=0&ft=-1) | [sheet 14](https://codal.ir/Reports/Decision.aspx?LetterSerial=r9mC89jB39KVhOadOOObOOOKQIXg%3d%3d&rt=0&let=6&ct=0&ft=-1&sheetId=14) | `0a96748040ea7617100b011a88bd83d043dfaad5191bab9f36d3a08b8526ea28` | `542d1744c93474103f7d4a1b4f5d61da7c54fdd6d14d5f5fdd6bed7a97a6044e` |
| Registered standalone, `لکما` | [Codal](https://codal.ir/Reports/Decision.aspx?LetterSerial=NXh0zzf8btuBRc7NnlYrdg%3d%3d&rt=0&let=6&ct=0&ft=-1) | [sheet 0](https://codal.ir/Reports/Decision.aspx?LetterSerial=NXh0zzf8btuBRc7NnlYrdg%3d%3d&rt=0&let=6&ct=0&ft=-1&sheetId=0) | `7ed861e8345829745fce019f520d8a0728700a686b7819f3c6c507ee4391f350` | `ec32efe80a32e9e7896915df42e47e468d4952b50dd680d4829d94bd284c6197` |
| Registered consolidated, `دحاوی` | [Codal](https://codal.ir/Reports/Decision.aspx?LetterSerial=i8COOObOOOXrPKfcWIiu70yhDpMA%3d%3d&rt=0&let=6&ct=0&ft=-1) | [sheet 14](https://codal.ir/Reports/Decision.aspx?LetterSerial=i8COOObOOOXrPKfcWIiu70yhDpMA%3d%3d&rt=0&let=6&ct=0&ft=-1&sheetId=14) | `4b6c89d9f27330839d45a3be050905c1c2434907aeeefcabf327817ddfbd22fa` | `715c502ec3992098b24def60ee7d22e34a99889851d0bf98991ce74d6ef15cb1` |

Codal generates a different ASP.NET `__VIEWSTATE` between some requests; the registered standalone balance page had stable datasource JSON across two responses but different raw SHA-256 values. Each stored response therefore retains **its own** hash; byte equality across separate HTTP requests is not presumed.

No Codal requests, database writes, or object-store writes were made during the 5,913-object inventory or 1,286-page certification pass. The four balance-sheet fixture pairs were separately fetched from public Codal URLs through the VPS. Production has not run the draft backfill or enabled a Codal worker for this change.
