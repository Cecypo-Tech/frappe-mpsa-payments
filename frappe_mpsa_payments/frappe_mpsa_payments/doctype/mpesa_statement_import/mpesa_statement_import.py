# Copyright (c) 2026, Cecypo.Tech and contributors
# For license information, please see license.txt

import hashlib
import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, escape_html, fmt_money, get_datetime, get_link_to_form

from frappe_mpsa_payments import importer
from frappe_mpsa_payments.statement_parser import StatementParseError, parse_statement

CHUNK_SIZE = 1024 * 1024


class MpesaStatementImport(Document):
    def validate(self):
        file_path = self._get_file_path()

        self.file_hash = _sha256(file_path)
        self._check_already_imported()

        parsed = self._parse(file_path)

        self.business_shortcode = self._resolve_shortcode(parsed.business_shortcode)
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

    def _resolve_shortcode(self, from_file: str) -> str:
        """The file's shortcode, else the one the user typed; never two different ones.

        The newer statement template carries its shortcode only in the file
        name, so a renamed file has none and the user types it in.
        """
        typed = (self.business_shortcode or "").strip()

        # A value left over from the previously attached file was not typed.
        before = self.get_doc_before_save()
        if (
            before
            and before.statement_file != self.statement_file
            and typed == (before.business_shortcode or "").strip()
        ):
            typed = ""

        from_file = (from_file or "").strip()
        if from_file and typed and typed != from_file:
            frappe.throw(
                _(
                    "This statement is for shortcode {0}, but {1} was entered. "
                    "Clear the Business Shortcode, or attach the statement for {1}."
                ).format(from_file, typed),
                title=_("Shortcode Mismatch"),
            )

        shortcode = from_file or typed
        if not shortcode:
            frappe.throw(
                _(
                    "The business shortcode could not be read from this statement. "
                    "Statements named like 'Statements for 123456 from ...' carry it in "
                    "the file name; this file has been renamed. Enter the Business "
                    "Shortcode and save again."
                ),
                title=_("Business Shortcode Needed"),
            )
        return shortcode

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
    # Bank transfer
    # ------------------------------------------------------------------

    @frappe.whitelist()
    def get_bank_transfer_context(self) -> dict:
        """What the Create Bank Transfer dialog needs to show and filter on."""
        self._check_can_transfer()
        withdrawals = self._get_parsed().bank_withdrawals
        if not withdrawals:
            return {"withdrawals": []}

        company, mode_of_payment, paid_from = _mpesa_account(self.business_shortcode)
        return {
            "company": company,
            "mode_of_payment": mode_of_payment,
            "paid_from": paid_from,
            "withdrawals": [
                {
                    "receipt_no": w.receipt_no,
                    "completion_time": w.completion_time,
                    "amount": w.amount,
                    "details": w.details,
                    "payment_entry": _existing_transfer(company, w.receipt_no),
                }
                for w in withdrawals
            ],
        }

    @frappe.whitelist()
    def create_bank_transfer(self, bank_account: str) -> list[dict]:
        """Draft one Internal Transfer per withdrawal-to-bank row.

        Drafts, not submitted: the user chose the bank and should check the
        entry before it posts.  A receipt that already has a non-cancelled
        Internal Transfer is returned as-is, so clicking twice is harmless.
        """
        self._check_can_transfer()
        withdrawals = self._get_parsed().bank_withdrawals
        if not withdrawals:
            return []

        company, mode_of_payment, paid_from = _mpesa_account(self.business_shortcode)
        _check_bank_account(bank_account, company, paid_from)

        results = []
        for withdrawal in withdrawals:
            existing = _existing_transfer(company, withdrawal.receipt_no)
            if existing:
                results.append({"name": existing, "created": False})
                continue

            if not withdrawal.completion_time:
                frappe.throw(
                    _(
                        "Could not read the date of withdrawal {0} from the statement."
                    ).format(withdrawal.receipt_no)
                )
            posting = get_datetime(withdrawal.completion_time).date()

            pe = frappe.new_doc("Payment Entry")
            pe.update(
                {
                    "payment_type": "Internal Transfer",
                    "company": company,
                    "mode_of_payment": mode_of_payment,
                    "posting_date": posting,
                    "paid_from": paid_from,
                    "paid_to": bank_account,
                    "paid_amount": withdrawal.amount,
                    "received_amount": withdrawal.amount,
                    "reference_no": withdrawal.receipt_no,
                    "reference_date": posting,
                    # Without this ERPNext replaces the remark with its own.
                    "custom_remarks": 1,
                    "remarks": _("{0} ({1}) from M-Pesa statement import {2}").format(
                        withdrawal.details, withdrawal.receipt_no, self.name
                    ),
                }
            )
            pe.insert()
            results.append({"name": pe.name, "created": True})

        return results

    def _check_can_transfer(self):
        if self.docstatus != 1:
            frappe.throw(
                _(
                    "Submit this import first, so the payments are in the books before moving the money."
                )
            )

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

        # The newer template's shortcode is in the name it was uploaded under.
        self._file_name = file_doc.file_name

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
            parsed = parse_statement(
                file_path, original_name=getattr(self, "_file_name", None)
            )
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


