"""Read Keez movement PDFs into source records; no cash calculations or persistence here."""
from __future__ import annotations

import hashlib
import re
import subprocess
from collections import Counter
from pathlib import Path

from engine import ConfigError, parse_source_amount


def parse_movements_pdf(data: bytes, source: str, row_ids: set[str], registration_number: str | None = None) -> dict:
    if not data.startswith(b"%PDF-"):
        raise ConfigError("Select a Keez Receipts and Payments PDF")
    try:
        result = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data,
                                capture_output=True, timeout=30, check=True)
    except FileNotFoundError as exc:
        raise ConfigError("pdftotext is required to import PDFs (install Poppler)") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("Could not extract the movement PDF") from exc
    text = result.stdout.decode("utf-8")
    if registration_number is not None:
        heading = next((line.strip() for line in text.splitlines() if line.strip()), "")
        match = re.search(r"(?:RO)?([0-9]+)\s*$", heading)
        if not match or match[1] != registration_number.removeprefix("RO"):
            raise ConfigError("Movement PDF company registration does not match this cash-flow scenario")
    return parse_movements_text(text, source, row_ids,
                                hashlib.sha256(data).hexdigest())


def parse_movements_text(text: str, source: str, row_ids: set[str], digest: str = "") -> dict:
    records = {}
    occurrences = Counter()
    header = None
    page = 1
    for line_number, line in enumerate(text.splitlines(), 1):
        if "Perioada" in line and "Referinta" in line and "Detalii" in line:
            header = line
            continue
        page_match = re.search(r"Pagina\s+(\d+)\s*/", line)
        if page_match:
            page = int(page_match[1]) + 1
        match = re.match(r"\s*(\d{6})\s+(\d+)\s+(\d{2})\.(\d{2})\.(\d{4})\s+", line)
        if not match:
            if re.match(r"\s*\d{6}\s", line):
                raise ConfigError(f"Unrecognized movement line at {source}:{line_number}")
            continue
        if not header:
            raise ConfigError("Movement PDF header not recognized")
        amount = re.search(r"\b(?:Intrare|Iesire)\s+([+-]?[\d.]+,\d{2})(?:\s|$)", line)
        if not amount:
            raise ConfigError(f"Missing cash amount at {source}:{line_number}")
        period, reference, day, month, year = match.groups()
        if period != year + month:
            raise ConfigError("Movement date and accounting period disagree")
        details = line[header.index("Detalii"):].strip()
        partner_start = header.index("Partner", header.index("Cod Partner") + len("Cod Partner"))
        partner = line[partner_start:header.index("Detalii")].strip()
        if not details:
            raise ConfigError("Missing movement description")
        # Source reference identifies a statement/batch, not a unique movement.
        # Preserve distinct identical occurrences while making overlapping full exports idempotent.
        identity = "|".join([period, reference, f"{year}-{month}-{day}",
                             " ".join(line[match.end():amount.start()].split()),
                             amount[1], partner, " ".join(details.split())])
        fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        occurrences[fingerprint] += 1
        key = f"movement-{fingerprint}-{occurrences[fingerprint]}"
        mapping = [
            ("Furnizori de imobilizari", "fixed-assets"), ("Furnizori:", "suppliers"),
            ("Clienti:", "clients"), ("Avansuri", "advances"),
            ("Credite bancare pe termen scurt", "short-term-debt"),
            ("Cheltuieli cu serviciile bancare", "interest-and-bank-charges"),
            ("Cheltuieli privind dobanzile", "interest-and-bank-charges"),
            ("Actionari/asociati", "shareholders"),
            ("Decontari intre entitatile afiliate", "intercompany-settlements"),
            ("Personal - salarii", "net-salaries-and-taxes"),
        ]
        row_id = next((row for prefix, row in mapping if details.startswith(prefix) and row in row_ids), None)
        records[key] = {"date": f"{year}-{month}-{day}", "reference": reference,
                        "source": f"{source}, page {page}, extracted line {line_number}; SHA-256 {digest}",
                        "description": details, "partner": partner, "row_id": row_id,
                        "amount": parse_source_amount(amount[1])}
    if not records:
        raise ConfigError("No Keez cash movements found in the PDF")
    return records


if __name__ == "__main__":
    import argparse
    from engine import load_config, movement_import_summary
    parser = argparse.ArgumentParser(description="Inspect a Keez movement PDF without changing the scenario")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("cashflow.yaml"))
    args = parser.parse_args()
    config = load_config(args.config)
    records = parse_movements_pdf(args.pdf.read_bytes(), str(args.pdf), {row.id for row in config.rows}, config.settings.registration_number)
    print(movement_import_summary(records))
