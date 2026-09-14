# Cash-flow forecasting decisions

Date: 2026-09-13

Status: **Implemented and verified (schema version 2)**

This document records the reviewed product and financial-model decisions agreed
with the user. It is the specification for the version-2 forecasting model.
Section 11 records the implementation-phase resolutions. Operational usage is
documented in `README.md` and `AGENTS.md`; source interpretation is documented in
[ACCOUNTING_NOTES.md](ACCOUNTING_NOTES.md).

## 1. Purpose and human workflow

The tool combines supplied accounting facts with an editable cash-flow forecast.
An input is independent information supplied by a human: a known accounting
amount, a business assumption, a configuration value, or a planned event.
Being stored in YAML does not, by itself, make a value a browser-editable input.

1. The user supplies accounting exports or values through files/chat, initially
   for a few months and subsequently one month at a time.
2. Imported completed months become actuals, locked in the browser. Corrections
   arrive through replacement accounting information or an explicit assistant
   update with source provenance.
3. The engine estimates the remaining months from available information.
4. The user edits direct business assumptions in unclosed periods in the table.
5. Each confirmed edit updates dependent estimates, taxes, and cash balances.
6. New accounting information supersedes the corresponding forecasts.

Forecast eligibility follows accounting coverage, not today's calendar date.
An elapsed month without supplied actuals may still be a forecast month.

## 2. Cell meaning and editability

Financial role and provenance are separate concepts. A VAT row is derived in a
forecast, but its completed-month value can be an imported accounting fact.

| Displayed value | Browser-editable? | Meaning |
| --- | --- | --- |
| Actual | No | Supplied accounting fact |
| Direct-row estimate | Yes | Engine-generated business assumption |
| Direct-row override | Yes | Human replacement for an estimate |
| Derived forecast | No | Engine result based on assumptions and rules |
| Accountant-confirmed future tax amount | No | Exact supplied amount replacing the corresponding tax estimate |

- VAT, Taxes, dividend payments, subtotals, and rolled-forward balances are
  read-only in the browser. Read-only does not mean historical facts are
  recalculated using forecast formulas.
- A missing value means not supplied; `0` means explicitly no movement.
- Clearing an override removes it and restores automatic estimation. It does
  not save a zero or turn an estimate into an actual.
- A forecast override for a month becomes inactive when replaced by actuals.
  Later-month overrides remain effective for their own dates.
- Editability must be defined by the financial model and enforced by the API,
  as well as reflected by the browser.

## 3. Row classifications

All cash-flow amounts are signed: positive adds cash and negative removes cash.
Clients and Suppliers forecast assumptions are net of VAT. Here, net Clients
means collections excluding VAT, not profit after expenses.

| Row | Default forecast behavior | Direct forecast input? | Profit-proxy contributor? |
| --- | --- | --- | --- |
| Clients | Carry latest value forward | Yes | Yes |
| Suppliers | Carry latest value forward | Yes | Yes |
| Payroll | Carry latest value forward | Yes | Yes |
| Interest and Bank Charges | Carry latest value forward | Yes | Configured deductible portion |
| Advances | Zero unless planned | Yes | No |
| Miscellaneous | Zero unless planned | Yes | Configured operating amounts |
| Fixed Assets | Zero unless planned | Yes | No |
| Short-Term Debt | Zero unless planned | Yes | No |
| Other Shareholder Movements | Zero unless planned | Yes | No |
| Intercompany Settlements | Zero unless planned | Yes | No |
| VAT | Calculate VAT cash movement | No | No |
| Taxes | Calculate tax cash payments, subject to supplied exact amounts | No | No |
| Dividends Paid | Calculate from gross dividend events | No | No |
| Opening balance, subtotals, closing balance | Import/reconcile facts or derive roll-forward results | No | No |

The forecast methods and profit-proxy contributions are explicit row metadata.
An activity grouping such as financing does not itself determine whether a row
contributes to the profit proxy.

"Net Salaries and Taxes" becomes **Payroll**, the manually supplied total
payroll cash cost including its associated payroll obligations. No employee-level
payroll-tax calculation is part of this model. Those obligations must not also
be counted in Taxes.

Other Shareholder Movements represents funding, repayments, and other
non-dividend movements. Dividends are identified separately; historical
Shareholders amounts are not automatically classified as either kind.

## 4. Estimation and override propagation

For a recurring direct row, the latest available actual or explicit override
establishes the run rate. Each following unknown month inherits that value until
a newer actual or override supersedes it.

