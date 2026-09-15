# Declarative Cash Flow

A local accounting-plus-forecast table. `cashflow.yaml` is the active source of
truth; `engine.py` performs all financial calculations. The reviewed model and
implementation decisions are in [DECISIONS.md](DECISIONS.md).

The supplied **2025 year is closed accounting history**. No 2026 actuals have
been provisioned: all of 2026 is initially a forecast, even months before today.
The starting estimates and accounting interpretation are explained in
[ACCOUNTING_NOTES.md](ACCOUNTING_NOTES.md).

## Setup and Run

Requires Python 3.10 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python server.py
```

Open <http://127.0.0.1:8000>. The server binds to loopback by default and is
unauthenticated; use authentication and TLS before exposing it to an untrusted
network.

## Working with the Table

- The table shows **Inflows** (Clients), **Expenses** (Suppliers, Payroll, Taxes,
  Fixed Assets, Advances, and Miscellaneous), a standalone **VAT** row, and
  **Financing** with its existing rows and subtotal. Each collapsible section
  keeps its total visible; VAT is always visible. Positive receipts within
  Expenses offset spending in its signed total.
- **Regio** is nested under Expenses, with its own collapse control and total.
  Root expense rows represent regular activity; project amounts appear only in
  Regio. Forecast category cells default to zero and apply only to their month.
  For actuals, update the exports in `keez-exports/` and tell the assistant to
  import them, identifying the expenses belonging to Regio. See [REGIO.md](REGIO.md).
- The default window is January–December 2026. The compact period control beside
  RON/EUR moves by a month; click the range to select another starting month.
  The selected window is retained in the URL, for example `?start=2026-03`.
  Column headers use abbreviated English months and two-digit years: `Jan 26`,
  `Feb 26`, `Mar 26`.
- Only direct assumptions in unclosed months are editable, in RON. **Clients
  and Suppliers inputs exclude VAT.** Positive values add cash; negative values
  remove cash. EUR is display-only at `settings.ron_per_eur`.
- **Clients (net) and Suppliers (net) display net values in every month.** Closed
  gross cash reports use an estimated standard-rate VAT split, assuming full
  supplier-VAT deductibility. The extracted VAT is included in the VAT row so
  the cash total still reconciles. Their read-only cell explanations retain the
  original cash amounts and applied rate.
- Actuals, VAT, Taxes, dividend payments, subtotals, and balances are read-only.
- Enter saves and advances; Tab/blur saves; Escape discards unsaved typing.
  A save already sent to the server can finish after Escape.
- Empty a cell and save to **remove its override** and restore estimation.
  Entering `0` instead records an explicit zero.
- Editable estimates have muted text; overrides have bold accent text. Hover/focus a cell
  to inspect its provenance and calculation or source explanation.
- Recurring overrides establish a new run rate from their month onward.
  Planned one-off movements affect only their specified month.
- Saves are serialized and confirmed by the server. Invalid or failed edits
  retain their draft. Currency/period changes wait for pending valid saves.
- The browser polls every two seconds when idle. An unrelated revision conflict
  is retried once; a conflicting change to the same cell requires review of
  the retained draft.

Changing the visible window does not reset cash, VAT credits, losses, or tax
payments. The same month has the same results in overlapping windows. Windows
must fit within 100 years from `settings.history_start` and end by 9999-12.

## Active Schema (Version 3)

| Field | Contents |
| --- | --- |
| `schema_version` | `3` (version-2 scenarios remain supported) |
| `settings` | Company, RON currency, display conversion, default start, 12-month window, dated history start and initial cash |
| `rows` | Ordered row definitions, activity, forecast method, profit weight, optional explicit net seed, sparse overrides |
| `actuals` | Contiguous complete accounting months with source, amount basis, all row values, and reported closing balance |
| `taxes` | Effective-month VAT rates, profit/dividend rates, historical seed interpretation, dated tax-state checkpoints |
| `tax_payments` | Exact payment amounts by due month, with source and component/total scope |
| `dividends` | Gross dividend events by payment month, with source |
| `expense_projects` | Named expense projects with category-to-company-row mappings and sparse monthly overrides |
| `movements` | Immutable accounting movement facts plus editable whole-movement project ownership and VAT-basis metadata |
| `allocation_reviews` | Sourced zero assumptions or completed monthly allocation reviews, fingerprinted against their source |

The stored timeline can exceed twelve months. Generated estimates and derived
values are not written back into the input file.

A recurring row has this shape:

```yaml
- id: clients
  name: Clients (net)
  activity: operating
  forecast: carry
  profit_weight: 1
  seed: null
  overrides: {"2026-10": 80000.00}
