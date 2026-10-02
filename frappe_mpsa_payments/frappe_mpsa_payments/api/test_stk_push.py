# Copyright (c) 2026, Cecypo.Tech and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_mpsa_payments.frappe_mpsa_payments.api.stk_push import (
    create_stk_push_request,
)

GATEWAY = "Mpesa-_Test STK Reuse"
MODULE = "frappe_mpsa_payments.frappe_mpsa_payments.api.stk_push"
REQUEST_CLASS = (
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_express_request"
    ".mpesa_express_request.MpesaExpressRequest"
)
PHONE = "254700000123"
REFERENCE = "_TEST-STK-REUSE-1"


class TestStkPushReuse(FrappeTestCase):
    def setUp(self):
        self.addCleanup(frappe.db.rollback)
        # A push already waiting on the customer for this sale.
        frappe.get_doc(
            {
                "doctype": "Mpesa Express Request",
                "name": "_Test STK Pending",
                "status": "In Progress",
                "account_reference": REFERENCE,
                "phone_number": PHONE,
                "payment_gateway": GATEWAY,
                "currency": "KES",
                "base_amount": 560,
                "amount": 560,
            }
        ).db_insert()

    def _push(self, amount):
        # No Safaricom call: a new request is built but never sent.
        with (
            patch(f"{MODULE}.get_payment_gateway_from_mop", return_value=GATEWAY),
            patch(f"{REQUEST_CLASS}.insert"),
            patch(f"{REQUEST_CLASS}.submit"),
        ):
            return create_stk_push_request(
                phone_number=PHONE,
                amount=amount,
                mode_of_payment="_Test",
                company="_Test",
                account_reference=REFERENCE,
                prevent_duplicates=1,
            )

    def test_a_pending_push_for_the_same_amount_is_reused(self):
        result = self._push(560)

        self.assertTrue(result["duplicate_prevented"])
        self.assertEqual(result["request_name"], "_Test STK Pending")

    def test_a_changed_amount_sends_a_new_push(self):
        """A discount after the first push must not leave the customer asked for the old amount."""
        result = self._push(4)

        self.assertFalse(result["duplicate_prevented"])
        self.assertEqual(result["amount"], 4)