- A recurring-row override changes the run rate from that month onward.
- An event-row override applies only to its specified month. Following unknown
  months return to zero unless another event is planned.
- An explicit zero participates in these rules like any other supplied amount.
- Clearing an override recomputes the affected forecast from the remaining
  actuals and overrides.
- Generated estimates are calculated on demand, not persisted as facts.
- No seasonality, growth factor, or trailing average is part of the initial
  forecasting rule.

A recurring row without any usable history requires an explicit sourced net
starting assumption; a missing actual must not silently become zero.

## 5. VAT

### Basis and rate schedule

VAT is estimated from aggregate net Clients and Suppliers values, without
individual invoices. One configured standard rate applies to these forecast
amounts; the simplified model treats supplier VAT as deductible. Other rows do
not independently generate VAT under the agreed initial model.

| Effective period | Rate |
| --- | --- |
| Through 2025-07 | 19% |
| From 2025-08 onward | 21% |

Rates are stored by effective month so a window spanning a rate change uses the
appropriate rate for each activity month. Remitting an earlier liability does
not recalculate it at the payment month's rate.

### Cash movement and settlement

With Suppliers represented as negative values:

```text
VAT activity for month = (net Clients + signed net Suppliers) × month's rate
VAT cash row = current month's VAT activity - VAT remittance paid this month
```

The VAT cash row therefore includes VAT collected from clients and paid to
suppliers, as well as settlement with the tax authority. This lets the displayed
Clients and Suppliers remain net while the cash balance includes VAT cash.

- A positive liability, after available VAT credits, is remitted the following
  month.
- VAT credits carry forward to offset later liabilities; the forecast does not
  automatically treat a credit as a cash refund.
- Credits and liabilities are tracked separately from the displayed VAT cash
  row. Current-period credit does not silently rewrite a prior-period liability.
- Imported accounting payments and any normalization of historical net/gross
  presentation must be reconciled before seeding forecast VAT state.

## 6. Profit tax

The configured profit-tax rate is **16%**. Until exact accounting information is
available, the engine uses a cash-flow proxy with configurable contributors:

```text
Profit proxy = net Clients
             + signed net Suppliers
             + signed Payroll
             + configured operating Miscellaneous
             + configured deductible Interest and Bank Charges
```

Advances, VAT, capex, debt principal, shareholder movements, dividends, tax
payments, and intercompany settlements are excluded from this proxy.

- Negative proxy results create losses that offset later positive proxy
  results before new tax accrues.
- The remaining positive proxy is taxed at 16%.
- Estimated profit tax is paid in the following month.
- Later losses do not automatically create a cash refund of earlier tax.
- Losses carry across calendar years until absorbed or replaced by a dated
  checkpoint. Initial carry balances are explicit assumptions (section 11).

This is the agreed forecasting convention, not a statutory Romanian taxable
profit calculation or a claim that statutory payments follow this monthly
schedule. Accountant-provided amounts and their cash-payment schedule take
precedence over the corresponding estimates.

## 7. Dividends and the Taxes row

A rare dividend is supplied as an explicit **gross dividend event**, through
files/chat. It is separate from other shareholder movements.

```text
Shareholder cash payment = 84% of gross dividend, in the event month
Dividend withholding payment = 16% of gross dividend, in the following month
```

Both are outflows in the signed cash-flow report. Gross dividend means the
distribution before dividend withholding, from funds after company profit tax;
it is not a second profit-tax base. The event does not automatically distribute
all available profit or cash.

The gross event is an input, not an additional cash row on top of the shareholder
payment and withholding. Dividends and their tax do not reduce the profit proxy.

The forecast **Taxes** row combines profit-tax payments and dividend-tax
payments, retaining separate internal components. Exact accounting information
may specify a component or a combined total, including other reported taxes:

- Each supplied amount needs a payment month and an explicit scope: profit tax,
  dividend tax, another identified tax, or combined Taxes total.
- A confirmed component replaces its corresponding estimate.
- A confirmed combined total replaces the month's forecast total; it is not
  added to estimated components.
- Historical combined totals are preserved as facts without inventing a split.
- A paid tax amount alone does not establish taxable profit, a remaining loss
  balance, or outstanding liabilities.

## 8. Timeline, balances, and interface

- The default view is January–December, initially **January–December 2026**.
- The view always contains exactly 12 consecutive months.
- A compact period control beside RON/EUR shifts the start by a month or lets
  the user select a start month, supporting windows such as March–February.
