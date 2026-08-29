# Copyright (c) 2026, Cecypo.Tech and contributors
# For license information, please see license.txt

"""Row-by-row import engine for M-Pesa Utility Account statements.

Design notes
------------
* The target doctype, ``Mpesa C2B Payment Register``, has **no unique index on
  ``transid``**. Deduplication is therefore entirely application-level and
  lives here.
* Its ``before_insert`` *throws* when the receipt already exists as an
  ``Mpesa Express Request`` (an STK push). We pre-check for that rather than
  catching and string-matching the controller's exception -- pre-checking is
  far more reliable and keeps the transaction clean.
* Its ``after_insert`` auto-submits when ``auto_reconcile_c2b`` is enabled on
  the matching ``Mpesa Settings``, which creates a Payment Entry and runs
  reconciliation. **That cascade is intended and is deliberately not
  suppressed here.**
* Every row is wrapped in its own savepoint so a row that raises can never
  roll back the rows already written in this run, and no exception is ever
  allowed to escape the loop -- the user always gets a summary.
"""

from __future__ import annotations

import datetime
from typing import Any

import frappe
from frappe.utils import get_datetime

TARGET_DOCTYPE = "Mpesa C2B Payment Register"
EXPRESS_DOCTYPE = "Mpesa Express Request"

STATUS_CREATED = "created"
STATUS_SKIPPED = "skipped"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"


def import_statement(parsed, source_name: str) -> dict:
    """Import every payment row of ``parsed`` into Mpesa C2B Payment Register.

    :param parsed: a ``ParsedStatement`` from ``frappe_mpsa_payments.statement_parser``
    :param source_name: name of the ``Mpesa Statement Import`` document driving this run
    :returns: ``{"created": int, "skipped": int, "blocked": int, "failed": int, "details": list[dict]}``
    """
    counts = {
        STATUS_CREATED: 0,
        STATUS_SKIPPED: 0,
        STATUS_BLOCKED: 0,
        STATUS_FAILED: 0,
    }
    details: list[dict] = []

    for position, row in enumerate(parsed.payment_rows or []):
        outcome = _process_row(parsed, row, position)
        details.append(outcome)
        counts[outcome["status"]] += 1

    return {
        "created": counts[STATUS_CREATED],
        "skipped": counts[STATUS_SKIPPED],
        "blocked": counts[STATUS_BLOCKED],
        "failed": counts[STATUS_FAILED],
        "details": details,
    }


def _process_row(parsed, row, position: int) -> dict:
    """Handle exactly one statement row. Never raises."""
    outcome = {
        "row_index": getattr(row, "row_index", position),
        "receipt_no": getattr(row, "receipt_no", None),
        "amount": getattr(row, "amount", None),
        "billrefnumber": getattr(row, "billrefnumber", None),
        "status": STATUS_FAILED,
        "reference": None,
        "reason": None,
    }

    save_point = f"row_{position}"

    try:
        # 1. Already imported? -> skipped
        existing = frappe.db.get_value(
            TARGET_DOCTYPE, {"transid": row.receipt_no}, "name"
        )
        if existing:
            outcome["status"] = STATUS_SKIPPED
            outcome["reference"] = existing
            outcome["reason"] = f"Already present in {TARGET_DOCTYPE} as {existing}"
            return outcome

        # 2. Captured by an STK push? -> blocked. The target controller would
        #    throw on insert; do not even attempt it.
        express = frappe.db.get_value(
            EXPRESS_DOCTYPE, {"transaction_id": row.receipt_no}, "name"
        )
        if express:
            outcome["status"] = STATUS_BLOCKED
            outcome["reference"] = express
            outcome["reason"] = (
                f"Transaction already captured by {EXPRESS_DOCTYPE} {express}"
            )
            return outcome

        # 3. Insert.
        frappe.db.savepoint(save_point)
        try:
            doc = frappe.get_doc(_build_payload(parsed, row))
            doc.insert(ignore_permissions=True)
        except Exception as e:
            frappe.db.rollback(save_point=save_point)
            outcome["status"] = STATUS_FAILED
            outcome["reason"] = str(e)
            return outcome

        outcome["status"] = STATUS_CREATED
        outcome["reference"] = doc.name
        outcome["docstatus"] = doc.docstatus
        outcome["payment_entry"] = doc.get("payment_entry")
        return outcome

    except Exception as e:
        # Belt and braces: a failure in the pre-checks (or in the rollback
        # itself) must still land in the summary rather than abort the run.
        try:
            frappe.db.rollback(save_point=save_point)
        except Exception:
            pass

        outcome["status"] = STATUS_FAILED
        outcome["reason"] = str(e)
        return outcome


def _build_payload(parsed, row) -> dict[str, Any]:
    """Map a StatementRow onto Mpesa C2B Payment Register fields.

    ``full_name``, ``currency``, ``company`` and ``mode_of_payment`` are
    deliberately left to the target's ``set_missing_values``; ``currency`` is
    still passed through because the target treats it as a Link to Currency
    and overwrites it with "KES" anyway.
    """
    posting_date, posting_time = _split_completion_time(row)

    return {
        "doctype": TARGET_DOCTYPE,
        "transid": row.receipt_no,
        "transtime": row.transtime,
        "transamount": row.amount,
        "businessshortcode": parsed.business_shortcode,
        "billrefnumber": row.billrefnumber,
        "transactiontype": row.transactiontype,
        # The statement masks the payer's phone number (e.g. 25471****456).
        # Stored as-is by design -- no attempt is made to reconstruct it.
        "msisdn": row.msisdn,
        "firstname": row.firstname,
        "middlename": row.middlename,
        "lastname": row.lastname,
        "currency": row.currency,
        "default_currency": row.currency,
        "posting_date": posting_date,
        "posting_time": posting_time,
    }


def _split_completion_time(row) -> tuple[Any, Any]:
    """Split ``completion_time`` ("2026-07-28 17:16:40") into date and time.

    Falls back to ``transtime`` ("20260728171640"). If neither can be parsed we
    raise: leaving both blank makes Frappe silently default the posting date to
    *today*, which would back-date-shift an accounting record without anyone
    noticing. Raising here lands the row in ``failed`` with a clear reason
    instead, which is the safe outcome.
    """
    completion_time = getattr(row, "completion_time", None)
    if completion_time:
        try:
            dt = get_datetime(completion_time)
            return dt.date(), dt.time()
        except Exception:
            pass

    transtime = getattr(row, "transtime", None)
    if transtime:
        try:
            dt = datetime.datetime.strptime(str(transtime).strip(), "%Y%m%d%H%M%S")
            return dt.date(), dt.time()
        except Exception:
            pass

    raise ValueError(
        f"Could not read a transaction date from this row "
        f"(completion_time={completion_time!r}, transtime={transtime!r})"
    )
