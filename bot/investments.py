"""Snapshot parser for the investments spreadsheet.

You maintain a Google Sheet that tracks every savings/investment/insurance
account, with a new value column added every ~6 months. To wire that into
the desktop app's analytics WITHOUT live brokerage feeds:

  1. You manually export the sheet (File → Download → .xlsx) into the
     ``G:\\My Drive\\ynabclone\\investments\\`` folder. Filename can be
     anything; we pick the file with the most recent mtime.
  2. The bot's HTTP API exposes parsed data via ``GET /investments/snapshot``.
  3. The UI renders dashboards on top.

This module:

  * ``find_latest_snapshot(folder)`` — locates the newest xlsx file
  * ``parse_snapshot(path)`` — returns a structured snapshot:
        {
          "as_of": "YYYY-MM-DD",        # mtime of the file
          "holdings": [HoldingRow, ...],
          "totals": {
            "snapshots": [{label, date, total, ...}, ...],
          },
          "insurance": [InsuranceRow, ...],
        }

The parser is forgiving: blank rows separate sections, header signatures
("Account" in column A → savings, "Type of Insurance" → insurance) drive
section selection. Currency strings like "$45,614.00" parse to cents.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SNAPSHOTS_DIR = Path(r"G:\My Drive\ynabclone\investments")

# ────────────────────────────────────────────────────────────────────────────
# Public data shapes — these mirror the React-side types in
# ynabhelper-ui/src/lib/types.ts.
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class SnapshotValue:
    """One (date, value) point for a holding's growth series."""
    label: str           # original column header, e.g. "2026 Value (02-15-25)"
    snapshot_date: str | None  # YYYY-MM-DD if extractable, else None
    cents: int


@dataclass
class HoldingRow:
    name: str
    account_type: str
    account_number: str
    owner: str
    values: list[SnapshotValue]
    notes: str
    is_real_estate: bool

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["values"] = [asdict(v) for v in self.values]
        return d


@dataclass
class InsuranceRow:
    insurance_type: str
    through_employer: bool | None
    provider: str
    sales_contact: str
    coverage: str
    deductible: str
    annual_premium_cents: int | None
    comments: str
    renewal_date: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


_REAL_ESTATE_TYPES = {
    "home equity",
    "home equity - 1/4 value of total home",
}


_MONEY_RE = re.compile(r"^-?\$?([\d,]+(?:\.\d+)?)\s*$")


def _parse_money_to_cents(raw: Any) -> int | None:
    """Parse '$45,614.00' or 45614 or '-$84,015.00' to integer cents.

    Returns None on empty / unparseable. Negative sign preserved.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(round(raw * 100))
    s = str(raw).strip()
    if not s or s.lower() in {"n/a", "none", "—", "-"}:
        return None
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("-").lstrip("$").replace(",", "").strip()
    if not s:
        return None
    try:
        return sign * int(round(float(s) * 100))
    except (ValueError, TypeError):
        return None


_DATE_HINT_RE = re.compile(
    r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})"
)


def _extract_date_from_label(label: str) -> str | None:
    """Pull a YYYY-MM-DD out of column headers like 'End of 2021 Value
    (12-2-2021)', '2024 Value (3-3-24)', '2026 Value (02-15-25)'.
    """
    if not label:
        return None
    m = _DATE_HINT_RE.search(label)
    if not m:
        # Header without a date hint — return year if present
        ym = re.search(r"(\d{4})", label)
        if ym:
            return f"{ym.group(1)}-01-01"
        return None
    a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
    year = int(c)
    if year < 100:
        year += 2000
    month, day = a, b
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        try:
            return date(year, b, a).isoformat()
        except ValueError:
            return None


def _parse_bool(raw: Any) -> bool | None:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if s in {"TRUE", "YES", "Y", "T", "1"}:
        return True
    if s in {"FALSE", "NO", "N", "F", "0"}:
        return False
    return None


# ────────────────────────────────────────────────────────────────────────────
# File discovery
# ────────────────────────────────────────────────────────────────────────────


def find_latest_snapshot(folder: Path | str = SNAPSHOTS_DIR) -> Path | None:
    """Newest .xlsx in the folder by mtime. None if nothing's there yet."""
    folder = Path(folder)
    if not folder.exists():
        return None
    files = [
        p for p in folder.iterdir()
        if p.suffix.lower() == ".xlsx" and not p.name.startswith("~$")
    ]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def list_snapshots(folder: Path | str = SNAPSHOTS_DIR) -> list[dict[str, Any]]:
    folder = Path(folder)
    if not folder.exists():
        return []
    out = []
    for p in sorted(
        folder.iterdir(),
        key=lambda x: x.stat().st_mtime if x.exists() else 0,
        reverse=True,
    ):
        if p.suffix.lower() != ".xlsx" or p.name.startswith("~$"):
            continue
        st = p.stat()
        out.append({
            "name": p.name,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
        })
    return out


