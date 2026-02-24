import frappe
from frappe import _
from frappe.utils import flt, nowdate


@frappe.whitelist()
def pos_quick_pay_mpesa_process(**kwargs):
    """
    Central API endpoint for Mpesa Quick Pay across all invoice types.

    Required in every call:
        action   – check_mpesa_available | get_mpesa_payments | process_mpesa |
                   get_customer_phone | create_payment_request | revert_mpesa |
                   update_mpesa_after_submit
        doctype  – "POS Invoice" or "Sales Invoice"

    Additional params depend on the action.
    """
    action = frappe.form_dict.get("action")

    if action == "check_mpesa_available":
        return _check_mpesa_available()

    elif action == "get_mpesa_payments":
        return _get_mpesa_payments()

    elif action == "process_mpesa":
        return _process_mpesa()

    elif action == "get_customer_phone":
        return _get_customer_phone()

    elif action == "create_payment_request":
        return _create_payment_request()

    elif action == "revert_mpesa":
        return _revert_mpesa()

    elif action == "update_mpesa_after_submit":
        return _update_mpesa_after_submit()

    else:
        frappe.throw(_("Invalid action"))


@frappe.whitelist()
def check_mpesa_configuration():
    """Verify overall Mpesa configuration completeness (used in setup/debug)."""
    result = {"success": True, "issues": []}

    mops = frappe.get_all(
        "Mode of Payment", filters={"type": "Phone", "enabled": 1}, fields=["name"]
    )
    if not mops:
        result["success"] = False
        result["issues"].append("No enabled Phone-type Mode of Payment found")
    else:
        result["phone_mops"] = [m["name"] for m in mops]
        for mop in mops:
            if not frappe.get_all(
                "Mode of Payment Account", filters={"parent": mop["name"]}
            ):
                result["success"] = False
                result["issues"].append(
                    f"Mode of Payment '{mop['name']}' has no company accounts configured"
                )

    settings = frappe.get_all(
        "Mpesa Settings", fields=["name", "company", "business_shortcode"]
    )
    if not settings:
        result["success"] = False
        result["issues"].append("No Mpesa Settings documents found")
    else:
        result["mpesa_settings"] = settings

    result["payment_gateways"] = frappe.get_all(
        "Payment Gateway Account",
        filters={"payment_gateway": ["like", "%Mpesa%"]},
        fields=["name", "payment_gateway"],
    )
    return result


def get_phone_mop_for_company(company):
    """Return the first enabled Phone-type Mode of Payment that has an account for the company."""
    mops = frappe.get_all(
        "Mode of Payment", filters={"type": "Phone", "enabled": 1}, fields=["name"]
    )

    for mop in mops:
        if frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": mop["name"], "company": company},
            "default_account",
        ):
            return mop["name"]
    return None


def get_mpesa_shortcode_for_company(company):
    """Return the first business_shortcode string from Mpesa Settings for this company."""
    rows = frappe.get_all(
        "Mpesa Settings",
        filters={"company": company},
        fields=["business_shortcode"],
        limit=1,
    )
    if rows and rows[0].get("business_shortcode"):
        return str(rows[0]["business_shortcode"])
    return None


def get_mop_account_for_company(mop, company):
    """Return the default account for a given Mode of Payment and company."""
    mop_account = frappe.db.get_value(
        "Mode of Payment Account",
        {"parent": mop, "company": company},
        "default_account",
    )
    if not mop_account:
        frappe.throw(
            _(
                "No default account found for Mode of Payment '{0}' and company '{1}'"
            ).format(mop, company)
        )
    return mop_account


def resolve_doctype(doctype):
    """Validate doctype is one we support; throw otherwise."""
    supported = ("POS Invoice", "Sales Invoice")
    if doctype not in supported:
        frappe.throw(
            _("Unsupported doctype '{0}'. Must be one of: {1}").format(
                doctype, ", ".join(supported)
            )
        )
    return doctype


def get_invoice(doctype, invoice_name):
    """Load and return the new invoice document(draft)."""
    invoice = frappe.get_doc(resolve_doctype(doctype), invoice_name)
    if invoice.docstatus != 0:
        frappe.throw(_("Cannot add payments to a submitted or cancelled invoice"))
    return invoice


def _check_mpesa_available():
    """
    Check whether Mpesa can be used for a given company.

    Params: company
    Returns: { available: bool, phone_mop: str or None, phone_mop_account: str or None }
    """
    company = frappe.form_dict.get("company")

    phone_mop = get_phone_mop_for_company(company) if company else None
    shortcode = get_mpesa_shortcode_for_company(company) if company else None
    phone_mop_account = (
        get_mop_account_for_company(phone_mop, company) if phone_mop else None
    )

    available = bool(phone_mop and shortcode and phone_mop_account)

    return {
        "available": available,
        "phone_mop": phone_mop,
        "phone_mop_account": phone_mop_account,
    }


