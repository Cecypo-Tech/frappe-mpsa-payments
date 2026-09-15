from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from frappe_mpsa_payments.frappe_mpsa_payments import migrate


class TestCustomFieldOrder(FrappeTestCase):
    def test_link_targets_are_created_before_the_doctype_linking_to_them(self):
        """A fresh install checks B2C Payment Disbursement's Document Links as
        soon as its fields are created, so the columns those links go through
        must already exist. create_fields creates one doctype at a time, in the
        order given, so the targets have to come first."""
        with patch.object(migrate, "create_fields") as create_fields:
            migrate.create_custom_erpnext_fields()

        order = list(create_fields.call_args.args[0])
        linking = order.index("B2C Payment Disbursement")
        for target in ("Journal Entry Account", "Payment Entry Reference"):
            self.assertLess(order.index(target), linking, f"{target} must come first")
