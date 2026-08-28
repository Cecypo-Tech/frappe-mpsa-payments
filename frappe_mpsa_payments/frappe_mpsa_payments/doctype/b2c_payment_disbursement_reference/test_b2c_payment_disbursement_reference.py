# Copyright (c) 2024, Navari Limited and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from .b2c_payment_disbursement_reference import (
    is_valid_receiver_contact,
    sanitise_phone_number,
)


class TestB2CPaymentDisbursementReference(FrappeTestCase):
    """These rows used to be MPesa B2C Employee Payment Item.

    The doctype was renamed and its fields with it - amount became
    allocated_amount, record_amount became outstanding_amount - so the tests
    were addressing a doctype that no longer exists.
    """

    def _row(self, payment_type="Mpesa Disbursement", **fields):
        """A reference row attached to a parent, the way validate() sees one.

        validate() reads the payment type off the parent, which Frappe wires
        up as a *weak* reference when the row is appended - so the parent has
        to outlive this call, or row.parent_doc comes back None and every
        validation raises AttributeError instead of what it is testing.
        """
        self.parent = frappe.new_doc("B2C Payment Disbursement")
        self.parent.payment_type = payment_type
        row = self.parent.append("references", {})
        row.update(fields)
        return row

    # -- validate ------------------------------------------------------------

    def test_a_row_without_a_party_is_rejected(self):
        row = self._row(allocated_amount=100)

        with self.assertRaises(frappe.ValidationError) as context:
            row.validate()

        self.assertIn("Party is mandatory", str(context.exception))

    def test_an_allocation_under_ten_shillings_is_rejected(self):
        """M-Pesa will not move less than KES 10."""
        row = self._row(party="EMP-0001", allocated_amount=5)

        with self.assertRaises(frappe.ValidationError) as context:
            row.validate()

        self.assertIn("cannot be less than Kshs. 10", str(context.exception))

    def test_an_allocation_over_the_outstanding_amount_is_rejected(self):
        row = self._row(party="EMP-0001", allocated_amount=200, outstanding_amount=100)

        with self.assertRaises(frappe.ValidationError) as context:
            row.validate()

        self.assertIn(
            "cannot be greater than Outstanding Amount", str(context.exception)
        )

    def test_an_unreachable_receiver_is_rejected(self):
        row = self._row(
            party="EMP-0001",
            allocated_amount=100,
            outstanding_amount=100,
            partyb="some_invalid_number",
        )

        with self.assertRaises(frappe.ValidationError) as context:
            row.validate()

        self.assertIn("Incorrect Receiver's Mobile Number", str(context.exception))

    def test_a_valid_receiver_is_normalised_in_place(self):
        """The row keeps the number in the form Safaricom expects."""
        row = self._row(
            party="EMP-0001",
            allocated_amount=100,
            outstanding_amount=100,
            partyb="0712345678",
        )

        row.validate()

        self.assertEqual(row.partyb, "254712345678")

    def test_pesalink_needs_a_bank_code_instead_of_a_phone_number(self):
        row = self._row(
            payment_type="Stanbic PesaLink",
            party="EMP-0001",
            allocated_amount=100,
            outstanding_amount=100,
            partyb="not-a-phone-number",
        )

        with self.assertRaises(frappe.ValidationError) as context:
            row.validate()

        self.assertIn("Bank Code is Required", str(context.exception))

    def test_pesalink_passes_once_it_has_a_bank_code(self):
        row = self._row(
            payment_type="Stanbic PesaLink",
            party="EMP-0001",
            allocated_amount=100,
            outstanding_amount=100,
            bank_code="031",
        )

        row.validate()

        self.assertEqual(row.bank_code, "031")

    # -- sanitise_phone_number -----------------------------------------------

    def test_a_local_number_becomes_an_international_one(self):
        for entered, expected in [
            ("0712345678", "254712345678"),
            ("0112345678", "254112345678"),
            ("0712345678 ", "254712345678"),
            (" 0712345678", "254712345678"),
            ("0712345678 +", "254712345678"),
        ]:
            with self.subTest(entered=entered):
                self.assertEqual(sanitise_phone_number(entered), expected)

    def test_a_number_already_in_international_form_only_loses_its_plus(self):
        for entered, expected in [
            ("+254712345678", "254712345678"),
            ("254712345678", "254712345678"),
        ]:
            with self.subTest(entered=entered):
                self.assertEqual(sanitise_phone_number(entered), expected)

    def test_anything_that_is_not_a_local_number_is_left_alone(self):
        """Stripped of '+' and spaces, but otherwise untouched - it is then
        is_valid_receiver_contact's job to reject it."""
        for entered, expected in [
            ("07123456789", "07123456789"),
            ("25471234567", "25471234567"),
            ("0712345678a", "0712345678a"),
        ]:
            with self.subTest(entered=entered):
                self.assertEqual(sanitise_phone_number(entered), expected)

    # -- is_valid_receiver_contact -------------------------------------------

    def test_a_safaricom_number_is_reachable(self):
        for contact in ["254712345678", "+254712345678", "254112345678"]:
            with self.subTest(contact=contact):
                self.assertTrue(is_valid_receiver_contact(contact))

    def test_the_011_range_is_reachable(self):
        """011 numbers are Safaricom too, and were rejected once."""
        for contact in ["254112345678", "+254112345678", "254102345678"]:
            with self.subTest(contact=contact):
                self.assertTrue(is_valid_receiver_contact(contact))

    def test_a_number_of_the_wrong_length_or_shape_is_not_reachable(self):
        for contact in ["25471234567", "2547123456789", "254712345a78"]:
            with self.subTest(contact=contact):
                self.assertFalse(is_valid_receiver_contact(contact))

    def test_a_local_number_is_not_reachable_until_it_is_sanitised(self):
        """The two functions are used as a pair, in that order."""
        self.assertFalse(is_valid_receiver_contact("0712345678"))
        self.assertTrue(is_valid_receiver_contact(sanitise_phone_number("0712345678")))
