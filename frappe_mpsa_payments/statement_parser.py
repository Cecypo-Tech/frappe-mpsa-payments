"""Pure-Python parser for M-Pesa Utility Account statement exports (legacy .xls).

This module deliberately does NOT import ``frappe``.  It is a plain library so
that it can be unit tested without a Frappe site, and so that the importer can
depend on it without dragging in site state.

Statement anatomy
-----------------
The exports produced by the M-Pesa org portal are OLE2 ``.xls`` workbooks with
a single sheet (normally named ``transaction``) laid out as:

* A preamble of label/value pairs (``Account Holder:``, ``Short Code:``,
  ``Time Period:``, ``Total Paid In:`` ...).  The number of preamble rows is
  **not** guaranteed, so we locate everything by scanning for label cells.
* A single column-header row whose first cell is ``Receipt No.``.
* The data rows.

Critically, every customer payment is emitted **twice** with the same
``Receipt No.``: once as a ``Pay Bill Charge`` row (blank ``Paid In``, negative
``Withdrawn``) and once as the real payment row (``Paid In`` > 0).  Only the
latter is a transaction we want to import, so :func:`parse_statement` filters
to ``Paid In > 0``.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

__all__ = [
    "ParsedStatement",
    "StatementParseError",
    "StatementRow",
    "parse_statement",
]


class StatementParseError(Exception):
    """Raised when the file is not a parseable M-Pesa statement."""


@dataclass
class StatementRow:
    """One importable customer payment (a ``Paid In`` > 0 row)."""

    receipt_no: str = ""
    transtime: str = ""
    completion_time: str = ""
    amount: float = 0.0
    billrefnumber: str = ""
    transactiontype: str = ""
    msisdn: str = ""
    firstname: str = ""
    middlename: str = ""
    lastname: str = ""
    currency: str = ""
    row_index: int = -1


@dataclass
class ParsedStatement:
    """Everything the importer needs from one statement file."""

    business_shortcode: str = ""
    account_holder: str = ""
    statement_period: str = ""
    total_rows: int = 0
    payment_rows: list[StatementRow] = field(default_factory=list)
    header_total_paid_in: float | None = None
    duplicate_receipts: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Header / label vocabulary
# --------------------------------------------------------------------------

#: Accepted spellings for the column-header row's first cell.
_RECEIPT_HEADER_KEYS = {"receiptno", "receiptnumber"}

#: Canonical column key -> accepted normalised header spellings.
_COLUMNS = {
    "receipt_no": ("receiptno", "receiptnumber"),
    "completion_time": ("completiontime",),
    "initiation_time": ("initiationtime",),
    "details": ("details",),
    "transaction_status": ("transactionstatus",),
    "paid_in": ("paidin",),
    "withdrawn": ("withdrawn",),
    "balance": ("balance",),
    "reason_type": ("reasontype",),
    "other_party_info": ("otherpartyinfo",),
    "linked_transaction_id": ("linkedtransactionid",),
    "account_no": ("acno", "accountno", "acno."),
    "currency": ("currency",),
}

#: Preamble labels we care about, canonical key -> normalised label spellings.
_PREAMBLE_LABELS = {
    "account_holder": ("accountholder",),
    "short_code": ("shortcode", "businessshortcode"),
    "time_period": ("timeperiod",),
    "total_paid_in": ("totalpaidin",),
}

#: Datetime formats seen in these exports, most specific / most likely first.
_DATETIME_FORMATS = (
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%Y-%m-%d",
)

_WHITESPACE = re.compile(r"\s+")
_TRAILING_ZERO_FLOAT = re.compile(r"^-?\d+\.0+$")


def _normalise_key(value: object) -> str:
    """Fold a header/label cell down to a comparison key.

    ``"A/C No."`` -> ``"acno"``, ``"Total Paid In:"`` -> ``"totalpaidin"``.
    Punctuation and whitespace are dropped so that minor formatting drift
    between statement versions does not break column lookup.
    """

    text = _to_text(value).strip().rstrip(":").casefold()
    return re.sub(r"[^a-z0-9]", "", text)


def _to_text(value: object) -> str:
    """Render a raw cell value as text without inventing float artefacts."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        # xlrd hands back every numeric cell as a float; 850060.0 must not
        # become the string "850060.0".
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


