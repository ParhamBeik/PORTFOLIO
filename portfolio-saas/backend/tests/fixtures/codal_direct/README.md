Real codal.ir responses captured 2026-10-05 from the production VPS (read-only probes),
trimmed for size. See docs/CODAL-DIRECT-MIGRATION.md §3.

- `v2_q_day_page1_trimmed.json` — `search.codal.ir/api/search/v2/q`, FromDate=ToDate=1404/07/02,
  page 1. `Letters` cut from 20 to 4; `Total`=290 and `Page`=15 are the real values.
- `v2_q_day_past_last_page.json` — same day, PageNumber=99: same Total, empty Letters.
- `v2_q_400_invalid_date.json` — `FromDate=-1&ToDate=-1` is rejected (omit the params instead).
- `search_429_body.txt` — body of the HTTP 429 (text/html, no Retry-After).
- `captcha_challenge.html` — what codal.ir `Decision.aspx`/`Attachment.aspx` return, with
  HTTP 200 text/html, once the IP is challenged. Must never be stored as an artifact.
- `excel_html_workbook_head.html` — first 3 KB of an `excel.codal.ir` "Excel": served as
  application/ms-excel but the body is an HTML workbook.
