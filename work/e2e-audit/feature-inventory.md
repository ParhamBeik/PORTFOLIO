# Feature and Route Inventory

Source discovery and live browser coverage as of 2026-09-12.

| Surface | Source route/API | Live result | Status |
|---|---|---|---|
| Anonymous sign-in | `/`, `/login`, `/api/auth/login/` | Form rendered; invalid synthetic credentials rejected clearly | PASS (negative path) |
| Anonymous signup | `/signup`, `/api/auth/register/` | Direct route opens sign-in mode; validated synthetic signup succeeded; duplicate existing-email submission was rejected clearly | FINDING F-002; PASS (create/duplicate) |
| Password reset request | Auth forgot-password flow, `/api/auth/password-reset/` | Form rendered; real email send not attempted | BLOCKED |
| Password reset confirm | `/reset-password`, `/api/auth/password-reset/confirm/` | Token-dependent form rendered with disabled submit | BLOCKED without token |
| Public privacy | `/privacy` | Rendered anonymous and signed-in | PASS |
| Public terms | `/terms` | Rendered anonymous and signed-in | PASS |
| Holdings dashboard | `/`, valuation/insights/analytics APIs | Synthetic portfolio/holding create, read-after-refresh, edit, zero boundary, explicit holding delete, and reversal interaction exercised | PASS with F-004/F-005 |
| Ledger | `/ledger`, account ledger/trade/import APIs | Synthetic add/edit/reverse/delete exercised; cash-deposit validation failure captured | PASS with integrity finding F-005 |
| Liability CRUD | Portfolio liability APIs and dashboard dialog | Synthetic liability create, read, edit, total recalculation, and delete exercised | PASS |
| Breakdown | `/family` and `/breakdown` alias | Rendered; alias redirected | PASS (read-only) |
| Comparison | `/comparison` | Rendered mode/range controls | PASS (read-only) |
| Price history | `/prices`, `/api/prices/history/` | Rendered asset selector and explicit no-history state | PASS (empty state) |
| My Optimal | `/optimal` | Rendered route | PASS (read-only) |
| Best Overall | `/universe` and `/best-overall` alias | Rendered route; alias redirected | PASS (read-only) |
| Onboarding | `/onboarding` | Correctly redirected to dashboard for populated account | PASS (member boundary) |
| Operations | `/ops`, `/api/admin/*` | Owner/staff session inspected read-only across all operator panels; synthetic member redirected to `/` | PASS (read-only admin); PASS (member denial) |
| Account menu | Profile, password, export, logout, delete controls | Synthetic profile name edited and persisted; synthetic account deletion completed and login rejected afterward | PASS except password/reset delivery not write-tested |
| Backend health | `/api/health/`, `/ready/`, `/prices/` | All public probes returned 200 at baseline | PASS |

## Explicitly limited or blocked subflows

| Subflow | Result | Status / reason |
|---|---|---|
| CSV import preview/commit | The live ledger exposes `Import CSV`, but no file was uploaded during this run | NOT TESTED; would require a second synthetic account/fixture after the deleted test account, and must remain synthetic |
| Account export/download | The admin and synthetic menus exposed download controls; no export was triggered | DEFERRED; exporting admin data would transmit sensitive data to local storage, and the synthetic account was deleted after CRUD coverage |
| Password change | Control was inspected but not submitted | BLOCKED by the browser safety handoff for changing a password; no credential was requested or stored |
| Password-reset delivery/confirmation | Request and token-dependent form were inspected; no real mail/token used | BLOCKED by SMTP configuration and no synthetic verification token |
| Optimizer/analytics detail branches | Primary pages and visible empty/loading/read-only states rendered; no order/payment or external action exists | PASS (read-only), with browser timing limits documented in the evidence ledger |

The source exposes the subflows above in addition to the CRUD paths exercised on the
isolated account. They are explicitly classified here instead of being inferred as
passed from source inspection.