```

`carry` uses the latest usable actual or override. Project-mapped expense rows
use reviewed actuals less project allocations; while review is incomplete, the
previous regular run rate is retained. If there is no history, supply an
explicit net seed as `{value: 80000.00, source: "Business starting assumption"}`.
`zero` rows default to no planned event. The reserved derived methods are `vat`,
`taxes`, and `dividends`, attached to their corresponding stable row IDs.

## Supplying Accounting Months

Provide completed-month exports or amounts through files/chat. In the YAML,
each `actuals[YYYY-MM]` record requires:

- `source`: the accounting reference.
- `basis: net` for new months: Clients/Suppliers exclude VAT, and the VAT cash
  row includes VAT collected/paid with operations minus remittances.
- `values`: **every configured row ID**, including explicit zeros where known.
  These are company `rows[]` IDs; project rows are allocated from them, never
  separately supplied as additional accounting cash.
- `closing_balance`: the reported cash balance.
- Optional `opening_balance` and `note` for source reconciliation.

Only complete, contiguous months beginning at `history_start` are accepted.
Partial information is not published as actuals. `engine.import_actual_month`
validates a complete update before returning a new document. Corrections to an
existing month use the same path. Matching overrides become inactive once that
month is actual; later overrides retain their dates.

Keez Cash Flow contains actual cash receipts/payments, not net revenue/cost
figures. For exact net normalization, supply aggregate signed Clients VAT and
Suppliers VAT. `engine.normalize_cash_actual` subtracts those components from
the gross cash rows and transfers them into VAT, preserving the original cash
total and reported balances. It records the transformation in the source note.
The Profit PDF is not a substitute for collections/payment timing.

The 2025 records deliberately retain `basis: reported` and their original cash
figures. Their source discrepancies appear in balance-cell explanations. The
user-approved standard-rate conversion supplies both the historical net
presentation and the initial net forecast run rate. Converted historical Clients,
Suppliers, and VAT cells carry `actual-net-estimate` provenance and remain locked.
For each cash row the VAT transferred is gross minus rounded net, preserving
source cents. Supplied net actuals display directly and supersede these estimates.

`keez-exports/` is immutable evidence. `accounting/actuals-2025.yaml` remains its
reference transcription. A reset/import from those sources must reconcile the
corresponding PDFs before replacing active facts.

## VAT, Taxes, and Dividends

VAT uses effective-month rates: initially 19% through July 2025, then 21%.
Standard-rate VAT and full supplier deductibility are accepted modelling
assumptions, subject to monthly accounting corrections.
The engine rounds the Clients and Suppliers VAT components separately, tracks
credits, and schedules a positive liability for the following month. The VAT
cash row includes current operating VAT cash and the remittance paid that month.

Profit tax is an explicitly approximate cash-flow model: 16% of positive
configured profit-proxy contributions after carried losses, paid the following
month. Losses carry across calendar years. Payroll includes payroll obligations;
capex, debt principal, advances, VAT, taxes, and shareholder/intercompany cash
are excluded from the configured proxy. This forecast schedule is distinct from
the accountant's statutory calculation and payment schedule.

Supply exact tax **payment magnitudes** as positive amounts (or explicit zero):

```yaml
tax_payments:
  "2026-10":
    source: "Accountant-confirmed October payment"
    profit: 1200.00
