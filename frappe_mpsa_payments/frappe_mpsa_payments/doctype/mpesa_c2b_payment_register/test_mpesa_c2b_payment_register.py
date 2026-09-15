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

    # -- allocation ----------------------------------------------------------

    def test_allocation_is_capped_at_what_the_invoice_still_owes(self):
        """A payment larger than the balance must not be allocated in full.

        Payment Entry throws on an over-allocation, and that throw happens
        while this record is being submitted - so the whole payment, not just
        the surplus, would fail to reach the books.
        """
        doc = self._register(transamount=15120)

        with patch("frappe.db.get_value", return_value=2000) as mock_get_value:
            refs = doc._allocation_for("SINV-0001", None)

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["allocated_amount"], 2000)
        self.assertEqual(refs[0]["reference_doctype"], "Sales Invoice")
        self.assertEqual(mock_get_value.call_args.args[2], "outstanding_amount")

    def test_the_whole_payment_is_allocated_when_it_fits(self):
        doc = self._register(transamount=1500)

        with patch("frappe.db.get_value", return_value=2000):
            refs = doc._allocation_for("SINV-0001", None)

        self.assertEqual(refs[0]["allocated_amount"], 1500)

    def test_an_order_owes_what_has_not_been_advanced_against_it(self):
        doc = self._register(transamount=15120)
        order = frappe._dict(rounded_total=0, grand_total=10000, advance_paid=4000)

        with patch("frappe.db.get_value", return_value=order):
            refs = doc._allocation_for(None, "SAL-ORD-0001")

        self.assertEqual(refs[0]["reference_doctype"], "Sales Order")
        self.assertEqual(refs[0]["allocated_amount"], 6000)

    def test_a_settled_order_takes_no_allocation(self):
        """Paid in full by an earlier payment: there is nothing owing."""
        doc = self._register(transamount=15120)
        order = frappe._dict(rounded_total=0, grand_total=10000, advance_paid=10000)

        with patch("frappe.db.get_value", return_value=order):
            self.assertEqual(doc._allocation_for(None, "SAL-ORD-0001"), [])

    def test_a_settled_invoice_takes_no_allocation(self):
        doc = self._register(transamount=15120)

        with patch("frappe.db.get_value", return_value=0):
            self.assertEqual(doc._allocation_for("SINV-0001", None), [])

    def test_nothing_matched_allocates_nothing(self):
        """The payment still becomes a Payment Entry, fully unallocated."""
        doc = self._register(transamount=15120)

        self.assertEqual(doc._allocation_for(None, None), [])

    def test_a_reference_that_vanished_takes_no_allocation(self):
        doc = self._register(transamount=15120)

        with patch("frappe.db.get_value", return_value=None):
            self.assertEqual(doc._allocation_for(None, "SAL-ORD-GONE"), [])

    def test_an_invoice_wins_over_an_order(self):
        doc = self._register(transamount=1000)

        with patch("frappe.db.get_value", return_value=5000):
            refs = doc._allocation_for("SINV-0001", "SAL-ORD-0001")

        self.assertEqual(refs[0]["reference_name"], "SINV-0001")

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

    # -- auto-raised invoice -------------------------------------------------

    def _invoice_that_fails_on_submit(self):
        """A stand-in for the invoice make_sales_invoice returns.

        It writes to the database on insert and again on submit, then fails
        the way the real one did for INV-05124: ERPNext marks the order billed
        and writes the stock ledger before it posts the GL, so an insufficient
        stock error arrives with those rows already written.
        """
        marker = frappe.generate_hash(length=12)
        failure = f"2.0 units needed to complete this transaction {marker}"

        class InvoiceThatFailsOnSubmit:
            allocate_advances_automatically = 0

            def insert(self, ignore_permissions=False):
                frappe.get_doc(
                    {"doctype": "ToDo", "description": f"inserted {marker}"}
                ).insert(ignore_permissions=True)
                return self

            def submit(self):
                frappe.get_doc(
                    {"doctype": "ToDo", "description": f"submitted {marker}"}
                ).insert(ignore_permissions=True)
                from erpnext.stock.stock_ledger import NegativeStockError

                raise NegativeStockError(failure)

        return InvoiceThatFailsOnSubmit(), marker, failure

    def test_a_failed_invoice_submit_leaves_nothing_behind(self):
        """INV-05124: a half-submitted invoice was committed with the payment.

        The failure is swallowed so the payment still reaches the books, which
        means whatever the submit wrote before it failed must be undone here.
        """
        doc = self._register()
        invoice, marker, _failure = self._invoice_that_fails_on_submit()
        # Stands in for the register and its Payment Entry, written earlier in
        # the same transaction. Only the invoice's writes may be undone: a full
        # rollback would lose the payment itself.
        frappe.get_doc({"doctype": "ToDo", "description": f"before {marker}"}).insert(
            ignore_permissions=True
        )

        with patch(
            "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
            return_value=invoice,
        ):
            result = doc._create_sales_invoice_from_order("SAL-ORD-0001")

        self.assertIsNone(result)
        remaining = frappe.get_all(
            "ToDo",
            filters={"description": ["like", f"%{marker}"]},
            pluck="description",
        )
        self.assertEqual(
            remaining,
            [f"before {marker}"],
            "the rollback kept the failed submit's writes or took earlier ones",
        )

    def test_a_failed_invoice_submit_is_still_logged(self):
        """Rolling back the submit must still leave a record of why it failed.

        The rollback hides the failure from the books, so the Error Log is the
        only place anyone can find out the order was never invoiced.
        """
        doc = self._register()
        invoice, _marker, failure = self._invoice_that_fails_on_submit()

        with patch(
            "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
            return_value=invoice,
        ):
            doc._create_sales_invoice_from_order("SAL-ORD-0001")

        self.assertTrue(
            frappe.db.exists("Error Log", {"error": ["like", f"%{failure}%"]}),
            "the failure never reached the Error Log",
        )

    def test_a_successful_invoice_is_kept(self):
        """The savepoint must only undo a failure, never a submitted invoice."""
        doc = self._register()
        marker = frappe.generate_hash(length=12)

        class InvoiceThatSubmits:
            allocate_advances_automatically = 0
            name = f"SINV-{marker}"

            def insert(self, ignore_permissions=False):
                frappe.get_doc(
                    {"doctype": "ToDo", "description": f"inserted {marker}"}
                ).insert(ignore_permissions=True)
                return self

            def submit(self):
                frappe.get_doc(
                    {"doctype": "ToDo", "description": f"submitted {marker}"}
                ).insert(ignore_permissions=True)

        with patch(
            "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
            return_value=InvoiceThatSubmits(),
        ):
            result = doc._create_sales_invoice_from_order("SAL-ORD-0001")

        self.assertEqual(result, f"SINV-{marker}")
        kept = frappe.get_all(
            "ToDo",
            filters={"description": ["like", f"%{marker}"]},
            pluck="description",
        )
        self.assertCountEqual(kept, [f"inserted {marker}", f"submitted {marker}"])

    # -- auto-submit on insert -----------------------------------------------

    def _auto_reconciling_register(self):
        """A register whose insert auto-submits, with only the Payment Entry faked.

        The settings row is written straight to the table so no Mpesa Settings
        validation runs, and the shortcode is unique so no cached lookup from
        another test can answer for it.
        """
        shortcode = "T" + frappe.generate_hash(length=8)
        frappe.get_doc(
            {
                "doctype": "Mpesa Settings",
                "name": f"_Test Auto Submit {shortcode}",
                "business_shortcode": shortcode,
                "auto_reconcile_c2b": 1,
                "auto_create_sales_invoice": 0,
            }
        ).db_insert()

        doc = frappe.new_doc("Mpesa C2B Payment Register")
        doc.update(
            {
                "businessshortcode": shortcode,
                "transamount": 1160,
                "company": "_Test Company",
                "customer": "_Test Customer",
                "mode_of_payment": "_Test Mpesa",
                "firstname": "TEST",
            }
        )
        payment_entry = f"PE-{shortcode}"
        fake_entry = patch(
            f"{MODULE}.create_payment_entry",
            return_value=frappe._dict(name=payment_entry),
        )
        return doc, payment_entry, fake_entry

    def test_an_auto_submitted_payment_is_reconciled_once(self):
        """after_insert submits, and insert then ran on_submit a second time.

        Every reconciliation step ran twice per paybill payment: a failing
        auto-raised invoice was attempted and logged twice.
        """
        doc, payment_entry, fake_entry = self._auto_reconciling_register()
        cls = type(doc)

        with (
            fake_entry,
            patch.object(
                cls, "_reconcile_payment", autospec=True, wraps=cls._reconcile_payment
            ) as reconcile,
        ):
            doc.insert(
                ignore_permissions=True, ignore_links=True, ignore_mandatory=True
            )

        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(
            frappe.db.get_value(doc.doctype, doc.name, ["docstatus", "payment_entry"]),
            (1, payment_entry),
        )

    def test_a_guest_callback_still_submits_the_payment(self):
        """Safaricom's callback inserts as Guest, relying on ignore_permissions.

        Whatever submits the register must carry that through, or the payment
        silently stays a draft.
        """
        doc, payment_entry, fake_entry = self._auto_reconciling_register()

        frappe.set_user("Guest")
        try:
            with fake_entry:
                doc.insert(
                    ignore_permissions=True, ignore_links=True, ignore_mandatory=True
                )
        finally:
            frappe.set_user("Administrator")

        self.assertEqual(
            frappe.db.get_value(doc.doctype, doc.name, ["docstatus", "payment_entry"]),
            (1, payment_entry),
        )

    def test_the_inserted_document_shows_it_was_submitted(self):
        """The statement importer reports docstatus and payment_entry off the
        document it inserted, so that copy must reflect the submit."""
        doc, payment_entry, fake_entry = self._auto_reconciling_register()

        with fake_entry:
            doc.insert(
                ignore_permissions=True, ignore_links=True, ignore_mandatory=True
            )

        self.assertEqual(doc.docstatus, 1)
        self.assertEqual(doc.payment_entry, payment_entry)