def _get_mpesa_payments():
    """
    Return pending (draft) Mpesa C2B payments for a company.

    Always returns the total count of all draft entries for the shortcode.
    A filtered list is returned only when `search` is >= 3 characters.
    Search covers: full_name, transid, billrefnumber.

    Params: company, search
    Returns: { count: int, payments: list }
    """
    company = frappe.form_dict.get("company")
    search = (frappe.form_dict.get("search") or "").strip()

    if not company:
        return {"count": 0, "payments": []}

    shortcode = get_mpesa_shortcode_for_company(company)
    if not shortcode:
        return {"count": 0, "payments": []}

    base_filters = {"docstatus": 0, "businessshortcode": shortcode}
    total_count = frappe.db.count("Mpesa C2B Payment Register", base_filters)

    payments = []
    if len(search) >= 3:
        rows = frappe.get_all(
            "Mpesa C2B Payment Register",
            filters=base_filters,
            fields=[
                "name",
                "full_name",
                "transamount",
                "transid",
                "msisdn",
                "posting_date",
                "billrefnumber",
                "creation",
            ],
            order_by="creation desc",
            limit_page_length=200,
        )
        sl = search.lower()
        for p in rows:
            full_name = (p.get("full_name") or "").lower()
            transid = (p.get("transid") or "").lower()
            billref = (p.get("billrefnumber") or "").lower()

            if sl in full_name or sl in transid or sl in billref:
                payments.append(p)

    return {"count": total_count, "payments": payments}


def _revert_mpesa():
    """
    Revert a submitted Mpesa C2B Payment Register back to draft and
    remove (or update if merged) the corresponding payment row on the invoice.

    Params:
        mpesa_name   – Mpesa C2B Payment Register name to revert
        invoice_name – the invoice to remove the payment row from
        doctype      – "POS Invoice" | "Sales Invoice"
    """
    mpesa_name = frappe.form_dict.get("mpesa_name")
    invoice_name = frappe.form_dict.get("invoice_name")
    doctype = resolve_doctype(frappe.form_dict.get("doctype"))

    if not mpesa_name:
        frappe.throw(_("mpesa_name is required"))

    if not invoice_name:
        frappe.throw(_("invoice_name is required"))

    result = {"success": True}

    mpesa = frappe.get_doc("Mpesa C2B Payment Register", mpesa_name)

    if mpesa.docstatus == 1:
        mpesa.cancel()

        amended = frappe.copy_doc(mpesa)
        amended.docstatus = 0
        amended.customer = None
        amended.mode_of_payment = None
        amended.submit_payment = 0
        amended.pos_invoice = None
        amended.sales_invoice = None
        amended.amended_from = mpesa.name
        amended.insert(ignore_permissions=True)
        result["new_name"] = amended.name

    elif mpesa.docstatus == 0:
        # Already draft — just clear the fields in case they were set (unlikely since mpesa register is always submitted on process_mpesa)
        frappe.db.set_value(
            "Mpesa C2B Payment Register",
            mpesa_name,
            {
                "customer": None,
                "mode_of_payment": None,
                "submit_payment": 0,
                "pos_invoice": None,
                "sales_invoice": None,
            },
        )

    else:
        frappe.throw(_("Cannot revert a cancelled Mpesa entry"))

    invoice = frappe.get_doc(doctype, invoice_name)

    for row in invoice.payments:
        # only have custom_reference_text for merged payments, otherwise reference_no is used
        ref_text = row.custom_reference_text or row.reference_no or ""
        refs = [r.strip() for r in ref_text.splitlines() if r.strip()]

        if mpesa_name in refs:
            if len(refs) == 1:
                invoice.remove(row)
            else:
                # Merged row — remove just this reference and reduce amount
                mpesa_amt = flt(
                    frappe.db.get_value(
                        "Mpesa C2B Payment Register", mpesa_name, "transamount"
                    )
                )
                row.amount = max(0, flt(row.amount) - mpesa_amt)
                remaining_refs = [r for r in refs if r != mpesa_name]
                if len(remaining_refs) == 1:
                    # If only one reference remains, move it to reference_no for
                    row.reference_no = remaining_refs[0]
                    row.custom_reference_text = ""
                else:
                    row.custom_reference_text = "\n".join(remaining_refs)

    invoice.save(ignore_permissions=True)
    result["invoice_saved"] = True

    return result


