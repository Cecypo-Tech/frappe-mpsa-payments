# Copyright (c) 2024, Navari Limited and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from .mpesa_c2b_payment_register import DEFAULT_RECONCILIATION_ORDER

MODULE = (
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype"
    ".mpesa_c2b_payment_register.mpesa_c2b_payment_register"
)


class TestMpesaC2BPaymentRegister(FrappeTestCase):
    """The Reconciliation Priority table has to actually drive matching.

    It used to be inert: `match_field` was never read by any Python, and the
    order was hardcoded, so configuring the table changed nothing.
    """

    def setUp(self):
        # get_meta hits the database on a cold cache, and these tests patch
        # frappe.db.get_value out from under it. Warm the metas first so the
        # patch only ever intercepts the lookups under test.
        for doctype in ("Sales Invoice", "Sales Order", "Quotation", "Customer"):
            frappe.get_meta(doctype)

    def _register(self, **fields):
        doc = frappe.new_doc("Mpesa C2B Payment Register")
        doc.businessshortcode = "898102"
        doc.billrefnumber = "ACC-001"
        doc.company = "Dev Co"
        doc.update(fields)
        return doc

    def _with_priority(self, rows):
        """Patch the configured rows without touching Mpesa Settings."""
        return patch.object(
            frappe.model.document.Document,
            "_reconciliation_order",
            lambda _self: rows,
            create=True,
        )

    def test_unconfigured_settings_keep_the_historical_order(self):
        doc = self._register()

        with patch("frappe.db.get_value", return_value=None), patch(
            "frappe.get_all", return_value=[]
        ):
            self.assertEqual(doc._reconciliation_order(), list(DEFAULT_RECONCILIATION_ORDER))

    def test_configured_rows_replace_the_default_order(self):
        doc = self._register()
        rows = [
            frappe._dict(target_doctype="Customer", match_field="tax_id"),
            frappe._dict(target_doctype="Sales Invoice", match_field="name"),
        ]

        with patch("frappe.db.get_value", return_value="898102"), patch(
            "frappe.get_all", return_value=rows
        ):
            self.assertEqual(
                doc._reconciliation_order(),
                [("Customer", "tax_id"), ("Sales Invoice", "name")],
            )

    def test_a_row_without_a_match_field_falls_back_to_name(self):
        doc = self._register()
        rows = [frappe._dict(target_doctype="Sales Order", match_field=None)]

        with patch("frappe.db.get_value", return_value="898102"), patch(
            "frappe.get_all", return_value=rows
        ):
            self.assertEqual(doc._reconciliation_order(), [("Sales Order", "name")])

    def test_configured_match_field_is_used_in_the_lookup(self):
        """The whole point: a non-name match_field must reach the query."""
        doc = self._register(billrefnumber="P051234567X")

        with patch.object(
            type(doc), "_reconciliation_order", lambda _self: [("Customer", "tax_id")]
        ), patch("frappe.db.get_value", return_value="CUST-0001") as mock_get_value:
            doc._find_customer_from_billref("P051234567X")

        self.assertEqual(doc.customer, "CUST-0001")
        filters = mock_get_value.call_args.args[1]
        self.assertEqual(filters.get("tax_id"), "P051234567X")
        self.assertNotIn("name", filters)

    def test_priority_stops_at_the_first_hit(self):
        doc = self._register()
        order = [("Sales Invoice", "name"), ("Customer", "name")]

        with patch.object(type(doc), "_reconciliation_order", lambda _self: order), patch(
            "frappe.db.get_value", side_effect=["CUST-FROM-INVOICE", "CUST-FROM-CUSTOMER"]
        ) as mock_get_value:
            doc._find_customer_from_billref("ACC-001")

        self.assertEqual(doc.customer, "CUST-FROM-INVOICE")
        self.assertEqual(mock_get_value.call_count, 1)

    def test_a_row_pointing_at_a_missing_doctype_is_skipped(self):
        doc = self._register()
        order = [("No Such Doctype", "name"), ("Customer", "name")]

        with patch.object(type(doc), "_reconciliation_order", lambda _self: order), patch(
            "frappe.db.get_value", return_value="CUST-0001"
        ):
            doc._find_customer_from_billref("ACC-001")

        self.assertEqual(doc.customer, "CUST-0001")

    def test_matching_refs_follow_the_configured_order(self):
        """Sales Order first means an order wins even when an invoice matches."""
        doc = self._register(customer="CUST-0001")
        order = [("Sales Order", "name"), ("Sales Invoice", "name")]

        with patch.object(type(doc), "_reconciliation_order", lambda _self: order), patch(
            "frappe.get_value", return_value="SO-0001"
        ):
            invoice, sales_order = doc._get_matching_refs()

        self.assertIsNone(invoice)
        self.assertEqual(sales_order, "SO-0001")

    def test_matching_refs_skip_doctypes_a_payment_cannot_settle(self):
        """A Customer row resolves the payer but cannot be reconciled against."""
        doc = self._register(customer="CUST-0001")
        order = [("Customer", "name")]

        with patch.object(type(doc), "_reconciliation_order", lambda _self: order):
            self.assertEqual(doc._get_matching_refs(), (None, None))