# ────────────────────────────────────────────────────────────────────────────
# Parsing
# ────────────────────────────────────────────────────────────────────────────


# Column-A signatures that mark the start of each section.
_HOLDINGS_HEADER_KEYS = ("Account",)
_INSURANCE_HEADER_KEYS = ("Type of Insurance",)
_TOTALS_HEADER_KEYS = ("Total",)


def _iter_sheet_rows(ws) -> list[list[Any]]:
    """Materialize the entire sheet as rows of cell values."""
    return [list(row) for row in ws.iter_rows(values_only=True)]


def _is_blank(row: list[Any]) -> bool:
    return all(cell is None or str(cell).strip() == "" for cell in row)


def _find_section_starts(rows: list[list[Any]]) -> dict[str, int]:
    """Return {section_name: row_index} for each known section."""
    starts: dict[str, int] = {}
    for i, row in enumerate(rows):
        if not row:
            continue
        cell0 = str(row[0] or "").strip()
        if not cell0:
            continue
        if cell0 in _HOLDINGS_HEADER_KEYS and "holdings" not in starts:
            starts["holdings"] = i
        elif cell0 in _TOTALS_HEADER_KEYS and "totals" not in starts:
            starts["totals"] = i
        elif cell0 in _INSURANCE_HEADER_KEYS and "insurance" not in starts:
            starts["insurance"] = i
    return starts


def _parse_holdings_section(
    rows: list[list[Any]], start: int, end: int,
) -> list[HoldingRow]:
    """Rows from `start` (header) to `end` exclusive."""
    if start >= len(rows):
        return []
    header = rows[start]
    # Column layout:
    #   0  Account
    #   1  Account Type
    #   2  Account #
    #   3  Primary Owner
    #   4..N-3  date-value columns
    #   N-2  Password Stored
    #   N-1  Notes
    # We treat the trailing two columns as Password/Notes when their
    # headers match; otherwise we fall back to everything-but-first-4.
    value_col_start = 4
    value_col_end = len(header)
    notes_col = None
    if value_col_end > 4 and header[-1] and "note" in str(header[-1]).lower():
        notes_col = value_col_end - 1
        value_col_end -= 1
    if (
        value_col_end > 4
        and header[value_col_end - 1]
        and "password" in str(header[value_col_end - 1]).lower()
    ):
        value_col_end -= 1
    value_headers = [
        (i, str(header[i] or "").strip()) for i in range(value_col_start, value_col_end)
    ]

    holdings: list[HoldingRow] = []
    for ri in range(start + 1, end):
        row = rows[ri]
        if _is_blank(row):
            continue
        name = str(row[0] or "").strip()
        if not name:
            continue
        account_type = str(row[1] or "").strip() if len(row) > 1 else ""
        account_number = str(row[2] or "").strip() if len(row) > 2 else ""
        owner = str(row[3] or "").strip() if len(row) > 3 else ""
        notes = ""
        if notes_col is not None and len(row) > notes_col:
            notes = str(row[notes_col] or "").strip()

        values: list[SnapshotValue] = []
        for col_idx, col_label in value_headers:
            if col_idx >= len(row):
                continue
            cents = _parse_money_to_cents(row[col_idx])
            if cents is None:
                continue
            values.append(SnapshotValue(
                label=col_label,
                snapshot_date=_extract_date_from_label(col_label),
                cents=cents,
            ))

        is_real_estate = account_type.lower() in _REAL_ESTATE_TYPES
        if not values and not account_type:
            # Likely a continuation row of structure, skip.
            continue

        holdings.append(HoldingRow(
            name=name,
            account_type=account_type,
            account_number=account_number,
            owner=owner,
            values=values,
            notes=notes,
            is_real_estate=is_real_estate,
        ))
    return holdings


