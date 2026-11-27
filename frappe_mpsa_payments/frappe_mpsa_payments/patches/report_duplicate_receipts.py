import frappe

REGISTER = "Mpesa C2B Payment Register"


def execute():
    """Report every M-Pesa receipt that has more than one live register row.

    Before the insert guard, the C2B callback and a Pull Transactions could record
    one receipt twice. Which copy to cancel depends on which one a sale used - and a
    receipt whose copies each carry a Payment Entry is money booked twice, for an
    accountant to settle - so this only reports; it changes no row.
    """
    transids = frappe.db.sql_list(
        f"""SELECT transid FROM `tab{REGISTER}`
        WHERE docstatus < 2 AND IFNULL(transid, '') != ''
        GROUP BY transid HAVING COUNT(*) > 1"""
    )
    if not transids:
        return

    rows = frappe.get_all(
        REGISTER,
        filters={"transid": ["in", transids], "docstatus": ["<", 2]},
        fields=["transid", "name", "docstatus", "transamount", "payment_entry", "sales_invoice"]
        if frappe.db.has_column(REGISTER, "sales_invoice")
        else ["transid", "name", "docstatus", "transamount", "payment_entry"],
        order_by="transid, creation",
    )
    state = {0: "Draft", 1: "Submitted"}
    lines = [
        f"{len(transids)} receipt(s) have more than one live register row.",
        "A Draft copy whose receipt has a Submitted row is not offered to any picker.",
        "Cancel or delete the spare copies by hand; a receipt with two Payment Entries is booked twice.",
        "",
    ]
    for r in rows:
        lines.append(
            f"{r.transid}  {r.name}  {state[r.docstatus]}  {r.transamount}"
            f"  PE={r.payment_entry or '-'}  SI={r.get('sales_invoice') or '-'}"
        )
    frappe.log_error(title="M-Pesa duplicate receipts", message="\n".join(lines))