def _to_clean_id(value: object) -> str:
    """Coerce a cell to a clean identifier string.

    Numeric cells lose their spurious ``.0``.  Text cells are returned as-is
    (only stripped) so that identifiers with **leading zeros** survive -- an
    account number of ``"0850060"`` must not be mangled into ``"850060"``.
    The one exception is a text cell that itself carries a ``.0`` suffix,
    which is a float that leaked into a text column.
    """

    text = _to_text(value).strip()
    if _TRAILING_ZERO_FLOAT.match(text):
        text = text.split(".", 1)[0]
    return text


def _to_float(value: object) -> float:
    """Best-effort numeric coercion; unparseable or blank becomes ``0.0``."""

    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    text = _to_text(value).strip()
    if not text:
        return 0.0

    # Statements occasionally render amounts with thousands separators or a
    # currency prefix; strip anything that is not part of a number.
    text = text.replace(",", "").replace(" ", "")
    text = re.sub(r"^[A-Za-z]{3}", "", text)
    if text.startswith("(") and text.endswith(")"):
        # Accounting-style negative.
        text = "-" + text[1:-1]

    try:
        return float(text)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# Sheet access helpers
#
# ``_parse_sheet`` only ever touches ``nrows``, ``ncols``, ``cell_value`` and
# (optionally) ``cell_type``.  That keeps it usable with a lightweight fake
# sheet in tests, while still handling real xlrd date cells when present.
# --------------------------------------------------------------------------

_XL_CELL_DATE = 3


def _cell(sheet: object, row: int, col: int) -> object:
    if col < 0:
        return ""
    try:
        return sheet.cell_value(row, col)
    except (IndexError, ValueError):
        return ""


def _is_date_cell(sheet: object, row: int, col: int) -> bool:
    cell_type = getattr(sheet, "cell_type", None)
    if cell_type is None or col < 0:
        return False
    try:
        return cell_type(row, col) == _XL_CELL_DATE
    except (IndexError, ValueError):
        return False


def _row_values(sheet: object, row: int) -> list[object]:
    return [_cell(sheet, row, col) for col in range(sheet.ncols)]


def _row_is_blank(sheet: object, row: int) -> bool:
    return all(_to_text(value).strip() == "" for value in _row_values(sheet, row))


# --------------------------------------------------------------------------
# Datetime handling
# --------------------------------------------------------------------------


def _parse_datetime(
    value: object, sheet: object = None, row: int = -1, col: int = -1
) -> datetime.datetime | None:
    """Turn a completion-time cell into a datetime, or ``None`` if unusable."""

    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)

    # A genuine Excel serial date, only trustworthy when the sheet says so.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if sheet is not None and _is_date_cell(sheet, row, col):
            try:
                import xlrd  # local import: only needed for real workbooks

                datemode = getattr(getattr(sheet, "book", None), "datemode", 0)
                return xlrd.xldate.xldate_as_datetime(float(value), datemode)
            except Exception:
                return None
        return None

    text = _to_text(value).strip()
    if not text:
        return None

    text = _WHITESPACE.sub(" ", text)
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# Other Party Info -> msisdn + name parts
# --------------------------------------------------------------------------

_MSISDN_ONLY = re.compile(r"^[\d*+\s-]+$")

#: The ``<msisdn> - <name>`` separator.  Written as a regex rather than a plain
#: ``" - "`` partition so that a trailing separator with no name after it
#: (``"25470****111 - "``) is still recognised as a separator and does not
#: leave a stray dash glued onto the msisdn.  Requiring whitespace on the left
#: keeps dashes *inside* an msisdn from being mistaken for the separator.
_PARTY_SEPARATOR = re.compile(r"\s+-\s+|\s+-\s*$")


def _is_mask_token(token: str) -> bool:
    """``"****"`` is masking, not a name."""

    return bool(token) and all(char == "*" for char in token)


def _split_other_party_info(raw: object) -> tuple[str, str, str, str]:
    """Split ``"25470****111 - ALICE **** EXAMPLE"``.

    Returns ``(msisdn, firstname, middlename, lastname)``.  Original casing is
    preserved -- real statements carry a mix of upper and lower case names and
    it is not this module's job to decide which is right.  The msisdn stays
    masked exactly as the statement supplied it.
    """

    text = _WHITESPACE.sub(" ", _to_text(raw).strip())
    if not text:
        return "", "", "", ""

    parts = _PARTY_SEPARATOR.split(text, maxsplit=1)
    if len(parts) == 2:
        msisdn, name = parts
    elif _MSISDN_ONLY.match(text):
        # Just a number, no name attached.
        msisdn, name = text, ""
    else:
        msisdn, name = "", text

    msisdn = msisdn.strip()
    tokens = name.split()

    if not tokens:
        return msisdn, "", "", ""
    if len(tokens) == 1:
        return msisdn, tokens[0], "", ""

    firstname = tokens[0]
    lastname = tokens[-1]
    middle_tokens = [token for token in tokens[1:-1] if not _is_mask_token(token)]
    return msisdn, firstname, " ".join(middle_tokens), lastname