def _parse_insurance_section(
    rows: list[list[Any]], start: int,
) -> list[InsuranceRow]:
    header = rows[start]
    cols = {str(h or "").strip().lower(): i for i, h in enumerate(header)}

    def col(*candidates: str) -> int | None:
        for c in candidates:
            if c in cols:
                return cols[c]
        return None

    i_type = col("type of insurance", "type")
    i_emp = col("thru employer?", "through employer?", "through employer")
    i_prov = col("insurance provider", "provider")
    i_sales = col("sales contact", "contact")
    i_cov = col("coverage")
    i_ded = col("deductable", "deductible")
    i_prem = col("annual premium", "premium")
    i_comm = col("comments", "comment", "notes")
    i_renew = col("renewal date", "renewal", "renews")

    out: list[InsuranceRow] = []
    for ri in range(start + 1, len(rows)):
        row = rows[ri]
        if _is_blank(row):
            # Insurance is the last section — blank rows after the table
            # mean the file is done.
            continue
        get = lambda i: ("" if i is None or i >= len(row) or row[i] is None
                         else str(row[i]).strip())
        ins_type = get(i_type)
        if not ins_type:
            continue
        prem_cents = _parse_money_to_cents(
            row[i_prem] if i_prem is not None and i_prem < len(row) else None
        )
        out.append(InsuranceRow(
            insurance_type=ins_type,
            through_employer=_parse_bool(
                row[i_emp] if i_emp is not None and i_emp < len(row) else None
            ),
            provider=get(i_prov),
            sales_contact=get(i_sales),
            coverage=get(i_cov),
            deductible=get(i_ded),
            annual_premium_cents=prem_cents,
            comments=get(i_comm),
            renewal_date=get(i_renew),
        ))
    return out


def _parse_totals_section(
    rows: list[list[Any]], start: int, end: int,
) -> list[dict[str, Any]]:
    """The totals block looks like:
         Total                | $674k | $749k | ... | $1.14M
         Minus Home Equity    | ...
         Annual Change        | ...
         Target Savings (...) | ...
         Delta                | ...
       Just collect each row's label + the numbers across columns.
    """
    if start >= len(rows):
        return []
    out: list[dict[str, Any]] = []
    for ri in range(start, end):
        row = rows[ri]
        if _is_blank(row):
            continue
        label = str(row[0] or "").strip()
        if not label:
            continue
        cents_values: list[int | None] = []
        for cell in row[1:]:
            if cell is None or str(cell).strip() == "":
                continue
            cents_values.append(_parse_money_to_cents(cell))
        out.append({
            "label": label,
            "cells": cents_values,
        })
    return out


def parse_snapshot(path: Path | str) -> dict[str, Any]:
    """Open an xlsx file and return a structured snapshot dict."""
    import openpyxl  # imported lazily so a missing dep doesn't break startup

    path = Path(path)
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    holdings: list[HoldingRow] = []
    insurance: list[InsuranceRow] = []
    totals: list[dict[str, Any]] = []

    # The user's sheet may have everything on one tab OR split across
    # multiple tabs. Scan every visible tab.
    for ws in wb.worksheets:
        rows = _iter_sheet_rows(ws)
        if not rows:
            continue
        starts = _find_section_starts(rows)
        n = len(rows)
        ordered = sorted(starts.items(), key=lambda kv: kv[1])
        for idx, (kind, start) in enumerate(ordered):
            end = ordered[idx + 1][1] if idx + 1 < len(ordered) else n
            if kind == "holdings":
                holdings.extend(_parse_holdings_section(rows, start, end))
            elif kind == "totals":
                totals.extend(_parse_totals_section(rows, start, end))
            elif kind == "insurance":
                insurance.extend(_parse_insurance_section(rows, start))

    wb.close()

    mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    return {
        "source_file": path.name,
        "as_of": mtime,
        "holdings": [h.to_dict() for h in holdings],
        "insurance": [i.to_dict() for i in insurance],
        "totals_rows": totals,
    }