def _process_mpesa():
    """
    Link selected Mpesa C2B entries to an draft invoice and add payment rows.

    Params:
        doctype           – "POS Invoice" | "Sales Invoice"
        invoice_name      – document name of the invoice
        customer
        mpesa_payments    – comma-separated Mpesa C2B Payment Register names
        outstanding_amount
        auto_save         – 1/0  save the invoice after adding rows
        auto_submit       – 1/0  submit the invoice if outstanding becomes 0
        merge_payments     – 1/0  if multiple Mpesa entries, merge into a single payment row on the invoice
    """
    doctype = resolve_doctype(frappe.form_dict.get("doctype"))
    invoice_name = frappe.form_dict.get("invoice_name")
    customer = frappe.form_dict.get("customer")
    names_str = frappe.form_dict.get("mpesa_payments") or ""
    auto_save = int(frappe.form_dict.get("auto_save") or 0)
    auto_submit = int(frappe.form_dict.get("auto_submit") or 0)
    merge_payments = int(frappe.form_dict.get("merge_payments") or 0)

    mpesa_names = [n.strip() for n in names_str.split(",") if n.strip()]
    if not mpesa_names:
        frappe.throw(_("No Mpesa payments selected"))

    invoice = get_invoice(doctype, invoice_name)
    phone_mop = get_phone_mop_for_company(invoice.company)
    if not phone_mop:
        frappe.throw(
            _("No Phone-type Mode of Payment configured for {0}").format(
                invoice.company
            )
        )

    shortcode = get_mpesa_shortcode_for_company(invoice.company)
    if not shortcode:
        frappe.throw(_("No Mpesa Settings found for {0}").format(invoice.company))

    mop_account = get_mop_account_for_company(phone_mop, invoice.company)

    payments_added = []
    mpesa_results = []
    doctype_field = "pos_invoice" if doctype == "POS Invoice" else "sales_invoice"

    for mpesa_name in mpesa_names:
        mpesa = frappe.get_doc("Mpesa C2B Payment Register", mpesa_name)

        # fail silently - status checked in get_mpesa_payments:
        if mpesa.docstatus != 0:
            continue
        if str(mpesa.businessshortcode or "") != shortcode:
            continue

        mpesa_amt = flt(mpesa.transamount or 0)
        if mpesa_amt <= 0:
            continue

        # Tag the Mpesa entry and submit it
        mpesa.customer = customer
        mpesa.submit_payment = 0
        mpesa.save(ignore_permissions=True)
        mpesa.submit()

        frappe.db.set_value(
            "Mpesa C2B Payment Register", mpesa_name, doctype_field, invoice.name
        )

        payments_added.append(
            {"mode_of_payment": phone_mop, "amount": mpesa_amt, "reference": mpesa_name}
        )
        mpesa_results.append({"name": mpesa.name, "amount": mpesa_amt})

    if merge_payments and len(payments_added) > 1:
        ref_text = "\n".join(p["reference"] for p in payments_added)
        invoice.append(
            "payments",
            {
                "mode_of_payment": phone_mop,
                "amount": sum(p["amount"] for p in payments_added),
                "account": mop_account,
                "type": "Phone",
                "custom_reference_text": ref_text,
            },
        )
    else:
        for p in payments_added:
            invoice.append(
                "payments",
                {
                    "mode_of_payment": phone_mop,
                    "amount": p["amount"],
                    "account": mop_account,
                    "type": "Phone",
                    "reference_no": p["reference"],
                },
            )

    result = {
        "success": True,
        "doctype": doctype,
        "payments_added": payments_added,
        "mpesa_payments": mpesa_results,
        "total_amount": sum(p["amount"] for p in payments_added),
        "merged": bool(merge_payments),
    }

    if auto_save:
        try:
            invoice.save(ignore_permissions=True)
            result["saved"] = True
            if auto_submit and invoice.docstatus == 0:
                if flt(invoice.outstanding_amount or 0) <= 0:
                    invoice.submit()
                    result["submitted"] = True
        except Exception as e:
            frappe.log_error(
                frappe.get_traceback(), "Mpesa Quick Pay – Invoice Save/Submit Error"
            )
            result["error"] = str(e)

    return result


