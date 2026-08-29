# Copyright (c) 2026, Cecypo.Tech and contributors
# For license information, please see license.txt

import hashlib
import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, escape_html, fmt_money, get_link_to_form

from frappe_mpsa_payments import importer
from frappe_mpsa_payments.statement_parser import StatementParseError, parse_statement

CHUNK_SIZE = 1024 * 1024


class MpesaStatementImport(Document):
    def validate(self):
        file_path = self._get_file_path()

        self.file_hash = _sha256(file_path)
        self._check_already_imported()

        parsed = self._parse(file_path)

        self.business_shortcode = parsed.business_shortcode
        self.account_holder = parsed.account_holder
        self.statement_period = parsed.statement_period
        self.total_rows = cint(parsed.total_rows)
        self.payment_rows = len(parsed.payment_rows or [])

        if parsed.duplicate_receipts:
            frappe.throw(
                _(
                    "This statement lists the same receipt number more than once, "
                    "which means the file itself is malformed. Nothing has been imported. "
                    "Duplicated receipts: {0}"
                ).format(", ".join(sorted(set(parsed.duplicate_receipts)))),
                title=_("Malformed Statement"),
            )

    def db_insert(self, *args, **kwargs):
        """Turn the unique-index violation on ``file_hash`` into a human message."""
        try:
            return super().db_insert(*args, **kwargs)
        except frappe.exceptions.DuplicateEntryError:
            self._throw_already_imported()

    def on_submit(self):
        parsed = self._get_parsed()
        result = importer.import_statement(parsed, self.name)

        self.db_set(
            {
                "created_count": result["created"],
                "skipped_count": result["skipped"],
                "blocked_count": result["blocked"],
                "failed_count": result["failed"],
                "import_summary": _render_summary(parsed, result),
            },
            update_modified=False,
        )

        _log_run(self, parsed, result)

    # ------------------------------------------------------------------
    # File handling
    # ------------------------------------------------------------------

    def _get_file_path(self) -> str:
        """Resolve the attachment to a real path on disk (public or private)."""
        if not self.statement_file:
            frappe.throw(_("Please attach a statement file."))

        try:
            file_doc = frappe.get_doc("File", {"file_url": self.statement_file})
        except frappe.DoesNotExistError:
            file_doc = None

        if not file_doc:
            frappe.throw(
                _("Could not find the uploaded file record for {0}.").format(
                    self.statement_file
                )
            )

        # get_full_path() resolves both /files/... (public) and /private/files/...
        path = file_doc.get_full_path()
        try:
            with open(path, "rb"):
                pass
        except OSError as e:
            frappe.throw(
                _(
                    "The attached statement file could not be read from disk: {0}"
                ).format(str(e))
            )

        return path

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse(self, file_path: str):
        try:
            parsed = parse_statement(file_path)
        except StatementParseError as e:
            frappe.throw(
                _("This file could not be read as an M-Pesa statement: {0}").format(
                    str(e)
                ),
                title=_("Unreadable Statement"),
            )
        except Exception as e:
            frappe.log_error(
                title=f"Mpesa Statement Import {self.name}: parse failed",
                message=frappe.get_traceback(),
            )
            frappe.throw(
                _("This file could not be read as an M-Pesa statement: {0}").format(
                    str(e)
                ),
                title=_("Unreadable Statement"),
            )

        self._parsed = parsed
        return parsed

    def _get_parsed(self):
        parsed = getattr(self, "_parsed", None)
        if parsed is not None:
            return parsed

        return self._parse(self._get_file_path())

    # ------------------------------------------------------------------
    # De-duplication of the file itself
    # ------------------------------------------------------------------

    def _check_already_imported(self):
        if not self.file_hash:
            return

        earlier = frappe.db.get_value(
            "Mpesa Statement Import",
            {"file_hash": self.file_hash, "name": ("!=", self.name or "")},
            "name",
        )
        if earlier:
            self._throw_already_imported(earlier)

    def _throw_already_imported(self, earlier: str | None = None):
        earlier = earlier or frappe.db.get_value(
            "Mpesa Statement Import",
            {"file_hash": self.file_hash, "name": ("!=", self.name or "")},
            "name",
        )

        if earlier:
            frappe.throw(
                _(
                    "This exact statement file was already uploaded as {0}. Open that import instead."
                ).format(get_link_to_form("Mpesa Statement Import", earlier)),
                title=_("Statement Already Imported"),
            )

        frappe.throw(
            _("This exact statement file has already been uploaded."),
            title=_("Statement Already Imported"),
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _sha256(file_path: str) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)

    return digest.hexdigest()


