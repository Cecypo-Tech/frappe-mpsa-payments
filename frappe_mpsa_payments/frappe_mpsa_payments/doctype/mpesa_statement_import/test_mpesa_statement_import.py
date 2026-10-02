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
NO_ROWS = {"created": 0, "skipped": 0, "blocked": 0, "failed": 0, "details": []}

#: A shortcode with a successful Register URL on this bench (see the C2B
#: Payment Register tests), so the M-Pesa account can be resolved.
SHORTCODE = "898102"
COMPANY = "Dev Co"
MPESA_ACCOUNT = "Mpesa-898102 - DC"
BANK_ACCOUNT = "987000111 - DTB Bank - DC"


class TestMpesaStatementImport(FrappeTestCase):
    def setUp(self):
        self.addCleanup(frappe.db.rollback)
        self._paths = []
        self.addCleanup(self._remove_files)

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
        with patch(IMPORTER, return_value=NO_ROWS):
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
            file_name="renamed.csv", business_shortcode=" 777888 "
        ).insert()

        self.assertEqual(doc.business_shortcode, "777888")

    def test_typed_shortcode_that_contradicts_the_file_is_refused(self):
        doc = self._new_import(business_shortcode="111111")

        with self.assertRaisesRegex(frappe.ValidationError, "is for shortcode"):
            doc.insert()

    def test_replacing_the_file_does_not_read_the_old_shortcode_as_typed(self):
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

    def test_bank_transfer_refuses_the_mpesa_account_as_the_bank(self):
        doc = self._submitted_import()

        with self.assertRaisesRegex(frappe.ValidationError, "M-Pesa account itself"):
            doc.create_bank_transfer(MPESA_ACCOUNT)

    def test_bank_transfer_without_a_registered_shortcode_explains_why(self):
        doc = self._new_import(
            file_name="renamed.csv", business_shortcode="000999"
        ).insert()
        with patch(IMPORTER, return_value=NO_ROWS):
            doc.submit()

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
        for receipt, _completion, _details in make_fixture.NEW_INTERNAL_TRANSFERS:
            self.assertIn(receipt, html)
