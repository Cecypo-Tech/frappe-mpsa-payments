# Copyright (c) 2025, Navari Limited and contributors
# For license information, please see license.txt

"""Public STK push API.

The entry point for any external system - a POS, a booking site, another
Frappe app - that needs this app to collect a payment over M-Pesa Express.

The flow is: create_stk_push_request() to prompt the payer, poll
get_stk_push_status() until it reports a terminal status, and optionally
link_stk_push_to_document() once the caller has a document to attach the
payment to. Callers routinely prompt before that document exists, which is
why linking is a separate step.
"""

import re

import frappe
from frappe import _
from frappe.utils import add_to_date, flt

from .sales_invoice import get_payment_gateway_from_mop


def _normalize_kenyan_phone(phone_number: str) -> str:
    """Normalize local/international Kenyan numbers to 254XXXXXXXXX."""
    digits = re.sub(r"\D", "", phone_number or "")
    if digits.startswith("254") and len(digits) >= 12:
        return digits[:12]
    if digits.startswith("0") and len(digits) >= 10:
        return "254" + digits[1:10]
    if len(digits) >= 9:
        return "254" + digits[-9:]
    return digits


#: Safaricom drops an unanswered prompt after about a minute and reports it. A request still
#: In Progress well after that lost its callback; it can no longer be paid from the prompt.
PROMPT_LIFETIME_MINUTES = 5


def _same_sale(account_reference, reference_doctype, reference_name) -> dict:
    """Requests for the same sale: by the document it is for when the caller names one (an
    account reference may be shared, like a till code), else by the account reference."""
    if reference_doctype and reference_name:
        return {
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
        }
    return {"account_reference": account_reference}


def _to_bool(value) -> bool:
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    return bool(value)