- The grouped legacy table remains the main interaction surface, with the
  agreed row-label changes and dividend distinction.
- Actuals and derived values render as read-only text. Direct estimates and
  overrides are editable and subtly distinguishable by provenance.
- RON is the editing currency. EUR is display-only, using the engine's fixed
  configured display conversion.
- Enter saves and advances, Tab/blur saves, and Escape restores an unsaved edit.
  Override clearing restores estimation; its precise interaction is finalized
  during the UI implementation.

The window is a view of one financial timeline. Moving it must not reset cash,
run rates, VAT credits, carried losses, or scheduled tax payments. The same
month must have the same result in overlapping views with identical source data.

The calculation needs a dated starting cash balance and reconciled history.
Later opening balances roll forward; the first visible month's opening balance
does not become a new editable starting balance simply because the view moved.
Liabilities generated outside the visible window still affect their payment
months, and obligations due after the window remain part of the timeline.

## 9. Sources, persistence, and migration

`engine.py` remains the sole authority for financial calculations, including
estimates, taxes, balance roll-forward, and display conversions.

`cashflow.yaml` remains the active source of truth. The target model stores:

- Supplied actuals and their accounting coverage/source information.
- Sparse direct-row overrides, distinct from actuals.
- Row identities, activities, forecast methods, and profit-proxy contributions.
- Effective-month VAT rates and configured profit/dividend tax rates.
- Sparse gross-dividend events.
- Exact supplied tax amounts with component/total scope and payment month.
- Dated starting cash and the carry state needed at the forecast boundary.
- A default display start month and a 12-month view length.

The stored history may extend beyond 12 months. A completion marker such as
`actual_through` must represent completed accounting coverage, not silently
promote missing values to actuals. Exact schema syntax is implementation work.

Browser writes mutate one eligible input, or remove its override, only after
full server validation and engine evaluation. Atomic persistence, ETag conflict
handling, and server-confirmed saved state remain requirements. Period
navigation must not rewrite financial values.

The Keez PDFs remain immutable evidence and the accounting YAML remains their
normalized reference transcription. The agreed migration target is locked 2025
active history supporting the initial 2026 forecast, subject to reconciliation:

- Confirm whether each relevant historical amount is net or gross before
  mapping it to the forecast basis.
- Preserve source amounts and provenance when reclassifying VAT presentation.
- Resolve reported balance/roll-forward differences before choosing the cash
  anchor.
- Do not infer dividend events, tax-component splits, or carry balances solely
  from broad accounting row labels.

The current active matrix is not automatically a reconciled history merely
because its values were originally seeded from reference data. Replacements
must inspect and reconcile the corresponding PDFs under the accounting-reset
rules.

## 10. Consistency conclusions

The reviewed model is consistent with these clarifications:

1. Browser-read-only amounts can be accounting facts or calculated forecasts.
2. Net forecast inputs require a reconciled historical amount basis to avoid
   counting VAT twice.
3. Period navigation changes the view, not financial starting state.
4. Missing data, explicit zero, and removed override are different states.
5. Payroll obligations, profit tax, and dividend withholding have distinct
   ownership and are counted once.
6. Forecast tax conventions yield to scoped, dated accounting information.

These replace the earlier broad statements that every leaf row is an input,
VAT/Taxes are always formula-generated, and the first visible opening balance
is always editable.

## 11. Implementation-phase resolutions

The following choices resolve the previously open details:

1. **Starting state:** preserve the closed 2025 report and start the 2026
   forecast from its reported December closing cash. January 2026 VAT credit,
   carried proxy loss, and prior tax due are explicit zero assumptions chosen
   by the user. Dated checkpoints replace carry balances and future outstanding
   payments when accounting state is supplied. Actual cash payments alone do
   not establish those carry balances.
2. **Incomplete coverage:** the user chose complete months only. Actual records
   are contiguous from the dated history start, require every row and a reported
   closing balance, and are published only when complete. No 2026 actuals have
   been provisioned. Recurring rows without history require an explicit seed.
3. **Year-end losses:** the user chose carry-forward across years until absorbed
   or replaced by explicit state. Neither January nor changing the view resets
   losses automatically.