```

Supported components are `profit`, `dividend`, `other`, and `vat`. Alternatively,
`total` replaces the entire Taxes row for that month, excluding VAT. A `total`
cannot be combined with profit/dividend/other components; it may accompany `vat`.
Supplied payments replace estimates rather than adding another cash movement.
Actual monthly cash rows take precedence over earlier payment estimates.

Gross dividends are separate from other shareholder movements:

```yaml
dividends:
  "2026-10": {gross: 10000.00, source: "Planned gross dividend"}
```

The event generates a shareholder outflow after 16% withholding in that month
and a withholding payment in the next month. An imported actual dividend payout
requires a matching gross event or explicit next-month dividend tax/total or
tax-state checkpoint. An actual zero payout supersedes a cancelled forecast
event and its forecast withholding.

`taxes.checkpoints[YYYY-MM]` replaces opening VAT credit, profit-proxy loss, and
outstanding payments as of that month. Its `kind` is `assumption` or `accounting`,
and `source` explains the values. Payments are positive magnitudes keyed by due
month and component. The initial January 2026 checkpoint explicitly assumes
zero carry balances and no prior tax due. Add accounting checkpoints when the
actual state is supplied; a tax payment alone does not identify a loss balance.

All monetary inputs have at most two decimal places. Decimal calculations use
ROUND_HALF_UP for VAT components, monthly profit tax, and dividend withholding.
Net dividend cash is gross less rounded withholding, so the parts reconcile.
Cash sums retain cents; EUR is rounded only for display.

## API

- `GET /api/health`: service status and API version `3`.
- `GET /api/state?start=YYYY-MM`: evaluated twelve-month report, cell provenance
  and editability, tax details, navigation bounds, and YAML `revision`.
  `report.months` retains `YYYY-MM` keys; `report.month_labels` supplies the
  corresponding column labels such as `Jan 26` for both live and snapshot views.
  `report.activity_groups` contains the ordered display sections: `inflows`,
  `expenses`, `vat`, and `financing`. A `null` subtotal identifies a standalone
  section (VAT) rendered without a collapsible heading or duplicate total.
  Groups have a `children` array; Expenses contains Regio there. Group totals
  already include descendants. Sum leaf rows once, not both leaves and totals.
- `PATCH /api/cell?start=YYYY-MM`: set or clear one direct forecast override in
  that window. Body: `{row_id, month, value, currency}`. `value` is a decimal
  string or JSON `null` to clear; `currency` must be `RON`.
  Project row IDs, such as `project-regio-suppliers`, use this same endpoint.

Accounting imports and Regio assignments use the backend engine through files/chat.
The HTTP interface provides forecast editing and read-only actuals display.

GET ETags identify the source **and selected window**. A matching
`If-None-Match` returns `304`. PATCH `If-Match` is the quoted YAML `revision`
from the response body, **not the GET representation ETag**. Stale writes
receive `409`; invalid configuration/targets receive `422`.

Each write validates and evaluates the full document, preserving Decimal
precision, comments, quotes, and file permissions. Atomic replacement prevents
partial files. The final pre-write revision check detects stale data; an
uncoordinated external writer can still race in the narrow check-to-replace
interval.

## Offline Snapshot and Verification

```bash
python engine.py
xdg-open dashboard.html
python -m unittest discover -s tests -v
node --check frontend/assets/app.js
```

`dashboard.html` is a generated, read-only snapshot of the default window. The
interactive period navigator and persistence are provided by `server.py`.

For opt-in real Chromium integration checks:

```bash
python -m pip install -r requirements-dev.txt
CASHFLOW_BROWSER_TESTS=1 python -m unittest tests.test_browser -v
```

These tests run against temporary scenario copies and temporary local servers.
They require Chromium (default `/usr/bin/chromium`, configurable through
`CHROMIUM_BINARY`) and Selenium's Chrome driver support.
