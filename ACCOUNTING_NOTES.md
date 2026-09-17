# Keez source interpretation and accounting imports

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

2025 is a closed reference year. January–August 2026 was imported on 2026-09-16
after reconciliation to the four 2026 PDFs (see the import record below).
September remains partial; the current calendar date does not convert estimates
into actuals. The original 2025 migration is preserved as follows:

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
- The initial 2026 forecast started from the reported December closing cash
  balance; new complete accounting months supersede those estimates.

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

## 2026 to-date import — 2026-09-16

The user requested publication of the supplied 2026 accounting records. Every
Cash Flow and Profit summary row, monthly column, and total was compared with
`accounting/reference-2026-todate.yaml` and its corresponding PDF. All 180 VAT
reference entries were checked against the reference's category assignments and
signed totals. The 450 detailed cash movements were parsed from all 13 pages,
with company identity, dates, source page, extracted line, and PDF hash retained.

### Coverage and accounting basis

- Active company actuals now extend through **2026-08**: eight new complete
  months, validated individually with `engine.import_actual_month`.
- The Cash Flow and Profit summaries include a partial September column. The
  Movements and VAT exports end on 31 August. September is kept outside active
  actuals, consistent with the reference's approved closed-month cutoff.
- Each new month uses `basis: net`, retaining the Cash Flow summary's whole-RON
  category cash and reported opening/closing balances as authoritative totals.
- `engine.normalize_cash_actual` uses the basis already approved and recorded in
  the reference: collected VAT on client receipts and **monthly recognized
  deductible operating-supplier VAT**. The latter is a recognition-basis bridge,
  not newly supplied exact cash-matched invoice VAT. Nonrecoverable VAT remains
  in costs, and signed corrections and recognition/payment timing differences
  remain documented in every month's note.
- Advances, Fixed Assets, and reverse-charge VAT are excluded from this operating
  VAT reclassification. Profit-report revenue/costs are reference evidence rather
  than replacement cash amounts. Engine checks confirm normalization preserves
  every month's total signed cash.
- Payroll retains the reported Net Salaries and Taxes cash, including its payroll
  obligations. Taxes retains the source total. January's tax cash comprises the
  suspense/social-insurance settlements in movement references 9010 and 9061;
  it is not treated as a supplied profit/dividend split. April, June, and July
  movement descriptions identify profit-tax payments.
- The sources identify shareholder funding/loan repayments and no separate
  dividend payout or tax-authority VAT remittance for these months. The monthly
  source mapping records zero separately identified dividend and VAT-remittance
  cash, with this interpretation stated explicitly in the accounting notes.

### Source reconciliation

The detailed export retains cents, whereas the Cash Flow summary rounds category
amounts to whole RON. Engine-produced differences are retained rather than
inserted as balancing transactions:

| Month | Movement cash minus summary-row cash (RON) |
| --- | ---: |
| 2026-01 | -0.02 |
| 2026-02 | -0.47 |
| 2026-03 | +0.53 |
| 2026-04 | 0.00 |
| 2026-05 | -0.51 |
| 2026-06 | -0.68 |
| 2026-07 | -0.61 |
| 2026-08 | -0.33 |

All eight months pass the engine's movement-cash reconciliation tolerance.
Reported closing cash differs from the displayed-row roll-forward by -1.00 RON
in February, May, and July and +1.00 RON in March. The engine annotates those
differences and retains the reported closing balances.

Detailed source classifications are also preserved: the parser leaves certain
payroll obligations, tax settlements, internal transfers, and miscellaneous
descriptions unmapped. Their original signed cash is imported and reconciled.
May includes offsetting internal transfers and a -0.05 RON penalty. In June,
reference 11492 labels -30.00 RON as other deductible operating expense, while
the summary reports Miscellaneous zero and its Interest and Bank Charges total
differs from the parser's bank/interest classification by +29.87 RON (detail
minus summary). The company summary and original movement description are both
retained; this does not create another expense or an invented balancing entry.

### Tax schedule and Regio review

- The reference records user confirmation that **RON 20,328.69 VAT is payable in
  September**, supported by the VAT PDF's page-9 balance. It is now stored as the
  sourced positive `tax_payments.2026-09.vat` payment, replacing September's VAT
  remittance estimate. It is not added to Taxes or treated as an actual payment
  in the partial September month.
- The existing explicit January opening tax-state assumptions remain the dated
  starting state. The imported paid-tax rows and VAT report have not been used to
  invent remaining profit losses or a replacement full tax checkpoint. Forecast
  tax carry state remains the engine's documented proxy where not supplied.
- All movements were merged with `engine.import_expense_movements`; duplicate
  imports were checked to be idempotent. **2026 Regio ownership review is pending
  the user's expense identification.** No zero-allocation assumptions were added
  for these months and no review digests were fabricated.
- Pending review, Regio actual cells show unknown allocations (`—`). Regular
  Suppliers and Payroll forecasts retain their last reviewed December-2025 run
  rates. Clients and Interest and Bank Charges use the latest August actuals.
  Completing project identification/review will let the reviewed regular expense
  residuals establish their new run rates.

Validation also checked locked January–August cells, preservation of all 2025
source actuals, unchanged existing overrides, and the confirmed September VAT
payment. `python engine.py` regenerated the dashboard after publication.

Source SHA-256 hashes at import:

| PDF under `keez-exports/` | SHA-256 |
| --- | --- |
| `cashflow-2026-todate.pdf` | `f3cb7e7257a2794376e8cff7cf8ecf65e111f0db8082e804ca0831d3541bcca4` |
| `profit-2026-todate.pdf` | `5aee5348515ab990d4afb91cf1e88a361fdab7a7eebbce72551450a2cbc166e3` |
| `movements-2026-todate.pdf` | `ec03667083b66f62d4a516f34d5cfafee728e49d88eba949b5d8b80cfcb320de` |
| `vat-2026-todate.pdf` | `2d8a6933f0e6e7fcc2d2bfff95714749d4b22e4b0b510ab34ac6a4c97131d9f4` |