@frappe.whitelist()
def create_stk_push_request(
    phone_number: str = None,
    amount: float = None,
    mode_of_payment: str = None,
    company: str = None,
    currency: str = "KES",
    account_reference: str = None,
    reference_doctype: str = None,
    reference_name: str = None,
    prevent_duplicates: int | bool = 1,
) -> dict:
    """Prompt a payer for an M-Pesa payment and return a handle to poll.

    `account_reference` is the Pay Bill account number: it is what the payment
    shows against on the payer's statement and what returns as BillRefNumber.
    Send the invoice or order name if the payment should reconcile against one
    automatically. `phone_number` accepts local or international formats.
    """
    if not (phone_number and amount and mode_of_payment and company):
        frappe.throw(
            _(
                "All fields (phone_number, amount, mode_of_payment, company) are required."
            )
        )

    amount = flt(amount)
    if amount <= 0:
        frappe.throw(_("Amount must be greater than zero."))

    payment_gateway = get_payment_gateway_from_mop(mode_of_payment, company)
    if not payment_gateway:
        frappe.throw(
            _(
                "No payment gateway mapping found for Mode of Payment {0} and company {1}."
            ).format(mode_of_payment, company)
        )

    normalized_phone = _normalize_kenyan_phone(phone_number)
    prevent_dup = _to_bool(prevent_duplicates)

    if prevent_dup and (account_reference or (reference_doctype and reference_name)):
        sale = _same_sale(account_reference, reference_doctype, reference_name)
        paid = frappe.db.get_value(
            "Mpesa Express Request",
            {**sale, "docstatus": 1, "status": "Completed"},
            "name",
        )
        if paid:
            # The money is in: another prompt would charge the customer twice.
            frappe.throw(
                _(
                    "M-Pesa request {0} for {1} was already paid. Submit the sale instead of "
                    "asking again."
                ).format(paid, account_reference or reference_name)
            )

        pending = frappe.get_all(
            "Mpesa Express Request",
            filters={
                **sale,
                "docstatus": 1,
                "status": "In Progress",
                "creation": [">", add_to_date(None, minutes=-PROMPT_LIFETIME_MINUTES)],
            },
            fields=[
                "name",
                "status",
                "checkout_request_id",
                "amount",
                "base_amount",
                "currency",
                "phone_number",
                "payment_gateway",
                "transaction_id",
            ],
            order_by="creation desc",
        )
        same_ask = [
            r
            for r in pending
            if r.phone_number == normalized_phone
            and r.payment_gateway == payment_gateway
            and flt(r.base_amount, 2) == flt(amount, 2)
            and (r.currency or "KES") == (currency or "KES")
        ]
        if same_ask:
            doc = same_ask[0]
            return {
                "status": "success",
                "duplicate_prevented": True,
                "request_name": doc.get("name"),
                "request_status": doc.get("status") or "In Progress",
                "checkout_request_id": doc.get("checkout_request_id"),
                "transaction_id": doc.get("transaction_id"),
                "amount": flt(doc.get("amount") or amount),
            }
        if pending:
            # Asking again for a different amount (a discount since), or on another phone,
            # would leave the customer with two live prompts for one sale - and both could be
            # paid. The first one ends within a couple of minutes, paid or not.
            frappe.throw(
                _(
                    "M-Pesa request {0} for {1} is still waiting on the customer ({2} {3}). "
                    "Wait for it to finish before asking again."
                ).format(
                    pending[0].name,
                    account_reference or reference_name,
                    pending[0].currency or "KES",
                    flt(pending[0].base_amount, 2),
                )
            )

    express_request = frappe.get_doc(
        {
            "doctype": "Mpesa Express Request",
            "reference_name": reference_name,
            "reference_doctype": reference_doctype,
            "phone_number": normalized_phone,
            "base_amount": amount,
            "currency": currency,
            "payment_gateway": payment_gateway,
            "settings": payment_gateway[6:],
            "account_reference": account_reference,
            "status": "In Progress",
        }
    )
    express_request.insert(ignore_permissions=True)
    express_request.submit()

    return {
        "status": "success",
        "duplicate_prevented": False,
        "request_name": express_request.name,
        "request_status": express_request.status,
        "checkout_request_id": express_request.checkout_request_id,
        "transaction_id": express_request.transaction_id,
        "amount": flt(express_request.amount or amount),
        "phone_number": express_request.phone_number,
    }


@frappe.whitelist()
def get_stk_push_status(request_name: str) -> dict:
    """Report where a request has got to. Poll this until status is terminal.

    Only `Completed` with a non-null `transaction_id` means the money arrived;
    that id is the M-Pesa receipt.
    """
    if not request_name:
        frappe.throw(_("request_name is required."))

    doc = frappe.get_doc("Mpesa Express Request", request_name)
    reference = None
    if doc.reference_doctype and doc.reference_name:
        reference = {
            "reference_doctype": doc.reference_doctype,
            "reference_name": doc.reference_name,
        }

    return {
        "request_name": doc.name,
        "status": doc.status,
        "result_code": doc.result_code,
        "result_desc": doc.result_desc,
        "transaction_id": doc.transaction_id,
        "checkout_request_id": doc.checkout_request_id,
        "amount": flt(doc.amount or 0),
        "phone_number": doc.phone_number,
        "is_reconciled": doc.is_reconciled,
        "reference": reference,
    }


@frappe.whitelist()
def link_stk_push_to_document(
    request_name: str,
    reference_name: str,
    reference_doctype: str = "Sales Invoice",
) -> dict:
    """Attach an already-initiated request to the document it paid for."""
    if not request_name or not reference_name:
        frappe.throw(_("request_name and reference_name are required."))

    request_doc = frappe.get_doc("Mpesa Express Request", request_name)
    request_doc.db_set("reference_doctype", reference_doctype)
    request_doc.db_set("reference_name", reference_name)

    return {
        "success": True,
        "request_name": request_doc.name,
        "reference_doctype": reference_doctype,
        "reference_name": reference_name,
        "status": request_doc.status,
        "transaction_id": request_doc.transaction_id,
    }