def _mpesa_account(shortcode: str) -> tuple[str, str, str]:
    """(company, mode_of_payment, account) for the shortcode's M-Pesa ledger.

    Resolved the same way Mpesa C2B Payment Register does: through the
    successfully registered URL for the shortcode.
    """
    registered = frappe.get_all(
        "Mpesa C2B Payment Register URL",
        filters={"business_shortcode": shortcode, "register_status": "Success"},
        fields=["company", "mode_of_payment"],
        limit=1,
    )
    if not registered or not registered[0].company or not registered[0].mode_of_payment:
        frappe.throw(
            _(
                "Shortcode {0} has no successfully registered Mpesa C2B Payment Register URL "
                "with a company and mode of payment, so the M-Pesa account to transfer "
                "from is unknown."
            ).format(shortcode),
            title=_("M-Pesa Account Unknown"),
        )

    company, mode_of_payment = registered[0].company, registered[0].mode_of_payment
    account = frappe.db.get_value(
        "Mode of Payment Account",
        {
            "parenttype": "Mode of Payment",
            "parent": mode_of_payment,
            "company": company,
        },
        "default_account",
    )
    if not account:
        frappe.throw(
            _("Mode of Payment {0} has no default account for {1}.").format(
                get_link_to_form("Mode of Payment", mode_of_payment), company
            ),
            title=_("M-Pesa Account Unknown"),
        )
    return company, mode_of_payment, account


def _check_bank_account(bank_account: str, company: str, paid_from: str):
    if not bank_account:
        frappe.throw(_("Select the bank account the money went to."))

    account = frappe.db.get_value(
        "Account", bank_account, ["company", "is_group", "account_type"], as_dict=True
    )
    if not account:
        frappe.throw(_("Account {0} does not exist.").format(bank_account))
    if account.company != company:
        frappe.throw(
            _("Account {0} does not belong to {1}.").format(bank_account, company)
        )
    if account.is_group:
        frappe.throw(_("Account {0} is a group account.").format(bank_account))
    if account.account_type not in ("Bank", "Cash"):
        frappe.throw(_("Account {0} is not a bank account.").format(bank_account))
    if bank_account == paid_from:
        frappe.throw(
            _("{0} is the M-Pesa account itself; pick the bank.").format(bank_account)
        )


def _existing_transfer(company: str, receipt_no: str) -> str | None:
    return frappe.db.get_value(
        "Payment Entry",
        {
            "company": company,
            "payment_type": "Internal Transfer",
            "reference_no": receipt_no,
            "docstatus": ("<", 2),
        },
        "name",
    )


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
        return (
            headline
            + "<p>No payment rows found in this statement.</p>"
            + _render_other_rows(parsed)
        )

    return (
        headline
        + "<table class='table table-bordered'><thead><tr>"
        + "<th>#</th><th>Receipt</th><th>Amount</th><th>Outcome</th><th>Posted</th>"
        + "<th>Reference</th><th>Detail</th>"
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + _render_other_rows(parsed)
    )


def _render_other_rows(parsed) -> str:
    """Bank withdrawals and the rows deliberately not imported."""
    html = ""

    withdrawals = getattr(parsed, "bank_withdrawals", None) or []
    if withdrawals:
        html += (
            "<p><b>Withdrawals to bank</b> &mdash; use <i>Create Bank Transfer</i> to draft "
            "the Payment Entry.</p><table class='table table-bordered'><thead><tr>"
            "<th>Receipt</th><th>Time</th><th>Amount</th><th>Detail</th></tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td>{escape_html(w.receipt_no)}</td>"
                f"<td>{escape_html(w.completion_time)}</td>"
                f"<td style='text-align:right'>{escape_html(fmt_money(w.amount, currency='KES'))}</td>"
                f"<td>{escape_html(w.details)}</td>"
                "</tr>"
                for w in withdrawals
            )
            + "</tbody></table>"
        )

    ignored = getattr(parsed, "ignored_rows", None) or []
    if ignored:
        html += (
            "<p><b>Not imported</b> &mdash; Safaricom-internal transfers and any other row "
            "that is not a customer payment.</p><table class='table table-bordered'><thead><tr>"
            "<th>Receipt</th><th>Time</th><th>Paid In</th><th>Withdrawn</th><th>Detail</th>"
            "</tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td>{escape_html(i.receipt_no)}</td>"
                f"<td>{escape_html(i.completion_time)}</td>"
                f"<td style='text-align:right'>{escape_html(fmt_money(i.paid_in, currency='KES'))}</td>"
                f"<td style='text-align:right'>{escape_html(fmt_money(i.withdrawn, currency='KES'))}</td>"
                f"<td>{escape_html(i.details)}</td>"
                "</tr>"
                for i in ignored
            )
            + "</tbody></table>"
        )

    return html


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
        "bank_withdrawals": [
            vars(w) for w in getattr(parsed, "bank_withdrawals", None) or []
        ],
        "ignored_rows": [vars(i) for i in getattr(parsed, "ignored_rows", None) or []],
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
