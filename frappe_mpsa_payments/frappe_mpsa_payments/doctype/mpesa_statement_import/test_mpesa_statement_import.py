# Copyright (c) 2026, Cecypo.Tech and contributors
# For license information, please see license.txt

import os
import tempfile
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_mpsa_payments.tests.fixtures import make_fixture

from .mpesa_statement_import import _render_summary

IMPORTER = "frappe_mpsa_payments.importer.import_statement"
#: The run's Error Log entry is written outside the test transaction, so the
#: rollback would leave one behind per submit.
LOG_RUN = (
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype"
    ".mpesa_statement_import.mpesa_statement_import._log_run"
)
NO_ROWS = {"created": 0, "skipped": 0, "blocked": 0, "failed": 0, "details": []}

# The bank transfer tests lean on ERPNext's own test company, which the runner
# only creates for doctypes a module declares.
test_dependencies = ["Company"]

#: ERPNext's test company; this suite builds its own M-Pesa ledger, bank and
#: registered shortcode in it, so nothing depends on a particular site's data.
COMPANY = "Wind Power LLC"
SHORTCODE = "999654"
TYPED_SHORTCODE = "777888"
MODE_OF_PAYMENT = "_Test Statement Mpesa"
MPESA_ACCOUNT = "_Test Statement Mpesa - WP"
BANK_ACCOUNT = "_Test Statement Bank - WP"
FOREIGN_BANK_ACCOUNT = "_Test Statement Bank EUR - WP"


def _make_account(account_name: str, currency: str) -> str:
    name = f"{account_name} - WP"
    if not frappe.db.exists("Account", name):
        frappe.get_doc(
            {
                "doctype": "Account",
                "account_name": account_name,
                "company": COMPANY,
                "parent_account": "Bank Accounts - WP",
                "account_type": "Bank",
                "account_currency": currency,
            }
        ).insert(ignore_permissions=True)
    return name


def _make_registered_shortcode():
    """An M-Pesa ledger, its mode of payment and a Success register URL."""
    currency = frappe.db.get_value("Company", COMPANY, "default_currency")
    _make_account("_Test Statement Mpesa", currency)
    _make_account("_Test Statement Bank", currency)
    _make_account("_Test Statement Bank EUR", "EUR" if currency != "EUR" else "USD")

    if not frappe.db.exists("Mode of Payment", MODE_OF_PAYMENT):
        frappe.get_doc(
            {
                "doctype": "Mode of Payment",
                "mode_of_payment": MODE_OF_PAYMENT,
                "type": "Bank",
                "accounts": [{"company": COMPANY, "default_account": MPESA_ACCOUNT}],
            }
        ).insert(ignore_permissions=True)

    _register_url(SHORTCODE)
    # The shortcode a renamed file's tests type in.
    _register_url(TYPED_SHORTCODE)


def _register_url(
    shortcode: str,
    status: str = "Success",
    mode_of_payment: str | None = MODE_OF_PAYMENT,
):
    if frappe.db.exists("Mpesa C2B Payment Register URL", shortcode):
        return
    # db_insert: the controller's validate registers the URL with Safaricom.
    frappe.get_doc(
        {
            "doctype": "Mpesa C2B Payment Register URL",
            "name": shortcode,
            "mpesa_settings": shortcode,
            "business_shortcode": shortcode,
            "register_status": status,
            "company": COMPANY,
            "mode_of_payment": mode_of_payment,
        }
    ).db_insert()


