import frappe
from frappe.utils import now_datetime

from ...utils.doctype_names import (
    MPESA_C2B_PAYMENT_REGISTER_DOCTYPE,
    MPESA_EXPRESS_REQUEST_DOCTYPE,
    MPESA_SETTINGS_DOCTYPE,
)
from ...utils.utils import (
    log_and_throw_error,
    update_mpesa_request_status,
)


def balance_query_on_success(response: dict, document_name: str, **kwargs) -> None:
    pass


def transaction_status_on_success(response: dict, document_name: str, **kwargs) -> None:
    try:
        # frappe.set_user("Administrator")

        frappe.flags.ignore_permissions = True

        result_code = response.get("ResultCode")
        status = "Completed" if result_code == "0" else "Failed"

        request_doc = frappe.get_doc(MPESA_EXPRESS_REQUEST_DOCTYPE, document_name)

        update_mpesa_request_status(
            document_name,
            {
                "result_code": result_code,
                "result_desc": response.get("ResultDesc"),
                "merchant_request_id": response.get("MerchantRequestID"),
                "checkout_request_id": response.get("CheckoutRequestID"),
                "response_code": response.get("ResponseCode"),
                "response_description": response.get("ResponseDescription"),
                "status": status,
            },
        )
        request_doc.reconcile_payment()

    except Exception:
        log_and_throw_error("MPESA Transaction Status Update Error", document_name)


def stk_push_on_success(
    response: dict, payload: dict, document_name: str, **kwargs
) -> None:
    try:
        fields = {
            "merchant_request_id": response.get("MerchantRequestID", ""),
            "checkout_request_id": response.get("CheckoutRequestID", ""),
            "response_code": response.get("ResponseCode", ""),
            "response_description": response.get("ResponseDescription", ""),
            "customer_message": response.get("CustomerMessage", ""),
            "amount": payload.get("Amount", 0.0),
            "phone_number": payload.get("PhoneNumber", ""),
            "timestamp": now_datetime(),
            "settings": kwargs.get("settings_name", ""),
        }

        doctype = kwargs.get("doctype", "")

        if doctype == MPESA_EXPRESS_REQUEST_DOCTYPE:
            for key, value in fields.items():
                frappe.db.set_value(
                    MPESA_EXPRESS_REQUEST_DOCTYPE, document_name, key, value
                )
            frappe.logger().info(f"Mpesa Express Request updated for {document_name}")
        else:
            doc = frappe.new_doc(MPESA_EXPRESS_REQUEST_DOCTYPE)
            for key, value in fields.items():
                setattr(doc, key, value)
            doc.insert(ignore_permissions=True)
            frappe.logger().info(
                f"Mpesa Express Request created for {document_name} with ID {doc.name}"
            )

        frappe.db.commit()

        frappe.publish_realtime(
            event="refresh_form",
            doctype=MPESA_EXPRESS_REQUEST_DOCTYPE,
            docname=document_name,
        )

        # frappe.enqueue(
        #     "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.check_transaction_status",
        #     name=document_name,
        #     enqueue_after_commit=True,
        #     timeout=300
        # )

    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"STK Push Success Error for {document_name}"
        )
        raise


def stk_push_on_error(
    response: dict, payload: dict, document_name: str, **kwargs
) -> None:
    try:
        frappe.db.set_value(
            MPESA_EXPRESS_REQUEST_DOCTYPE,
            document_name,
            {
                # "response_code": response.get("errorCode", ""),
                "response_description": response.get("errorMessage", ""),
                "status": "Failed",
            },
        )
        frappe.db.commit()

    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"STK Push Success Error for {document_name}"
        )
        raise


def _flatten_pull_transactions(raw) -> list:
    if isinstance(raw, dict):
        return [raw]
    if not isinstance(raw, list):
        return []
    flat = []
    for item in raw:
        if isinstance(item, list):
            flat.extend(_flatten_pull_transactions(item))
        elif isinstance(item, dict):
            flat.append(item)
    return flat


def pull_transaction_on_success(response: dict, document_name: str, **kwargs) -> None:
    settings = frappe.get_doc(
        MPESA_SETTINGS_DOCTYPE, kwargs.get("settings_name", document_name)
    )
    shortcode = (
        settings.till_number if settings.sandbox else settings.business_shortcode
    )

    # Safaricom's actual response nests one list of transaction dicts inside
    # "Response" (confirmed via live sandbox test) - not the "Transactions.Transaction"
    # shape assumed at plan time.
    transactions = _flatten_pull_transactions(response.get("Response", []))

    created, skipped, failed = 0, 0, 0
    created_records = []

    for txn in transactions:
        try:
            transid = txn.get("transactionId", "")
            if not transid:
                frappe.log_error(
                    title="Mpesa Pull Transaction: Missing TransactionID",
                    message=f"Transaction payload missing transactionId: {txn}",
                )
                skipped += 1
                continue

            if frappe.db.exists(
                MPESA_C2B_PAYMENT_REGISTER_DOCTYPE, {"transid": transid}
            ):
                frappe.log_error(
                    title="Mpesa Pull Transaction: Duplicate Skipped",
                    message=f"transid={transid} already exists in {MPESA_C2B_PAYMENT_REGISTER_DOCTYPE}",
                )
                skipped += 1
                continue

            doc = frappe.new_doc(MPESA_C2B_PAYMENT_REGISTER_DOCTYPE)
            doc.transid = transid
            doc.transtime = txn.get("trxDate", "")
            doc.transamount = float(txn.get("amount") or 0.0)
            doc.msisdn = txn.get("msisdn", "")
            doc.businessshortcode = shortcode
            doc.billrefnumber = txn.get("billreference", "")
            doc.transactiontype = txn.get("transactiontype", "")
            doc.insert(ignore_permissions=True)

            created_records.append({
                "transid": transid,
                "doc": doc.name,
                "amount": doc.transamount,
                "msisdn": doc.msisdn,
            })
            created += 1

        except Exception:
            failed += 1
            frappe.log_error(
                frappe.get_traceback(),
                f"Mpesa Pull Transaction: Failed for transid={txn.get('transactionId', '')}",
            )
            continue

    frappe.db.commit()

    if created_records:
        frappe.log_error(
            title="Mpesa Pull Transaction: Records Created",
            message=frappe.as_json(created_records),
        )

    owner = frappe.session.user
    frappe.publish_realtime(
        event="mpesa_pull_transaction_complete",
        message={
            "status": "success"
            if created > 0 or skipped == len(transactions)
            else "warning",
            "title": "Pull Transaction Complete",
            "message": f"{created} record(s) imported, {skipped} skipped, {failed} failed.",
            "count": created,
        },
        user=owner,
    )


def pull_transaction_on_error(response: dict, document_name: str, **kwargs) -> None:
    frappe.log_error(
        title="Mpesa Pull Transaction: Safaricom Error",
        message=frappe.as_json(response),
    )
    frappe.publish_realtime(
        event="mpesa_pull_transaction_complete",
        message={
            "status": "error",
            "title": "Pull Transaction Failed",
            "message": response.get(
                "errorMessage", "Safaricom rejected the pull request. Check Error Logs."
            ),
            "count": 0,
        },
        user=frappe.session.user,
    )
