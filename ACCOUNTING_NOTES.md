# Keez source interpretation and 2025 migration

Reviewed: 2026-09-13. Both supplied PDFs were inspected against
`accounting/actuals-2025.yaml`; the cash rows and reported balances agree with
the transcription. The PDF files and reference transcription remain the source
evidence.

## What Clients and Suppliers mean

Keez's [Dashboard documentation](https://app.keez.ro/help/client/web_app/contabilitate/dashboard.html#cash-flow)
defines the cash-flow categories as **sums received from clients** and **sums
paid to suppliers**. Its Receipts and Payments section maps bank/cash statement
transactions directly to Clients and Suppliers. The same page distinguishes
these receipts/payments from the Revenue and Expenses reports.

Consequently, these are actual cash amounts, including VAT where a payment is
taxable; they are not generally net revenue or net expenses. The supplied PDF
does not label individual VAT treatments or show each payment's VAT component.

The engine's `compare_cash_to_revenue` diagnostic provides supporting evidence
from the supplied PDFs:

| Month | Profit-report revenue | Revenue plus standard VAT | Cash-flow Clients |
| --- | ---: | ---: | ---: |
| 2025-01 | 105,650.00 | 125,723.50 | 125,723.00 |
| 2025-08 | 84,065.00 | 101,718.65 | 101,719.00 |
| 2025-12 | 73,305.00 | 88,699.05 | 88,699.00 |

These calculated comparisons are engine-produced. Other months differ because
cash and revenue need not share timing; a mismatch is not evidence that the
cash-flow row excludes VAT. Supplier payments cannot be reconstructed exactly
from aggregate Profit-report expenses either.

Sources inspected:

- `keez-exports/cashflow-2025.pdf`, page 1.
- `keez-exports/profit-2025.pdf`, page 1.
- [Keez Dashboard / Cash Flow and Receipts and Payments](https://app.keez.ro/help/client/web_app/contabilitate/dashboard.html).
- [Keez Cash Flow analysis](https://app.keez.ro/help/client/antreprenor/analiza_performantei/analiza_cf.html).

## Active history and the initial forecast

2025 is a closed reference year. No 2026 actuals have yet been supplied; the
current calendar date does not convert estimates into actuals.

- All twelve historical cash rows, their monthly amounts, and reported opening
  and closing balances are preserved in active `basis: reported` months.
- Display labels use the agreed forecast terminology while retaining stable
  source row IDs. Payroll includes the reported salary and payroll-tax cash.
- Historical Shareholders is retained unsplit. The new Dividends Paid row adds
  zero separate cash for those months, with an explanation that this does not
  establish the absence of historical dividends.
- The engine retains source reconciliation explanations: its reference check
  reports 38 rounding differences and 2 material opening-rollforward differences,
  in April and May. Historical balances remain reported facts rather than being
  replaced with invented adjustments.
- The forecast starts from the reported December closing cash balance.

The user explicitly accepted standard-rate VAT and full supplier-VAT
deductibility as modelling assumptions, to be corrected with monthly accounting
data. The date-based rate schedule is 19% through July 2025 and 21% thereafter.
This acceptance does not turn the assumed VAT split into source evidence.

The initial net Clients/Suppliers forecast uses the latest historical cash
amount divided by one plus its standard VAT rate. This **user-approved net
estimate** is recorded as `taxes.reported_cash_seed_basis: gross-standard` and
explained in cell provenance. Supplied net actuals or manual net overrides
supersede the corresponding forecast run rates.

## Net presentation for all displayed months

The app and scenario snapshot label the rows **Clients (net)** and
**Suppliers (net)**. For closed `basis: reported` months under the gross-standard
assumption, the engine presents both amounts net of their month's standard VAT.
The original gross cash remains in the source record and in the cell explanation.
The converted cells are read-only with `actual-net-estimate` provenance.

For each of the two rows, net is rounded to cents with ROUND_HALF_UP. Its signed
VAT portion is **original gross minus rounded net**. The displayed VAT row adds
both portions to the original reported VAT cash, preserving the source cash
total to the cent. This VAT presentation is also labelled estimated; it does not
infer historical tax liabilities, credits, or profit losses. Reported balances
and existing reconciliation explanations continue to anchor the cash timeline.

Records supplied with `basis: net`, and amounts explicitly configured as already
net, display directly. Forecast net inputs also display directly. The conversion
is calculated in `engine.py`, shared by the API and snapshot, and never stored as
an accounting correction. Exact accounting corrections supersede the estimated
presentation for their month and establish the subsequent forecast run rate.

## Future accounting imports

New active actual months use net Clients/Suppliers and total VAT cash movement.
For exact transformation of a gross cash report, obtain the aggregate signed
VAT collected with Clients and VAT paid with Suppliers for the period. The
engine's `normalize_cash_actual` reclassifies these amounts into VAT while
preserving the source cash total and reported balances. No invoice-level model
is required, but the aggregate PDFs alone do not supply this exact split.

Profit tax, dividend withholding, VAT credits, remaining proxy losses, and
unpaid obligations cannot be inferred reliably from combined paid-tax rows.
The user chose explicit zero opening tax-state assumptions at January 2026;
accounting checkpoints and scoped exact payments replace them when supplied.