# --------------------------------------------------------------------------
# Preamble
# --------------------------------------------------------------------------


def _value_right_of(sheet: object, row: int, col: int) -> str:
    """First non-empty cell to the right of ``(row, col)``."""

    for next_col in range(col + 1, sheet.ncols):
        text = _to_text(_cell(sheet, row, next_col)).strip()
        if text:
            return text
    return ""


def _find_labels(sheet: object, header_row: int) -> dict[str, tuple[int, int]]:
    """Map canonical preamble keys to the ``(row, col)`` of their label cell.

    Only rows above the column header are scanned, and the first occurrence of
    each label wins.
    """

    found: dict[str, tuple[int, int]] = {}
    for row in range(header_row):
        for col in range(sheet.ncols):
            key = _normalise_key(_cell(sheet, row, col))
            if not key:
                continue
            for canonical, spellings in _PREAMBLE_LABELS.items():
                if canonical not in found and key in spellings:
                    found[canonical] = (row, col)
    return found


def _read_time_period(sheet: object, row: int, col: int) -> str:
    """Build ``"<from> to <to>"`` from a ``Time Period:`` row.

    The row reads ``['Time Period:', 'From', <start>, 'To', <end>]`` but the
    ``From``/``To`` markers are located by scanning rather than by offset.
    """

    values = [_to_text(value).strip() for value in _row_values(sheet, row)]
    start = end = ""
    marker = None
    for index in range(col + 1, len(values)):
        text = values[index]
        if not text:
            continue
        folded = text.casefold().rstrip(":")
        if folded == "from":
            marker = "from"
            continue
        if folded == "to":
            marker = "to"
            continue
        if marker == "from" and not start:
            start = text
        elif marker == "to" and not end:
            end = text

    if start and end:
        return f"{start} to {end}"
    if not start and not end:
        # No From/To markers -- fall back to whatever value follows the label.
        return _value_right_of(sheet, row, col)
    return start or end


# --------------------------------------------------------------------------
# Core parsing
# --------------------------------------------------------------------------


def _find_header_row(sheet: object) -> int:
    """Locate the column-header row by its ``Receipt No.`` cell.

    The row index is *never* hardcoded: statements covering different date
    ranges carry different numbers of preamble rows.  We scan the first cell
    of every row, and fall back to scanning every cell in case a leading blank
    column is ever introduced.
    """

    for row in range(sheet.nrows):
        if _normalise_key(_cell(sheet, row, 0)) in _RECEIPT_HEADER_KEYS:
            return row

    for row in range(sheet.nrows):
        for col in range(sheet.ncols):
            if _normalise_key(_cell(sheet, row, col)) in _RECEIPT_HEADER_KEYS:
                return row

    return -1


def _map_columns(sheet: object, header_row: int) -> dict[str, int]:
    """Derive canonical column name -> column index from the header row."""

    mapping: dict[str, int] = {}
    for col in range(sheet.ncols):
        key = _normalise_key(_cell(sheet, header_row, col))
        if not key:
            continue
        for canonical, spellings in _COLUMNS.items():
            if canonical not in mapping and key in spellings:
                mapping[canonical] = col
    return mapping