4. **Source interpretation:** Keez documentation identifies Clients/Suppliers as
   actual receipts/payments, including VAT where applicable. The two supplied
   PDFs support the gross-cash interpretation but cannot establish exact
   deductible supplier VAT. Historical figures remain `basis: reported`, with
   reconciliation differences annotated. Initial net forecasting uses a
   provisional standard-rate conversion estimate, clearly distinguished from
   accounting net values. New actuals use net basis; exact gross-to-net
   normalization requires supplied aggregate VAT components. Historical
   Shareholders stays unsplit. See `ACCOUNTING_NOTES.md` for the evidence.
5. **Rounding:** the user chose Decimal arithmetic and two RON decimals with
   ROUND_HALF_UP. Round Clients and Suppliers VAT separately, monthly profit-tax
   accrual, and dividend withholding. Compute net dividend cash as gross minus
   rounded withholding; sum rounded cash movements. Round EUR only for display.

Implementation details consistent with those choices:

- Schema version 2 stores complete actual months, sparse row overrides, dated
  dividend events, exact scoped payments, and tax checkpoints; estimates remain
  engine-generated. The accounting cutoff is derived from contiguous actuals.
- The table keeps the grouped layout and uses dated column headers plus the
  compact month-window control. Navigation is retained in the URL; supported
  windows fit within 100 years from history start and end by 9999-12.
- Clearing the input and saving sends JSON null to remove an override.
- Historical source balances take precedence, with visible-on-hover
  reconciliation explanations; forecast balances roll forward from them.
- An actual dividend payout needs a matching gross event or explicit next-month
  tax information. Actual zero payouts deactivate earlier planned payouts and
  their derived withholding rather than leaving a duplicate future tax.
- Browser writes use the YAML revision; GET representation ETags also identify
  the selected window. Same-cell conflicts retain drafts for review.

## 12. Implementation acceptance criteria

- Actuals and derived rows reject direct browser/API edits; eligible direct
  forecast cells accept overrides and support clearing them.
- Recurring overrides propagate, event overrides remain month-specific, and
  explicit zero remains distinct from missing information.
- Accounting updates supersede the matching forecast without leaving active
  duplicate cash movements.
- VAT handles effective-rate changes, credit carry-forward, next-month
  settlement, and the net-input cash representation.
- Profit tax honors configured contributors, losses, next-month timing, and
  exact scoped accounting replacements.
- Dividend events produce the two scheduled outflows exactly once and remain
  separate from shareholder funding.
- Overlapping 12-month windows agree on the same months, including carried
  balances and obligations originating outside the view.
- The interface preserves the grouped-table workflow, currency behavior,
  keyboard editing, and confirmed-save semantics while exposing provenance.
- Migration is reconciled to the reference evidence and retains provenance.
- Code upgrades run the full test suite required by `AGENTS.md`, with meaningful
  coverage of these calculation, persistence, and interaction rules.

## 13. Verification

The implementation was verified on 2026-09-13 with 56 standard tests and seven
opt-in real Chromium integration tests, plus the engine snapshot export,
JavaScript syntax check, and diff whitespace checks. Browser writes used
temporary scenario copies. Historical migration checks compare every source
cash row and reported monthly balance against the reconciled reference.

## 14. All-month net presentation

The user subsequently accepted standard-rate VAT and full supplier-VAT
deductibility as modelling assumptions, to be corrected with monthly accounting
information, and requested net Clients/Suppliers throughout the app, explicitly
including the historical 2025 months.

- Display labels are **Clients (net)** and **Suppliers (net)**, with stable IDs.
- The existing date-based VAT schedule applies: 19% through July 2025, then 21%.
- For gross reported months, divide each cash row by one plus the rate and round
  net to cents. Add each signed gross-minus-rounded-net residual to the original
  VAT cash row. This preserves source cash totals exactly, including refunds and
  cent-rounding edge cases.
- Original actual amounts remain sourced records. The three converted display
  cells are locked, marked `actual-net-estimate`, and explain their original
  cash amounts, rate, and assumed VAT split. This presentation does not establish
  historical tax carry state; reported balances and reconciliation notes anchor
  the timeline.
- Net actuals, net forecast inputs, and reported amounts explicitly assumed net
  display directly. New accounting information supersedes the estimated split
  and establishes the next forecast run rate.
- Calculations live in `engine.py`; the API and scenario snapshot consume the
  same net-presented rows. Reference PDFs/transcription retain source evidence.

Verified on 2026-09-14: 61 standard tests and eight Chromium integration tests
passed, and the engine regenerated the scenario snapshot. Regression coverage
includes all twelve source-month cash totals, historical rate changes, signed
cent residuals, accountant-supplied net corrections, mixed windows, and RON/EUR
presentation.