class TestMpesaStatementImport(FrappeTestCase):
    def setUp(self):
        self.addCleanup(frappe.db.rollback)
        self._paths = []
        self.addCleanup(self._remove_files)
        _make_registered_shortcode()

    def _remove_files(self):
        for path in self._paths:
            if os.path.exists(path):
                os.remove(path)

    def _attach(
        self, file_name: str, writer=make_fixture.write_new_template_csv
    ) -> str:
        """Upload a generated statement under ``file_name`` and return its URL."""
        with tempfile.TemporaryDirectory() as tmp:
            path = writer(os.path.join(tmp, "statement"))
            with open(path, "rb") as f:
                content = f.read()

        file_doc = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": file_name,
                "is_private": 1,
                "content": content,
            }
        ).insert(ignore_permissions=True)
        self._paths.append(file_doc.get_full_path())
        return file_doc.file_url

    def _new_import(self, file_name=None, **fields):
        file_name = (
            file_name or f"Statements for {SHORTCODE} from 2020-01-04 to 2020-01-04.csv"
        )
        doc = frappe.new_doc("Mpesa Statement Import")
        doc.statement_file = self._attach(file_name)
        doc.update(fields)
        return doc

    def _submitted_import(self):
        doc = self._new_import().insert()
        with patch(IMPORTER, return_value=NO_ROWS), patch(LOG_RUN):
            doc.submit()
        return doc

    # ------------------------------------------------------------------
    # Shortcode
    # ------------------------------------------------------------------

    def test_new_template_takes_shortcode_and_period_from_the_file_name(self):
        doc = self._new_import().insert()

        self.assertEqual(doc.business_shortcode, SHORTCODE)
        self.assertEqual(doc.statement_period, "2020-01-04 to 2020-01-04")
        self.assertEqual(doc.payment_rows, len(make_fixture.NEW_PAYMENTS))

    def test_renamed_file_without_a_typed_shortcode_is_refused(self):
        doc = self._new_import(file_name="renamed.csv")

        with self.assertRaisesRegex(
            frappe.ValidationError, "Enter the Business Shortcode"
        ):
            doc.insert()

    def test_renamed_file_keeps_the_typed_shortcode(self):
        doc = self._new_import(
            file_name="renamed.csv", business_shortcode=f" {TYPED_SHORTCODE} "
        ).insert()

        self.assertEqual(doc.business_shortcode, TYPED_SHORTCODE)

    def test_typed_shortcode_that_contradicts_the_file_is_refused(self):
        doc = self._new_import(business_shortcode="111111")

        with self.assertRaisesRegex(frappe.ValidationError, "is for shortcode"):
            doc.insert()

    def test_typed_shortcode_reaches_the_imported_payments(self):
        """A renamed file's typed shortcode must be what the rows are written with.

        The importer builds every register from ``parsed.business_shortcode``;
        left blank, no Mpesa Settings, company or mode of payment is found.
        """
        doc = self._new_import(
            file_name="renamed.csv", business_shortcode=TYPED_SHORTCODE
        ).insert()

        with patch(IMPORTER, return_value=NO_ROWS) as importer, patch(LOG_RUN):
            doc.submit()

        parsed = importer.call_args.args[0]
        self.assertEqual(parsed.business_shortcode, TYPED_SHORTCODE)

    def test_replacing_with_a_renamed_file_keeps_the_typed_shortcode(self):
        doc = self._new_import(
            file_name="renamed.csv", business_shortcode=TYPED_SHORTCODE
        ).insert()

        doc.statement_file = self._attach(
            "renamed-again.xlsx", writer=make_fixture.write_new_template_xlsx
        )
        doc.save()

        self.assertEqual(doc.business_shortcode, TYPED_SHORTCODE)

    def test_replacing_the_file_does_not_read_the_old_shortcode_as_typed(self):
        _register_url("111111")
        doc = self._new_import(
            file_name="Statements for 111111 from 2020-01-04 to 2020-01-04.csv"
        ).insert()
        self.assertEqual(doc.business_shortcode, "111111")

        # Same content would trip the one-import-per-file hash, so use the xlsx.
        doc.statement_file = self._attach(
            f"Statements for {SHORTCODE} from 2020-01-04 to 2020-01-04.xlsx",
            writer=make_fixture.write_new_template_xlsx,
        )
        doc.save()

        self.assertEqual(doc.business_shortcode, SHORTCODE)

    # ------------------------------------------------------------------
    # Registered shortcode
    # ------------------------------------------------------------------

    def test_unregistered_shortcode_from_the_file_name_is_refused(self):
        doc = self._new_import(
            file_name="Statements for 555111 from 2020-01-04 to 2020-01-04.csv"
        )

        with self.assertRaisesRegex(
            frappe.ValidationError, "555111 has no successfully"
        ):
            doc.insert()

    def test_unregistered_typed_shortcode_is_refused(self):
        doc = self._new_import(file_name="renamed.csv", business_shortcode="555111")

        with self.assertRaisesRegex(
            frappe.ValidationError, "555111 has no successfully"
        ):
            doc.insert()

    def test_shortcode_whose_registration_failed_is_refused(self):
        _register_url("555222", status="Failed")
        doc = self._new_import(file_name="renamed.csv", business_shortcode="555222")

        with self.assertRaisesRegex(
            frappe.ValidationError, "555222 has no successfully"
        ):
            doc.insert()

    def test_shortcode_registered_without_a_mode_of_payment_is_refused(self):
        _register_url("555333", mode_of_payment=None)
        doc = self._new_import(file_name="renamed.csv", business_shortcode="555333")

        with self.assertRaisesRegex(
            frappe.ValidationError, "555333 has no successfully"
        ):
            doc.insert()

    def test_submit_checks_the_registration_again(self):
        # Frappe runs validate on submit, so a registration that failed after
        # the draft was saved still stops the import.
        doc = self._new_import().insert()
        frappe.db.set_value(
            "Mpesa C2B Payment Register URL", SHORTCODE, "register_status", "Failed"
        )

        with patch(IMPORTER, return_value=NO_ROWS) as importer, patch(LOG_RUN):
            with self.assertRaisesRegex(frappe.ValidationError, "has no successfully"):
                doc.submit()
        importer.assert_not_called()

    # ------------------------------------------------------------------
    # Preview before importing
    # ------------------------------------------------------------------

    def test_import_preview_says_what_submitting_will_create(self):
        doc = self._new_import().insert()

        preview = doc.get_import_preview()

        parsed = doc._get_parsed()
        self.assertEqual(preview["business_shortcode"], SHORTCODE)
        self.assertEqual(preview["company"], COMPANY)
        self.assertEqual(preview["mode_of_payment"], MODE_OF_PAYMENT)
        self.assertFalse(preview["auto_reconcile"])
        self.assertEqual(preview["statement_period"], "2020-01-04 to 2020-01-04")
        self.assertEqual(preview["payment_count"], len(make_fixture.NEW_PAYMENTS))
        self.assertEqual(preview["new_count"], len(make_fixture.NEW_PAYMENTS))
        self.assertEqual(preview["new_total"], make_fixture.NEW_TOTAL_PAID_IN)
        self.assertEqual(preview["already_present"], 0)
        self.assertEqual(preview["stk_captured"], 0)
        self.assertEqual(preview["bank_withdrawal_count"], 1)
        self.assertEqual(preview["ignored_count"], len(parsed.ignored_rows))
        # Each fixture sweep is a Paid In row plus its Withdrawn twin.
        self.assertEqual(
            preview["internal_transfer_count"], 2 * len(make_fixture.NEW_INTERNAL_TRANSFERS)
        )

    def test_import_preview_counts_only_payments_it_will_create(self):
        """Rows the importer skips or blocks are not counted as new."""
        doc = self._new_import().insert()
        present, captured = make_fixture.NEW_PAYMENTS[0], make_fixture.NEW_PAYMENTS[1]
        frappe.get_doc(
            {
                "doctype": "Mpesa C2B Payment Register",
                "name": "_Test Statement Present",
                "transid": present[0],
                "businessshortcode": SHORTCODE,
            }
        ).db_insert()
        frappe.get_doc(
            {
                "doctype": "Mpesa Express Request",
                "name": "_Test Statement Captured",
                "transaction_id": captured[0],
            }
        ).db_insert()

        preview = doc.get_import_preview()

        self.assertEqual(preview["already_present"], 1)
        self.assertEqual(preview["stk_captured"], 1)
        self.assertEqual(preview["new_count"], len(make_fixture.NEW_PAYMENTS) - 2)
        self.assertEqual(
            preview["new_total"],
            round(make_fixture.NEW_TOTAL_PAID_IN - present[3] - captured[3], 2),
        )

    def test_import_preview_reports_auto_reconcile(self):
        frappe.get_doc(
            {
                "doctype": "Mpesa Settings",
                "name": "_Test Statement Settings",
                "payment_gateway_name": "_Test Statement Settings",
                "business_shortcode": SHORTCODE,
                "auto_reconcile_c2b": 1,
            }
        ).db_insert()
        doc = self._new_import().insert()

        self.assertTrue(doc.get_import_preview()["auto_reconcile"])

    def test_import_preview_needs_a_draft(self):
        doc = self._submitted_import()

        with self.assertRaisesRegex(frappe.ValidationError, "already been imported"):
            doc.get_import_preview()

    # ------------------------------------------------------------------
    # Bank transfer
    # ------------------------------------------------------------------

    def test_bank_transfer_needs_a_submitted_import(self):
        doc = self._new_import().insert()

        with self.assertRaisesRegex(frappe.ValidationError, "Submit this import first"):
            doc.create_bank_transfer(BANK_ACCOUNT)

    def test_bank_transfer_drafts_an_internal_transfer_per_withdrawal(self):
        doc = self._submitted_import()

        results = doc.create_bank_transfer(BANK_ACCOUNT)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["created"])
        pe = frappe.get_doc("Payment Entry", results[0]["name"])
        receipt, _completion, _details = make_fixture.NEW_BANK_WITHDRAWAL
        self.assertEqual(pe.docstatus, 0)
        self.assertEqual(pe.payment_type, "Internal Transfer")
        self.assertEqual(pe.company, COMPANY)
        self.assertEqual(pe.paid_from, MPESA_ACCOUNT)
        self.assertEqual(pe.paid_to, BANK_ACCOUNT)
        self.assertEqual(pe.paid_amount, make_fixture.NEW_TOTAL_PAID_IN)
        self.assertEqual(pe.received_amount, make_fixture.NEW_TOTAL_PAID_IN)
        self.assertEqual(str(pe.posting_date), "2020-01-04")
        self.assertEqual(pe.reference_no, receipt)
        self.assertIn(doc.name, pe.remarks)

    def test_bank_transfer_twice_reuses_the_draft(self):
        doc = self._submitted_import()

        first = doc.create_bank_transfer(BANK_ACCOUNT)
        second = doc.create_bank_transfer(BANK_ACCOUNT)

        self.assertEqual(second, [{"name": first[0]["name"], "created": False}])
        receipt = make_fixture.NEW_BANK_WITHDRAWAL[0]
        self.assertEqual(
            frappe.db.count("Payment Entry", {"reference_no": receipt, "docstatus": 0}),
            1,
        )

    def test_bank_transfer_refuses_a_bank_in_another_currency(self):
        doc = self._submitted_import()

        with self.assertRaisesRegex(
            frappe.ValidationError, r"is in \w+, but the M-Pesa account"
        ):
            doc.create_bank_transfer(FOREIGN_BANK_ACCOUNT)

    def test_bank_transfer_refuses_the_mpesa_account_as_the_bank(self):
        doc = self._submitted_import()

        with self.assertRaisesRegex(frappe.ValidationError, "M-Pesa account itself"):
            doc.create_bank_transfer(MPESA_ACCOUNT)

    def test_bank_transfer_without_a_registered_shortcode_explains_why(self):
        doc = self._submitted_import()
        # The registration failed or was redone after the import.
        frappe.db.set_value(
            "Mpesa C2B Payment Register URL", SHORTCODE, "register_status", "Failed"
        )

        with self.assertRaisesRegex(
            frappe.ValidationError, "no successfully registered"
        ):
            doc.get_bank_transfer_context()

    def test_bank_transfer_context_lists_the_withdrawal(self):
        doc = self._submitted_import()

        context = doc.get_bank_transfer_context()

        self.assertEqual(context["company"], COMPANY)
        self.assertEqual(context["paid_from"], MPESA_ACCOUNT)
        self.assertEqual(
            [w["receipt_no"] for w in context["withdrawals"]],
            [make_fixture.NEW_BANK_WITHDRAWAL[0]],
        )
        self.assertIsNone(context["withdrawals"][0]["payment_entry"])

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def test_summary_lists_withdrawals_and_rows_not_imported(self):
        doc = self._new_import().insert()

        html = _render_summary(doc._get_parsed(), NO_ROWS)

        self.assertIn("Withdrawals to bank", html)
        self.assertIn(make_fixture.NEW_BANK_WITHDRAWAL[0], html)
        self.assertIn("Not imported", html)
        self.assertIn("<td>Internal org transfer</td>", html)
        for receipt, _completion, _details in make_fixture.NEW_INTERNAL_TRANSFERS:
            self.assertIn(receipt, html)

    def test_summary_keeps_the_reason_in_a_hover_not_a_column(self):
        doc = self._new_import().insert()
        result = dict(
            NO_ROWS,
            skipped=1,
            failed=1,
            details=[
                {
                    "row_index": 1,
                    "receipt_no": "AAA",
                    "amount": 10,
                    "status": "skipped",
                    "reference": "MPC2B-X",
                    "reason": "Already present in Mpesa C2B Payment Register as MPC2B-X",
                },
                {
                    "row_index": 2,
                    "receipt_no": "BBB",
                    "amount": 10,
                    "status": "failed",
                    "reference": None,
                    "reason": "Customer not found",
                },
            ],
        )

        html = _render_summary(doc._get_parsed(), result)

        self.assertNotIn("<th>Reference</th><th>Detail</th>", html)
        self.assertIn(
            'title="Already present in Mpesa C2B Payment Register as MPC2B-X"', html
        )
        self.assertNotIn(">Already present in", html)
        # A failure's message is the only clue, so it stays readable.
        self.assertIn(">Customer not found<", html)
        self.assertIn("/desk/mpesa-c2b-payment-register/MPC2B-X", html)
