from decimal import Decimal


def base_config(start="2026-01"):
    definitions = [
        ("clients", "Clients", "operating", "carry", 1, 1000),
        ("suppliers", "Suppliers", "operating", "carry", 1, -600),
        ("payroll", "Payroll", "operating", "carry", 1, -100),
        ("misc", "Miscellaneous", "operating", "zero", 1, None),
        ("fixed-assets", "Fixed Assets", "investing", "zero", 0, None),
        ("short-term-debt", "Debt", "financing", "zero", 0, None),
        ("shareholders", "Other Shareholder Movements", "financing", "zero", 0, None),
        ("vat", "VAT", "operating", "vat", 0, None),
        ("taxes", "Taxes", "operating", "taxes", 0, None),
        ("dividends-paid", "Dividends Paid", "financing", "dividends", 0, None),
    ]
    return {
        "schema_version": 2,
        "settings": {
            "company_name": "Example", "registration_number": "123", "currency": "RON",
            "ron_per_eur": Decimal("5.25"), "history_start": start, "start_date": start,
            "projection_months": 12, "initial_balance": 100,
        },
        "rows": [dict(id=id, name=name, activity=activity, forecast=method,
                      profit_weight=weight, seed=None if seed is None else {"value": seed, "source": "Explicit net assumption"},
                      overrides={}) for id, name, activity, method, weight, seed in definitions],
        "actuals": {},
        "taxes": {
            "vat_rates": {"2025-01": Decimal("0.19"), "2025-08": Decimal("0.21")},
            "profit_rate": Decimal("0.16"), "dividend_rate": Decimal("0.16"),
            "reported_cash_seed_basis": "gross-standard",
            "checkpoints": {start: {"source": "Explicit zero starting assumptions", "kind": "assumption",
                                     "vat_credit": 0, "profit_loss": 0, "payments": {}}},
        },
        "tax_payments": {}, "dividends": {},
    }


def row(raw, id):
    return next(item for item in raw["rows"] if item["id"] == id)


def zero_config(start="2026-01"):
    raw = base_config(start)
    for item in raw["rows"]:
        if item["seed"]:
            item["seed"]["value"] = 0
    return raw


def actual_record(raw, **values):
    amounts = {item["id"]: 0 for item in raw["rows"]}
    amounts.update(values)
    return {"source": "Accountant monthly cash report", "basis": "net",
            "closing_balance": 500, "values": amounts}
