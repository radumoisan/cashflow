# Cash Flow Agent Instructions

`cashflow.yaml` is the active version-2 source of truth for accounting actuals,
forecast assumptions, tax configuration, and events. `engine.py` is the sole
authority for financial calculations. Do not edit generated `dashboard.html`.

Read [DECISIONS.md](DECISIONS.md) for the forecasting specification and resolved
implementation details. [ACCOUNTING_NOTES.md](ACCOUNTING_NOTES.md) records the
Keez source interpretation and migration evidence.

## Routine Scenario Changes

1. Read `cashflow.yaml` and identify the input: a direct-row override, complete
   accounting month, exact tax payment, gross dividend, or dated starting state.
2. Ask a concise question if the month, amount, source basis, tax scope, or
   intended change is ambiguous.
3. Edit `cashflow.yaml`, retaining source explanations for accounting facts and
   explicit starting assumptions.
4. Run `python engine.py` after every successful routine YAML change.
5. Quote ending and lowest balances exactly as printed by the engine. Never
   hand-calculate or estimate financial balances.

## Forecast Inputs

- Direct forecast inputs are net of VAT for Clients/Suppliers, in signed RON:
  positive adds cash, negative removes cash, and zero explicitly means no movement.
- Store human changes in `rows[].overrides[YYYY-MM]`. Remove a key to restore
  estimation; missing and zero are different states.
- `carry` overrides establish a new run rate; `zero` overrides apply only to
  their month. Never persist generated estimates as actuals or overrides.
- VAT, Taxes, dividend cash, subtotals, and rolled-forward balances are derived
  for forecast months and are not browser-editable inputs.
- Forecast eligibility follows accounting coverage, not the current date.
- Row IDs are stable, unique, and lowercase hyphenated. Preserve existing IDs
  when changing labels. Activities are operating, investing, or financing.
- The view contains exactly 12 months; stored history and overrides can exceed
  the view. Changing `settings.start_date` does not change the dated cash anchor.
- `settings.initial_balance` applies at `settings.history_start`. Reported
  actual balances take precedence with engine-produced reconciliation notes.
- `settings.ron_per_eur` is the positive fixed display conversion; EUR is read-only.

## Accounting Updates and Reference Sources

- Accept complete, contiguous actual months from `settings.history_start`.
  Each requires a source, basis, every row value, and reported closing balance.
  Partial-month submissions remain outside active actuals until complete.
- Use `engine.import_actual_month` to validate a full import/correction before
  publishing it. Closed actuals are locked in the browser; explicit sourced
  accounting corrections are permitted through files/chat.
- New actual months use `basis: net`: Clients/Suppliers exclude VAT, and VAT
  includes operating VAT cash plus tax-authority cash. Existing 2025
  `basis: reported` months preserve the original Keez cash figures.
- The app and scenario snapshot show Clients/Suppliers net in every month.
  The user approved standard VAT and full supplier deductibility (19% through
  July 2025, then 21%). Engine presentation of gross reported months rounds net,
  transfers gross-minus-net residuals into VAT, and marks the three read-only
  cells `actual-net-estimate` with original amounts and rate in their notes.
  These display estimates must not be persisted as accounting facts or used to
  infer opening tax state. Supplied net records display directly.
- Keez cash collections/payments are VAT-inclusive where applicable. Do not
  equate Profit-report revenue/costs with cash timing or silently label gross
  cash as net. `engine.normalize_cash_actual` requires supplied aggregate signed
  VAT components for exact normalization; standard-rate net presentation/seeding is
  explicitly an estimate, not an accounting fact.
- Source PDFs in `keez-exports/` are immutable. The accounting YAML is a reference
  transcription and changes only when reconciled to its corresponding PDF.
- Accounting reset/import requests must inspect and reconcile corresponding
  PDFs before replacing active values. Preserve reported discrepancies and
  explanations rather than inventing balancing movements or tax/dividend splits.

## Tax and Dividend Inputs

- `tax_payments` contains sourced, positive payment magnitudes by due month:
  profit, dividend, other, or combined Taxes total; VAT is separate.
- Combined total replaces its month's Taxes estimate and cannot be combined with
  its components. It may accompany a separate VAT payment. Explicit zero is valid.
- Gross positive dividend events include source and month. Actual dividend cash
  must match a gross event or have explicit next-month tax information. A closed
  month's actual cash supersedes a cancelled/changed forecast event.
- Tax checkpoints replace opening VAT credit, profit-proxy loss, and outstanding
  payment schedules for their dated month, with `kind` and `source` required.
  Initial zero state is an explicit assumption. Paid tax alone does not establish
  remaining losses or liabilities. Losses carry across years until absorbed or
  replaced by a checkpoint.
- Engine calculations round monetary outputs to cents with ROUND_HALF_UP.
  Keep all financial arithmetic in `engine.py`.

## Browser Persistence

Browser PATCH may set or clear only one eligible direct forecast override and
is saved only after server confirmation. The server validates and evaluates the
full YAML, writes atomically, and checks the quoted YAML revision with If-Match.
GET ETags additionally identify the selected window and differ from that revision.
Do not bypass engine editability or validation. Preserve unsaved drafts on errors
and same-cell conflicts.

## Code Changes

- Frontend, backend, engine, tests, templates, and dependencies are code upgrades.
- Run the full suite for every upgrade: `python -m unittest discover -s tests -v`.
- For editing/navigation changes, run the opt-in Chromium integration tests with
  development dependencies: `CASHFLOW_BROWSER_TESTS=1 python -m unittest tests.test_browser -v`.
- Do not change `engine.py` or `template.html` for a routine scenario update.
