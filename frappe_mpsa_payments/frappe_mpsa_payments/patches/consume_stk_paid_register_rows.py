import frappe

from ..doctype.mpesa_c2b_payment_register.mpesa_c2b_payment_register import (
    consume_for_stk_push,
)


def execute():
    """Submit the draft C2B rows whose money an STK push already took."""
    transids = frappe.db.sql_list(
        """SELECT DISTINCT reg.transid
        FROM `tabMpesa C2B Payment Register` reg
        INNER JOIN `tabMpesa Express Request` req
            ON req.transaction_id = reg.transid AND req.status = 'Completed'
        WHERE reg.docstatus = 0 AND IFNULL(reg.transid, '') != ''"""
    )
    for transid in transids:
        consume_for_stk_push(transid)