def _update_mpesa_after_submit():
    """
    Link Mpesa entries to an already submitted invoice.

    - Does NOT modify invoice.payments
    - Does NOT try to save the invoice

    Params:
        doctype
        invoice_name
        customer
        mpesa_payments (comma separated)
    """
    doctype = resolve_doctype(frappe.form_dict.get("doctype"))
    invoice_name = frappe.form_dict.get("invoice_name")
    customer = frappe.form_dict.get("customer")
    names_str = frappe.form_dict.get("mpesa_payments") or ""

    mpesa_names = [n.strip() for n in names_str.split(",") if n.strip()]
    if not mpesa_names:
        frappe.throw(_("No Mpesa payments provided"))

    # override get_invoice
    invoice = frappe.get_doc(doctype, invoice_name)
    if invoice.docstatus != 1:
        frappe.throw(_("Invoice must be submitted first"))

    phone_mop = get_phone_mop_for_company(invoice.company)
    shortcode = get_mpesa_shortcode_for_company(invoice.company)

    if not phone_mop or not shortcode:
        frappe.throw(
            _("Mpesa configuration incomplete for {0}").format(invoice.company)
        )

    total_amount = 0
    doctype_field = "pos_invoice" if doctype == "POS Invoice" else "sales_invoice"

    try:
        for mpesa_name in mpesa_names:
            mpesa = frappe.get_doc("Mpesa C2B Payment Register", mpesa_name)

            # fail silently - status checked in get_mpesa_payments:
            if mpesa.docstatus != 0:
                continue
            if str(mpesa.businessshortcode or "") != shortcode:
                continue

            amount = flt(mpesa.transamount or 0)
            if amount <= 0:
                continue

            # Tag + submit Mpesa
            mpesa.customer = customer
            mpesa.submit_payment = 0
            mpesa.save(ignore_permissions=True)
            mpesa.submit()

            frappe.db.set_value(
                "Mpesa C2B Payment Register",
                mpesa.name,
                doctype_field,
                invoice.name,
            )

            total_amount += amount

    except Exception as e:
        frappe.throw(_("Error updating Mpesa payments: {0}").format(str(e)))

    return {"success": True}


def _get_customer_phone():
    """
    Return the primary mobile number for a customer.
    Checks linked Contact first, then the Customer record directly.

    Params: customer
    Returns: phone string (empty string if not found)
    """
    customer = frappe.form_dict.get("customer")
    phone = ""

    if customer:
        # Try to get phone from Contact linked to customer
        contact = frappe.db.get_value(
            "Dynamic Link",
            {
                "link_doctype": "Customer",
                "link_name": customer,
                "parenttype": "Contact",
            },
            "parent",
        )

        if contact:
            phone = frappe.db.get_value("Contact", contact, "mobile_no") or ""
            if not phone:
                phone = frappe.db.get_value("Contact", contact, "phone") or ""

        # If no contact, try customer's mobile_no field directly
        if not phone:
            phone = frappe.db.get_value("Customer", customer, "mobile_no") or ""

    return phone


def _create_payment_request():
    """
    Create and submit a Payment Request.

    Params:
        doctype       – "POS Invoice" | "Sales Invoice"
        invoice_name  – document name
        customer
        phone_number
        amount
    Returns: { success: True, payment_request: <name> }
    """
    doctype = resolve_doctype(frappe.form_dict.get("doctype"))
    invoice_name = frappe.form_dict.get("invoice_name")
    customer = frappe.form_dict.get("customer")
    phone_number = (frappe.form_dict.get("phone_number") or "").strip()
    amount = flt(frappe.form_dict.get("amount") or 0)

    if not invoice_name or not phone_number or amount <= 0:
        frappe.throw(
            _("invoice_name, phone_number, and a positive amount are all required")
        )

    invoice = get_invoice(doctype, invoice_name)

    settings = frappe.get_all(
        "Mpesa Settings",
        filters={"company": invoice.company},
        fields=["name", "payment_gateway_name"],
        limit=1,
    )
    if not settings:
        frappe.throw(_("No Mpesa Settings found for {0}").format(invoice.company))

    gateway_name = settings[0].get("payment_gateway_name") or settings[0]["name"]
    gateway_account = frappe.db.get_value(
        "Payment Gateway Account",
        {"payment_gateway": gateway_name},
        ["name", "payment_account"],
        as_dict=True,
    )
    if not gateway_account:
        # Try to find by pattern match
        rows = frappe.get_all(
            "Payment Gateway Account",
            filters={"payment_gateway": ["like", "%Mpesa%"]},
            fields=["name", "payment_gateway", "payment_account"],
            limit=1,
        )
        if rows:
            gateway_account = rows[0]

    if not gateway_account:
        frappe.throw(_("No Payment Gateway Account found for Mpesa"))

    pr = frappe.new_doc("Payment Request")
    pr.payment_request_type = "Inward"
    pr.transaction_date = nowdate()
    pr.phone_number = phone_number
    pr.company = invoice.company
    pr.party_type = "Customer"
    pr.party = customer
    pr.reference_doctype = doctype
    pr.reference_name = invoice_name
    pr.grand_total = amount
    pr.currency = invoice.currency
    pr.outstanding_amount = amount
    pr.payment_gateway_account = gateway_account.get("name")
    pr.payment_gateway = gateway_account.get("payment_gateway") or gateway_name
    pr.payment_account = gateway_account.get("payment_account")
    pr.payment_channel = "Phone"
    pr.mode_of_payment = get_phone_mop_for_company(invoice.company)
    pr.subject = _("Payment for {0}").format(invoice_name)
    pr.message = _("Payment for {0}").format(invoice_name)
    pr.mute_email = 1
    pr.make_sales_invoice = 0

    pr.insert(ignore_permissions=True)
    pr.submit()

    return {"success": True, "payment_request": pr.name}
