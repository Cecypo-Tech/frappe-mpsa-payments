import frappe
from erpnext.setup.utils import enable_all_roles_and_domains
from frappe.utils import now_datetime, nowdate


def before_tests():
    frappe.clear_cache()
    # complete setup if missing
    from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

    year = now_datetime().year
    if not frappe.get_list("Company"):
        setup_complete(
            {
                "currency": "KES",
                "full_name": "Test User",
                "company_name": "Navari Limited",
                "timezone": "Africa/Nairobi",
                "company_abbr": "NL",
                "industry": "Software",
                "country": "Kenya",
                "fy_start_date": f"{year}-01-01",
                "fy_end_date": f"{year}-12-31",
                "language": "english",
                "company_tagline": "Testing",
                "email": "test@navari.co.ke",
                "password": "test",
                "chart_of_accounts": "Standard",
            }
        )

    enable_all_roles_and_domains()
    # Manually commit to save setup progress in case of timeout
    frappe.db.commit()  # nosemgrep


def create_test_employee(employee_name="John Doe", company="Navari Limited"):
    """Create a test employee"""
    if frappe.db.exists("Employee", {"employee_name": employee_name}):
        return frappe.get_doc("Employee", {"employee_name": employee_name})

    employee = frappe.get_doc(
        {
            "doctype": "Employee",
            "employee_name": employee_name,
            "status": "Active",
            "company": company,
        }
    ).insert(ignore_mandatory=True, ignore_permissions=True)

    return employee


def create_test_employee_advance(employee, advance_amount=100):
    """Create a test employee advance"""
    employee_advance = frappe.get_doc(
        {
            "doctype": "Employee Advance",
            "employee": employee.name,
            "posting_date": nowdate(),
            "purpose": "Test",
            "advance_amount": advance_amount,
            "currency": "KES",
            "exchange_rate": 1,
        }
    ).insert()

    employee_advance.submit()
    return employee_advance


def create_test_payment_disbursement(
    employee,
    employee_advance,
    allocated_amount=100,
    outstanding_amount=100,
    partyb="0712345678",
    insert=True,
    submit=True,
):
    """Create a test B2C Payment Disbursement"""
    frappe.db.set_value(
        "Account",
        "Employee Advances - NL",
        "account_type",
        "Receivable",
        update_modified=False,
    )

    payment_disbursement = frappe.get_doc(
        {
            "doctype": "B2C Payment Disbursement",
            "payment_type": "Mpesa Disbursement",
            "posting_date": nowdate(),
            "company": "Navari Limited",
            "mode_of_payment": "Mpesa-Payment",
            "party_type": "Employee",
            "transaction_to_pay_against": "Employee Advance",
            "paid_from": "Mpesa-Payment - NL",
            "paid_from_account_currency": "KES",
            "paid_to": "Employee Advances - NL",
            "paid_to_account_currency": "KES",
            "references": [
                {
                    "reference_doctype": "Employee Advance",
                    "reference_name": employee_advance.name,
                    "party_type": "Employee",
                    "party": employee.name,
                    "allocated_amount": allocated_amount,
                    "outstanding_amount": outstanding_amount,
                    "partyb": partyb,
                }
            ],
        }
    )

    if insert:
        payment_disbursement.insert()
        if submit:
            payment_disbursement.submit()

    return payment_disbursement


def cleanup_test_documents(*docs):
    """Helper to clean up test documents"""
    for doc in docs:
        if doc and hasattr(doc, "name") and hasattr(doc, "doctype"):
            try:
                frappe.delete_doc(doc.doctype, doc.name, force=True)
            except Exception:
                pass
    frappe.db.commit()  # nosemgrep


def create_mpesa_settings(payment_gateway_name="Express"):
    if frappe.db.exists("Mpesa Settings", payment_gateway_name):
        return frappe.get_doc("Mpesa Settings", payment_gateway_name)

    doc = frappe.get_doc(
        doctype="Mpesa Settings",
        sandbox=1,
        payment_gateway_name=payment_gateway_name,
        consumer_key="5sMu9LVI1oS3oBGPJfh3JyvLHwZOdTKn",
        consumer_secret="VI1oS3oBGPJfh3JyvLHw",
        online_passkey="LVI1oS3oBGPJfh3JyvLHwZOd",
        till_number="174379",
        paybill_type="Pay Bill",
    )

    doc.insert(ignore_permissions=True)
    return doc
