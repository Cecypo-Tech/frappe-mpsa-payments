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

    #: Captured once, before any patching, so the tests never need the database.
    METAS = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for doctype in ("Sales Invoice", "Sales Order", "Quotation", "Customer"):
            cls.METAS[doctype] = frappe.get_meta(doctype)

    def _metas(self):
        """Serve real metas from the capture, and raise for anything unknown.

        These tests patch frappe.db.get_value, which get_meta also uses on a
        cold cache - and a failed lookup can clear that cache mid-test. Serving
        metas from a pre-captured dict keeps the patch confined to the lookups
        actually under test.
        """

        def fake_get_meta(doctype, *args, **kwargs):
            if doctype in self.METAS:
                return self.METAS[doctype]
            raise frappe.DoesNotExistError(doctype)

        return patch("frappe.get_meta", side_effect=fake_get_meta)

    @staticmethod
    def _lookups(matches, disabled=()):
        """A frappe.db.get_value fake for the two shapes the walk issues.

        Matching passes a filters dict; the disabled check passes a customer
        name. Telling them apart on that keeps a single patch serving both,
        so the tests do not need a database.
        """
        remaining = list(matches)

        def fake(doctype, filters, fieldname=None, *args, **kwargs):
            if doctype == "Customer" and isinstance(filters, str):
                return 1 if filters in disabled else 0
            return remaining.pop(0) if remaining else None

        return fake

    def _register(self, **fields):
        doc = frappe.new_doc("Mpesa C2B Payment Register")
        doc.businessshortcode = "898102"
        doc.billrefnumber = "ACC-001"
        doc.company = "Dev Co"
        doc.update(fields)
        return doc

    def test_unconfigured_settings_keep_the_historical_order(self):
        doc = self._register()

        with (
            patch("frappe.db.get_value", return_value=None),
            patch("frappe.get_all", return_value=[]),
        ):
            self.assertEqual(
                doc._reconciliation_order(), list(DEFAULT_RECONCILIATION_ORDER)
            )

    def test_table_is_dormant_while_auto_reconcile_is_off(self):
        """Rows left behind by someone who disabled auto reconciliation must
        not keep steering matching. The field is only shown while the toggle
        is on, so it should only apply while the toggle is on."""
        doc = self._register()
        rows = [frappe._dict(target_doctype="Customer", match_field="tax_id")]

        with (
            patch(
                "frappe.db.get_value",
                return_value=frappe._dict(name="SHORTCODE", auto_reconcile_c2b=0),
            ),
            patch("frappe.get_all", return_value=rows) as mock_get_all,
        ):
            order = doc._reconciliation_order()

        self.assertEqual(order, list(DEFAULT_RECONCILIATION_ORDER))
        self.assertFalse(mock_get_all.called, "configured rows were read anyway")

    def test_fallback_issues_exactly_the_original_queries(self):
        """Characterisation: an unconfigured install must query as it always did.

        Pinned against the pre-change implementation, which walked a hardcoded
        list of (doctype, customer_field, extra_filters) matching on name.
        """
        doc = self._register(billrefnumber="ACC-001")
        original = [
            ("Sales Invoice", {"name": "ACC-001", "docstatus": 1}, "customer"),
            ("Sales Order", {"name": "ACC-001", "docstatus": 1}, "customer"),
            (
                "Quotation",
                {"name": "ACC-001", "docstatus": 1, "quotation_to": "Customer"},
                "party_name",
            ),
            ("Customer", {"name": "ACC-001"}, "name"),
        ]

        with (
            self._metas(),
            patch.object(
                type(doc),
                "_reconciliation_order",
                lambda _self: list(DEFAULT_RECONCILIATION_ORDER),
            ),
            patch("frappe.db.get_value", return_value=None) as mock_get_value,
        ):
            doc._find_customer_from_billref("ACC-001")

        issued = [
            (call.args[0], call.args[1], call.args[2])
            for call in mock_get_value.call_args_list
        ]
        self.assertEqual(issued, original)

    def test_configured_rows_replace_the_default_order(self):
        doc = self._register()
        rows = [
            frappe._dict(target_doctype="Customer", match_field="tax_id"),
            frappe._dict(target_doctype="Sales Invoice", match_field="name"),
        ]

        with (
            patch(
                "frappe.db.get_value",
                return_value=frappe._dict(name="SHORTCODE", auto_reconcile_c2b=1),
            ),
            patch("frappe.get_all", return_value=rows),
        ):
            self.assertEqual(
                doc._reconciliation_order(),
                [("Customer", "tax_id"), ("Sales Invoice", "name")],
            )

    def test_a_row_without_a_match_field_falls_back_to_name(self):
        doc = self._register()
        rows = [frappe._dict(target_doctype="Sales Order", match_field=None)]

        with (
            patch(
                "frappe.db.get_value",
                return_value=frappe._dict(name="SHORTCODE", auto_reconcile_c2b=1),
            ),
            patch("frappe.get_all", return_value=rows),
        ):
            self.assertEqual(doc._reconciliation_order(), [("Sales Order", "name")])

    def test_configured_match_field_is_used_in_the_lookup(self):
        """The whole point: a non-name match_field must reach the query."""
        doc = self._register(billrefnumber="P051234567X")

        with (
            self._metas(),
            patch.object(
                type(doc),
                "_reconciliation_order",
                lambda _self: [("Customer", "tax_id")],
            ),
            patch(
                "frappe.db.get_value", side_effect=self._lookups(["CUST-0001"])
            ) as mock_get_value,
        ):
            doc._find_customer_from_billref("P051234567X")

        self.assertEqual(doc.customer, "CUST-0001")
        filters = mock_get_value.call_args_list[0].args[1]
        self.assertEqual(filters.get("tax_id"), "P051234567X")
        self.assertNotIn("name", filters)

    def test_priority_stops_at_the_first_hit(self):
        doc = self._register()
        order = [("Sales Invoice", "name"), ("Customer", "name")]

        with (
            self._metas(),
            patch.object(type(doc), "_reconciliation_order", lambda _self: order),
            patch(
                "frappe.db.get_value",
                side_effect=self._lookups(["CUST-FROM-INVOICE", "CUST-FROM-CUSTOMER"]),
            ) as mock_get_value,
        ):
            doc._find_customer_from_billref("ACC-001")

        self.assertEqual(doc.customer, "CUST-FROM-INVOICE")
        matched = [
            c for c in mock_get_value.call_args_list if isinstance(c.args[1], dict)
        ]
        self.assertEqual(len(matched), 1)

    def test_a_row_pointing_at_a_missing_doctype_is_skipped(self):
        doc = self._register()
        order = [("No Such Doctype", "name"), ("Customer", "name")]

        with (
            self._metas(),
            patch.object(type(doc), "_reconciliation_order", lambda _self: order),
            patch("frappe.db.get_value", side_effect=self._lookups(["CUST-0001"])),
        ):
            doc._find_customer_from_billref("ACC-001")

        self.assertEqual(doc.customer, "CUST-0001")

    def test_a_disabled_customer_is_never_matched(self):
        """The MARY case: a payer typed the name of a deactivated customer.

        `Customer` is only reachable here through the default order or a
        configured row, but either way a disabled one must not be attributed
        the money - the Payment Entry would refuse it at submit anyway.
        """
        doc = self._register(billrefnumber="MARY")

        with (
            self._metas(),
            patch.object(
                type(doc), "_reconciliation_order", lambda _self: [("Customer", "name")]
            ),
            patch(
                "frappe.db.get_value",
                side_effect=self._lookups(["MARY"], disabled={"MARY"}),
            ),
        ):
            doc._find_customer_from_billref("MARY")

        self.assertFalse(
            doc.customer, "a disabled customer was matched from the bill reference"
        )

    def test_a_disabled_match_does_not_end_the_walk(self):
        """Skipping a disabled customer must not skip the rows behind it."""
        doc = self._register(billrefnumber="MARY")
        order = [("Customer", "name"), ("Sales Invoice", "name")]

        with (
            self._metas(),
            patch.object(type(doc), "_reconciliation_order", lambda _self: order),
            patch(
                "frappe.db.get_value",
                side_effect=self._lookups(["MARY", "CUST-ACTIVE"], disabled={"MARY"}),
            ),
        ):
            doc._find_customer_from_billref("MARY")

        self.assertEqual(doc.customer, "CUST-ACTIVE")

    def test_nothing_matched_leaves_the_customer_unset(self):
        """An unmatched payment has to sit as a draft for someone to assign.

        before_submit throws "Customer is required" on an empty customer, which
        is what keeps the record in the queue rather than guessing a payer.
        """
        doc = self._register(billrefnumber="MARY")

        with (
            self._metas(),
            patch.object(
                type(doc),
                "_reconciliation_order",
                lambda _self: list(DEFAULT_RECONCILIATION_ORDER),
            ),
            patch("frappe.db.get_value", side_effect=self._lookups([])),
        ):
            doc._find_customer_from_billref("MARY")

        self.assertFalse(doc.customer)

    def test_the_configured_order_is_read_once_per_document(self):
        """It is consulted on insert, before submit and on submit."""
        doc = self._register()
        rows = [frappe._dict(target_doctype="Sales Invoice", match_field="name")]

        with (
            patch(
                "frappe.db.get_value",
                return_value=frappe._dict(name="SHORTCODE", auto_reconcile_c2b=1),
            ),
            patch("frappe.get_all", return_value=rows) as mock_get_all,
        ):
            first = doc._reconciliation_order()
            second = doc._reconciliation_order()

        self.assertEqual(first, second)
        self.assertEqual(mock_get_all.call_count, 1)

    def test_matching_refs_follow_the_configured_order(self):
        """Sales Order first means an order wins even when an invoice matches."""
        doc = self._register(customer="CUST-0001")
        order = [("Sales Order", "name"), ("Sales Invoice", "name")]

        with (
            self._metas(),
            patch.object(type(doc), "_reconciliation_order", lambda _self: order),
            patch("frappe.get_value", return_value="SO-0001"),
        ):
            invoice, sales_order = doc._get_matching_refs()

        self.assertIsNone(invoice)
        self.assertEqual(sales_order, "SO-0001")

    def test_matching_refs_skip_doctypes_a_payment_cannot_settle(self):
        """A Customer row resolves the payer but cannot be reconciled against."""
        doc = self._register(customer="CUST-0001")
        order = [("Customer", "name")]

        with (
            self._metas(),
            patch.object(type(doc), "_reconciliation_order", lambda _self: order),
        ):
            self.assertEqual(doc._get_matching_refs(), (None, None))
