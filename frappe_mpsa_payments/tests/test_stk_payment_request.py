# Copyright (c) 2026, Cecypo Tech and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_mpsa_payments.utils.utils import handle_successful_transaction

GATEWAY = "_Test Mpesa Phone"
COMPANY = "_Test Company"
BANK = "_Test Bank - _TC"


def _service_item():
    name = "_Test Mpesa PR Service"
    if not frappe.db.exists("Item", name):
        frappe.get_doc(
            {"doctype": "Item", "item_code": name, "item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 0}
        ).insert(ignore_permissions=True)
    return name


class TestStkPushOnPaymentRequest(IntegrationTestCase):
    """An STK push raised from a Payment Request (powerpack Quick Pay on a Sales Order).

    The push completed and the Payment Request showed Paid, but no Payment Entry
    existed: ERPNext marks a Phone-channel request Paid with nothing outstanding
    when told the payment was authorised, and the entry made after it was then
    refused as more than the request's outstanding amount.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not frappe.db.exists("Payment Gateway", GATEWAY):
            frappe.get_doc({"doctype": "Payment Gateway", "gateway": GATEWAY}).insert(ignore_permissions=True)

    def _payment_request(self, amount):
        so = frappe.get_doc(
            {
                "doctype": "Sales Order",
                "company": COMPANY,
                "customer": frappe.get_all("Customer", filters={"disabled": 0}, pluck="name", limit=1)[0],
                "transaction_date": frappe.utils.nowdate(),
                "delivery_date": frappe.utils.nowdate(),
                "items": [{"item_code": _service_item(), "qty": 1, "rate": amount}],
            }
        ).insert(ignore_permissions=True)
        so.submit()
        account = BANK
        gateway_account = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": GATEWAY, "payment_account": account})
        if not gateway_account:
            gateway_account = (
                frappe.get_doc(
                    {
                        "doctype": "Payment Gateway Account",
                        "payment_gateway": GATEWAY,
                        "payment_account": account,
                        "currency": so.currency,
                    }
                )
                .insert(ignore_permissions=True)
                .name
            )
        pr = frappe.get_doc(
            {
                "doctype": "Payment Request",
                "payment_request_type": "Inward",
                "company": so.company,
                "party_type": "Customer",
                "party": so.customer,
                "reference_doctype": "Sales Order",
                "reference_name": so.name,
                "grand_total": amount,
                "currency": so.currency,
                "payment_gateway_account": gateway_account,
                "payment_gateway": GATEWAY,
                "payment_account": account,
                "payment_channel": "Phone",
                "phone_number": "254700000000",
                "mute_email": 1,
            }
        )
        with patch("erpnext.accounts.doctype.payment_request.payment_request.PaymentRequest.request_phone_payment"):
            pr.insert(ignore_permissions=True)
            pr.submit()
        return pr

    def _completed_push(self, pr):
        transid = f"PRT{frappe.generate_hash(length=7).upper()}"
        frappe.get_doc(
            {
                "doctype": "Mpesa Express Request",
                "name": f"MEXP-TEST-{transid}",
                "status": "Completed",
                "docstatus": 1,
                "transaction_id": transid,
                "reference_doctype": "Payment Request",
                "reference_name": pr.name,
                "amount": pr.grand_total,
                "currency": pr.currency,
                "payment_gateway": GATEWAY,
                "phone_number": "254700000000",
            }
        ).db_insert()
        return frappe.get_doc("Mpesa Express Request", f"MEXP-TEST-{transid}")

    def _entries_for(self, pr):
        return frappe.get_all(
            "Payment Entry Reference",
            filters={"payment_request": pr.name, "docstatus": 1},
            pluck="parent",
        )

    def test_a_completed_push_books_one_payment_entry(self):
        pr = self._payment_request(100)

        handle_successful_transaction(self._completed_push(pr), frappe._dict(auto_create_sales_invoice=0))

        self.assertEqual(len(self._entries_for(pr)), 1)
        pr.reload()
        self.assertEqual(pr.status, "Paid")
        self.assertEqual(pr.outstanding_amount, 0)

    def test_a_repeated_callback_books_nothing_more(self):
        pr = self._payment_request(100)
        push = self._completed_push(pr)
        settings = frappe._dict(auto_create_sales_invoice=0)

        handle_successful_transaction(push, settings)
        handle_successful_transaction(push, settings)

        self.assertEqual(len(self._entries_for(pr)), 1)


class TestPushPhoneNumber(IntegrationTestCase):
    def test_every_format_the_dialog_accepts_reaches_safaricom_as_254(self):
        """Quick Pay says 0727870777 and 254727870777 both work; the second became 254254727870777."""
        from frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings import (
            sanitize_mobile_number,
        )

        for number in ("0727870777", "254727870777", "+254 727 870 777", "727870777"):
            self.assertEqual(sanitize_mobile_number(number), "254727870777", number)