def _render_summary(parsed, result: dict) -> str:
    rows = []
    unposted = 0

    for detail in result.get("details") or []:
        reference = detail.get("reference") or ""

        # A created record whose docstatus is still 0 was inserted but never
        # submitted. The target's after_insert swallows the submit exception
        # into an Error Log, so without surfacing this here the row would look
        # indistinguishable from one that reconciled cleanly.
        posted = "&mdash;"
        if detail.get("status") == "created":
            if detail.get("docstatus") == 1:
                posted = "Submitted"
            else:
                posted = "<span style='color:#b7791f'>Not submitted</span>"
                unposted += 1

        rows.append(
            "<tr>"
            f"<td>{escape_html(str(detail.get('row_index', '')))}</td>"
            f"<td>{escape_html(str(detail.get('receipt_no') or ''))}</td>"
            f"<td style='text-align:right'>{escape_html(fmt_money(detail.get('amount') or 0, currency='KES'))}</td>"
            f"<td>{escape_html(str(detail.get('status') or ''))}</td>"
            f"<td>{posted}</td>"
            f"<td>{escape_html(str(reference))}</td>"
            f"<td>{escape_html(str(detail.get('reason') or ''))}</td>"
            "</tr>"
        )

    headline = (
        f"<p><b>{result['created']}</b> created &middot; "
        f"<b>{result['skipped']}</b> skipped &middot; "
        f"<b>{result['blocked']}</b> blocked &middot; "
        f"<b>{result['failed']}</b> failed</p>"
    )

    if unposted:
        headline += (
            "<p style='color:#b7791f'><b>{0}</b> of the created records could not be "
            "submitted and are sitting as drafts &mdash; usually because no Customer "
            "could be resolved from the account number. They exist, but no Payment "
            "Entry was raised and nothing was reconciled. Check the Error Log for the "
            "per-record reason.</p>"
        ).format(unposted)

    if not rows:
        return headline + "<p>No payment rows found in this statement.</p>"

    return (
        headline
        + "<table class='table table-bordered'><thead><tr>"
        + "<th>#</th><th>Receipt</th><th>Amount</th><th>Outcome</th><th>Posted</th>"
        + "<th>Reference</th><th>Detail</th>"
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _log_run(doc, parsed, result: dict):
    """Exactly one Error Log entry per import run.

    ``frappe.log_error`` truncates its title, so the title carries only the
    headline numbers and everything substantial goes into the message.
    """
    message = {
        "import": doc.name,
        "statement_file": doc.statement_file,
        "file_hash": doc.file_hash,
        "business_shortcode": parsed.business_shortcode,
        "account_holder": parsed.account_holder,
        "statement_period": parsed.statement_period,
        "total_rows": parsed.total_rows,
        "payment_rows": len(parsed.payment_rows or []),
        "header_total_paid_in": getattr(parsed, "header_total_paid_in", None),
        "counts": {
            "created": result["created"],
            "skipped": result["skipped"],
            "blocked": result["blocked"],
            "failed": result["failed"],
        },
        "rows": result.get("details") or [],
    }

    frappe.log_error(
        title=(
            f"Mpesa Statement Import {doc.name}: "
            f"{result['created']} created, {result['skipped']} skipped, "
            f"{result['blocked']} blocked, {result['failed']} failed"
        )[:130],
        message=json.dumps(message, indent=2, default=str),
    )
