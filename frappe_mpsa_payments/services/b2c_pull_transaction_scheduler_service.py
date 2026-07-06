import frappe
from frappe.utils import add_to_date, now_datetime

from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import pull_transactions


def run_hourly_pull_transactions():
    settings_list = frappe.get_all(
        "Mpesa Settings",
        filters={"enable_daily_pull_transactions": 1},
        fields=["name"],
    )
    end_date = now_datetime()
    start_date = add_to_date(end_date, hours=-2)

    for row in settings_list:
        try:
            pull_transactions(
                mpesa_settings=row.name,
                start_date=start_date.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end_date.strftime("%Y-%m-%d %H:%M:%S"),
                offset=0,
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"[Daily Pull Transaction Error] {row.name}",
            )
