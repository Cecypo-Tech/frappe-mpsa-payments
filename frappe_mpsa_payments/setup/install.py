# Copyright (c) 2026, Cecypo.Tech and contributors
# For license information, please see license.txt

"""Role and permission setup for the statement importer.

The importer user is deliberately narrow: it may upload a statement and submit
it, and nothing else. It gets no permissions at all on Mpesa C2B Payment
Register -- the importer inserts those records with ignore_permissions=True on
the uploader's behalf.
"""

import frappe
from frappe.permissions import add_permission, update_permission_property

IMPORTER_ROLE = "Mpesa Statement Importer"
IMPORT_DOCTYPE = "Mpesa Statement Import"

# Upload and submit. No delete, no cancel, no amend, no export -- a statement
# import is an accounting event and must not be quietly removed after the fact.
PERMISSIONS = {
    "read": 1,
    "write": 1,
    "create": 1,
    "submit": 1,
    "report": 1,
    "delete": 0,
    "cancel": 0,
    "amend": 0,
    "export": 0,
    "import": 0,
    "share": 0,
}


def after_install():
    setup_importer_role()


def setup_importer_role():
    """Idempotent: safe to run on every install and every migrate."""
    _ensure_role()
    _ensure_permissions()


def _ensure_role():
    if frappe.db.exists("Role", IMPORTER_ROLE):
        return

    role = frappe.new_doc("Role")
    role.role_name = IMPORTER_ROLE
    role.desk_access = 1
    role.insert(ignore_permissions=True)
    frappe.logger().info(f"Created role {IMPORTER_ROLE}")


def _ensure_permissions():
    if not frappe.db.exists("DocType", IMPORT_DOCTYPE):
        # Runs before the doctype exists on a fresh install; after_migrate
        # will pick it up on the next pass.
        return

    add_permission(IMPORT_DOCTYPE, IMPORTER_ROLE, 0)

    for prop, value in PERMISSIONS.items():
        update_permission_property(IMPORT_DOCTYPE, IMPORTER_ROLE, 0, prop, value)


def create_importer_user(email: str, first_name: str, last_name: str = "") -> str:
    """Create a restricted statement-importer user.

    Intentionally takes no password. Frappe sends a welcome/reset email so the
    credential is never passed on a command line, stored in shell history, or
    committed to this repo.

        bench --site <site> execute \\
            frappe_mpsa_payments.setup.install.create_importer_user \\
            --kwargs '{"email": "importer@example.com", "first_name": "Statement"}'
    """
    if frappe.db.exists("User", email):
        user = frappe.get_doc("User", email)
    else:
        user = frappe.new_doc("User")
        user.email = email
        user.first_name = first_name
        user.last_name = last_name
        user.send_welcome_email = 1

    user.user_type = "System User"
    user.flags.ignore_permissions = True
    user.save(ignore_permissions=True)

    existing_roles = {r.role for r in user.get("roles", [])}
    if IMPORTER_ROLE not in existing_roles:
        user.add_roles(IMPORTER_ROLE)

    frappe.db.commit()
    return user.name
