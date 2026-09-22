# Copyright (c) 2026, Cecypo Tech and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_mpsa_payments.utils.utils import site_address


class TestSiteAddress(FrappeTestCase):
    def test_it_falls_back_to_site_config_when_there_is_no_request(self):
        """The test runner, a bench command and a scheduled sweep have no request.

        Frappe 16.34 made the unbound request proxy raise AttributeError
        rather than RuntimeError, which slipped past the fallback and took the
        C2B register-URL save down with it.
        """
        self.assertEqual(site_address(), frappe.utils.get_url())
