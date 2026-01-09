# Copyright (c) 2024, Navari Limited and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class B2CPaymentDisbursementReference(Document):
    def validate(self) -> None:
        """Validation Hook"""
        parent_doc = getattr(self, "parent_doc", None)
        payment_type = parent_doc.payment_type

        if not self.party:
            frappe.throw(
                f"Row #{self.idx}: Party is mandatory",
                frappe.ValidationError,
                title="Validation Error",
            )

        if self.allocated_amount is not None and flt(self.allocated_amount) < 10:
            frappe.throw(
                "Allocated Amount cannot be less than Kshs. 10",
                frappe.ValidationError,
                title="Validation Error",
            )

        if self.outstanding_amount and (
            self.allocated_amount > self.outstanding_amount
            or not self.outstanding_amount
        ):
            frappe.throw(
                "Allocated Amount cannot be greater than Outstanding Amount",
                frappe.ValidationError,
                title="Validation Error",
            )

        if payment_type != "Stanbic PesaLink" and self.partyb:
            mobile_no = sanitise_phone_number(self.partyb)

            if not is_valid_receiver_contact(mobile_no):
                frappe.throw(
                    f"Row #{self.idx}: Incorrect Receiver's Mobile Number: {self.partyb}",
                    frappe.ValidationError,
                    title="Incorrect Contact",
                )

            self.partyb = mobile_no

        if payment_type == "Stanbic PesaLink":
            if not self.bank_code:
                frappe.throw(
                    f"Row #{self.idx}: Bank Code is Required",
                    frappe.ValidationError,
                    title="Validation Error",
                )


def sanitise_phone_number(phone_number: str) -> str:
    """
    Sanitises a given phone_number string to Kenyan international format (254XXXXXXXXX).

    Accepts:
    - Local format: 0XXXXXXXXX (0 followed by 9 digits)
    - International format: 254XXXXXXXXX (254 followed by 9 digits)
    - International format with +: +254XXXXXXXXX

    Returns the sanitised number in format 254XXXXXXXXX, or an original number if invalid.
    """
    original = phone_number

    phone_number = (
        phone_number.replace("+", "")
        .replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
    )

    local_regex = re.compile(r"^0\d{9}$")
    if local_regex.match(phone_number):
        return "254" + phone_number[1:]

    international_regex = re.compile(r"^254\d{9}$")
    if international_regex.match(phone_number):
        return phone_number

    return original


def is_valid_receiver_contact(receiver: str) -> bool:
    """Validates the Receiver's mobile number"""
    receiver = receiver.replace("+", "").replace(" ", "").strip()

    if receiver.startswith("0") and len(receiver) == 10:
        receiver = "254" + receiver[1:]

    pattern1 = re.compile(r"^2547\d{8}$")
    pattern2 = re.compile(r"(25410|25411)\d{7}$")

    if receiver.startswith("2547"):
        return bool(pattern1.match(receiver))
    elif receiver.startswith("25410") or receiver.startswith("25411"):
        return bool(pattern2.match(receiver))

    return False
