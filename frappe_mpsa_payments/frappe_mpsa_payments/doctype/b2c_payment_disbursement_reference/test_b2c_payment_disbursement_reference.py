# Copyright (c) 2024, Navari Limited and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from ....setup.utils import (
    cleanup_test_documents,
    create_mpesa_settings,
    create_test_employee,
    create_test_employee_advance,
    create_test_payment_disbursement,
)
from ....utils.utils import create_payment_gateway
from ..mpesa_settings.mpesa_settings import create_mode_of_payment
from .b2c_payment_disbursement_reference import (
    is_valid_receiver_contact,
    sanitise_phone_number,
)


class TestB2CPaymentDisbursementReference(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")

        self.mpesa_settings = create_mpesa_settings("Payment")
        self.employee = create_test_employee()
        self.employee_advance = create_test_employee_advance(self.employee)
        create_payment_gateway("Mpesa-Payment", "Mpesa Settings", self.mpesa_settings)
        self.mode_of_payment = create_mode_of_payment(
            "Mpesa-Payment", "Phone", "Navari Limited"
        )

    def tearDown(self):
        frappe.db.rollback()

        cleanup_test_documents(self.employee_advance, self.employee)

    def test_validate_fails_if_amount_is_less_than_10(self):
        """Test that the validate method raises a ValidationError if the amount is less than 10"""
        with self.assertRaises(frappe.ValidationError) as context:
            create_test_payment_disbursement(
                self.employee,
                self.employee_advance,
                allocated_amount=5,
                insert=True,
                submit=False,
            )

        self.assertIn(
            "Allocated Amount cannot be less than Kshs. 10", str(context.exception)
        )

    def test_validate_fails_if_amount_is_greater_than_record_amount(self):
        """Test that the validate method raises a ValidationError if the amount is greater than record_amount"""
        with self.assertRaises(frappe.ValidationError) as context:
            create_test_payment_disbursement(
                self.employee,
                self.employee_advance,
                allocated_amount=200,
                insert=True,
                submit=False,
            )

        self.assertIn(
            "Allocated Amount cannot be greater than Outstanding Amount",
            str(context.exception),
        )

    def test_validate_fails_if_partyb_is_invalid(self):
        """Test that the validate method raises a ValidationError if partyb is invalid"""
        with self.assertRaises(frappe.ValidationError) as context:
            create_test_payment_disbursement(
                self.employee,
                self.employee_advance,
                partyb="07123456789",
                insert=True,
                submit=False,
            )

        self.assertIn("Incorrect Receiver's Mobile Number", str(context.exception))

    def test_validate_passes_if_partyb_is_valid(self):
        """Test that the validate method does not raise a ValidationError if partyb is valid"""
        doc = create_test_payment_disbursement(
            self.employee,
            self.employee_advance,
            partyb="0712345678",
            insert=True,
            submit=False,
        )
        try:
            doc.validate()
        except frappe.ValidationError:
            self.fail("validate() raised ValidationError unexpectedly!")

    def test_sanitise_phone_number(self):
        """Test that the sanitise_phone_number function correctly sanitises a phone number"""
        test_cases = [
            ("0712345678", "254712345678"),
            ("+254712345678", "254712345678"),
            ("254712345678", "254712345678"),
            ("0712345678 ", "254712345678"),
            (" 0712345678", "254712345678"),
            ("0712345678 +", "254712345678"),
            ("0712345678  ", "254712345678"),
        ]

        for input_number, expected_output in test_cases:
            self.assertEqual(sanitise_phone_number(input_number), expected_output)

    def test_sanitise_phone_number_invalid(self):
        """Test that the sanitise_phone_number function does not modify an invalid phone number"""
        test_cases = [
            ("+25471234567", "+25471234567"),
            ("07123456789", "07123456789"),
            ("25471234567", "25471234567"),
            ("0712345678a", "0712345678a"),
        ]

        for input_number, expected_output in test_cases:
            self.assertEqual(sanitise_phone_number(input_number), expected_output)

    def test_is_valid_receiver_contact(self):
        """Test that the is_valid_receiver_contact function correctly identifies valid and invalid contacts"""
        valid_contacts = [
            "254712345678",
            "+254712345678",
            "0712345678",
        ]

        invalid_contacts = [
            "07123456789",
            "25471234567",
            "+25471234567",
            "0712345678a",
        ]

        for contact in valid_contacts:
            self.assertTrue(is_valid_receiver_contact(contact))

        for contact in invalid_contacts:
            self.assertFalse(is_valid_receiver_contact(contact))

    def test_is_valid_reciever_contact_for_011_phone_numbers(self):
        """Test that the is_valid_receiver_contact function correctly identifies 011 phone numbers as valid"""
        valid_contacts = [
            "0112345678",
            "+254112345678",
            "254112345678",
        ]

        for contact in valid_contacts:
            self.assertTrue(is_valid_receiver_contact(contact))
