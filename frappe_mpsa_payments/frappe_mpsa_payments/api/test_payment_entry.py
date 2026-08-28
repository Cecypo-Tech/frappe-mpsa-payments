import frappe
from frappe.tests.utils import FrappeTestCase

from .payment_entry import (
    get_available_pos_profiles,
    get_outstanding_invoices,
    get_unallocated_payments,
    set_paid_amount_and_received_amount,
)

test_dependencies = ["Company", "Customer"]

#: ERPNext's own test company, so the party account these queries need exists.
COMPANY = "Wind Power LLC"
CUSTOMER = "_Test Mpesa Customer"


class TestPaymentFunctions(FrappeTestCase):
    """These called into ERPNext with a company and customer that never
    existed, so the queries returned an empty list without ever running -
    and get_outstanding_invoices, once it started resolving a party account,
    failed outright on the missing record."""

    def setUp(self):
        frappe.set_user("Administrator")

        if not frappe.db.exists("Customer", CUSTOMER):
            customer = frappe.new_doc("Customer")
            customer.customer_name = CUSTOMER
            customer.flags.name_set = True
            customer.name = CUSTOMER
            customer.insert(ignore_permissions=True)

    def test_get_outstanding_invoices(self):
        invoices = get_outstanding_invoices(COMPANY, CUSTOMER)

        self.assertIsInstance(invoices, list)

    def test_get_unallocated_payments(self):
        unallocated_payments = get_unallocated_payments(
            CUSTOMER, COMPANY, "USD", "Cash"
        )

        self.assertIsInstance(unallocated_payments, list)

    def test_get_available_pos_profiles(self):
        pos_profiles = get_available_pos_profiles(COMPANY, "USD")

        self.assertIsInstance(pos_profiles, list)

    def test_set_paid_amount_and_received_amount(self):
        paid_amount, received_amount = set_paid_amount_and_received_amount(
            "KES",
            {"account_currency": "KES", "bank_currency": "KES", "conversion_rate": 1.0},
            100.00,
            "Receive",
            None,
            1.0,
        )

        self.assertEqual(paid_amount, 100.00)
        self.assertEqual(received_amount, 100.00)

    def test_a_payment_in_another_currency_is_converted(self):
        """The bank amount decides what was received when the currencies differ."""
        paid_amount, received_amount = set_paid_amount_and_received_amount(
            "USD",
            {"account_currency": "KES", "bank_currency": "KES", "conversion_rate": 1.0},
            100.00,
            "Receive",
            None,
            130.0,
        )

        self.assertEqual(paid_amount, 100.00)
        self.assertEqual(received_amount, 13000.0)
