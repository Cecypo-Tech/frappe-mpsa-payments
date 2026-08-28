from frappe.tests.utils import FrappeTestCase

from .payment_entry import (
    get_available_pos_profiles,
    get_outstanding_invoices,
    get_unallocated_payments,
    set_paid_amount_and_received_amount,
)


class TestPaymentFunctions(FrappeTestCase):
    def test_get_outstanding_invoices(self):
        # Signature is (company, customer, invoice_type=...); the currency and
        # POS profile arguments this used to pass belong to a version of it
        # that no longer exists.
        company = "Test Company Maniac"
        customer = "Test Customer"

        invoices = get_outstanding_invoices(company, customer)

        # Assert the result
        self.assertTrue(isinstance(invoices, list))

    def test_get_unallocated_payments(self):
        customer = "Test Customer"
        company = "Test Company Maniac"
        currency = "KES"
        mode_of_payment = "Cash"

        # Call the function
        unallocated_payments = get_unallocated_payments(
            customer, company, currency, mode_of_payment
        )

        # Assert the result
        self.assertTrue(isinstance(unallocated_payments, list))

    def test_get_available_pos_profiles(self):
        company = "Test Company Maniac"
        currency = "KES"

        pos_profiles = get_available_pos_profiles(company, currency)

        self.assertTrue(isinstance(pos_profiles, list))

    def test_set_paid_amount_and_received_amount(self):
        party_account_currency = "KES"
        bank = {
            "account_currency": "KES",
            "bank_currency": "KES",
            "conversion_rate": 1.0,
        }
        outstanding_amount = 100.00
        payment_type = "Receive"
        bank_amount = None
        conversion_rate = 1.0

        paid_amount, received_amount = set_paid_amount_and_received_amount(
            party_account_currency,
            bank,
            outstanding_amount,
            payment_type,
            bank_amount,
            conversion_rate,
        )

        self.assertEqual(paid_amount, 100.00)
        self.assertEqual(received_amount, 100.00)
