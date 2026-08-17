"""Put Mpesa Statement Import on the M-Pesa workspace.

Frappe only creates a Workspace from its JSON when the record is missing; on
sites that already have the M-Pesa workspace the new link would never appear
and the importer would be reachable only by search. Reloading the workspace
is the supported way to push the change out.
"""

import frappe


def execute():
    if not frappe.db.exists("Workspace", "M-Pesa"):
        return

    frappe.reload_doc("frappe_mpsa_payments", "workspace", "m_pesa", force=True)