def _parse_sheet(sheet: object) -> ParsedStatement:
    """Parse an already-opened sheet.

    Kept separate from :func:`parse_statement` as an injectable seam: anything
    exposing ``nrows``, ``ncols`` and ``cell_value`` (optionally ``cell_type``)
    can be parsed, which makes the module testable without a workbook file.
    """

    header_row = _find_header_row(sheet)
    if header_row < 0:
        raise StatementParseError(
            "Could not find the statement column header row: no cell reading "
            "'Receipt No.' was found. This does not look like an M-Pesa "
            "statement export."
        )

    columns = _map_columns(sheet, header_row)
    for required in ("receipt_no", "paid_in"):
        if required not in columns:
            raise StatementParseError(
                f"Statement header row {header_row} is missing the required "
                f"'{required.replace('_', ' ').title()}' column. "
                f"Found columns: {[_to_text(v).strip() for v in _row_values(sheet, header_row) if _to_text(v).strip()]}"
            )

    labels = _find_labels(sheet, header_row)

    account_holder = ""
    if "account_holder" in labels:
        account_holder = _value_right_of(sheet, *labels["account_holder"])

    business_shortcode = ""
    if "short_code" in labels:
        row, col = labels["short_code"]
        business_shortcode = _to_clean_id(_value_right_of(sheet, row, col)).strip()

    statement_period = ""
    if "time_period" in labels:
        statement_period = _read_time_period(sheet, *labels["time_period"])

    header_total_paid_in: float | None = None
    if "total_paid_in" in labels:
        row, col = labels["total_paid_in"]
        raw_total = _value_right_of(sheet, row, col)
        if raw_total != "":
            header_total_paid_in = _to_float(raw_total)

    total_rows = 0
    payment_rows: list[StatementRow] = []

    for row in range(header_row + 1, sheet.nrows):
        if _row_is_blank(sheet, row):
            continue

        total_rows += 1

        amount = _to_float(_cell(sheet, row, columns["paid_in"]))
        if amount <= 0:
            # Either a 'Pay Bill Charge' twin of a real payment, or a
            # settlement/withdrawal row. Neither is an inbound payment.
            continue

        completion_col = columns.get("completion_time", -1)
        raw_completion = _cell(sheet, row, completion_col)
        completed_at = _parse_datetime(raw_completion, sheet, row, completion_col)

        msisdn, firstname, middlename, lastname = _split_other_party_info(
            _cell(sheet, row, columns.get("other_party_info", -1))
        )

        payment_rows.append(
            StatementRow(
                receipt_no=_to_clean_id(_cell(sheet, row, columns["receipt_no"])),
                transtime=completed_at.strftime("%Y%m%d%H%M%S") if completed_at else "",
                completion_time=completed_at.strftime("%Y-%m-%d %H:%M:%S")
                if completed_at
                else "",
                amount=amount,
                billrefnumber=_to_clean_id(
                    _cell(sheet, row, columns.get("account_no", -1))
                ),
                transactiontype=_to_text(
                    _cell(sheet, row, columns.get("reason_type", -1))
                ).strip(),
                msisdn=msisdn,
                firstname=firstname,
                middlename=middlename,
                lastname=lastname,
                currency=_to_text(
                    _cell(sheet, row, columns.get("currency", -1))
                ).strip(),
                row_index=row,
            )
        )

    return ParsedStatement(
        business_shortcode=business_shortcode,
        account_holder=account_holder,
        statement_period=statement_period,
        total_rows=total_rows,
        payment_rows=payment_rows,
        header_total_paid_in=header_total_paid_in,
        duplicate_receipts=_find_duplicate_receipts(payment_rows),
    )


def _find_duplicate_receipts(rows: list[StatementRow]) -> list[str]:
    """Receipt numbers occurring more than once among the payment rows.

    Returned in first-seen order, each listed once.  Note this deliberately
    ignores the charge-row twins -- those share a receipt number by design and
    are not duplicates.
    """

    seen: dict[str, int] = {}
    order: list[str] = []
    for row in rows:
        if not row.receipt_no:
            continue
        if row.receipt_no not in seen:
            order.append(row.receipt_no)
        seen[row.receipt_no] = seen.get(row.receipt_no, 0) + 1
    return [receipt for receipt in order if seen[receipt] > 1]


def _select_sheet(book: object):
    """Pick the sheet holding the transactions.

    Prefers a sheet that actually contains a ``Receipt No.`` header; falls back
    to the first sheet so the caller gets a useful error message rather than an
    arbitrary one.
    """

    sheets = book.sheets()
    if not sheets:
        raise StatementParseError("The workbook contains no sheets.")

    for sheet in sheets:
        if _find_header_row(sheet) >= 0:
            return sheet
    return sheets[0]


def parse_statement(file_path: str) -> ParsedStatement:
    """Parse an M-Pesa Utility Account statement export (legacy ``.xls``).

    Raises :class:`StatementParseError` if the file cannot be opened as an
    ``.xls`` workbook or does not contain a recognisable statement layout.
    """

    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - environment issue
        raise StatementParseError(
            "The 'xlrd' package is required to read M-Pesa .xls statements."
        ) from exc

    try:
        book = xlrd.open_workbook(file_path)
    except FileNotFoundError as exc:
        raise StatementParseError(f"Statement file not found: {file_path}") from exc
    except Exception as exc:
        raise StatementParseError(
            f"Could not open {file_path} as a legacy .xls workbook: {exc}. "
            "M-Pesa statements must be downloaded in the original .xls format "
            "(not .xlsx or .csv)."
        ) from exc

    return _parse_sheet(_select_sheet(book))
