# Copyright (c) 2024, Navari Limited and Contributors
# See license.txt

from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from ..mpesa_settings.test_mpesa_settings import create_mpesa_settings

test_dependencies = ["Company"]

SETTINGS = "_Test Register URL"


class TestMpesaC2BPaymentRegisterURL(FrappeTestCase):
    """This used to subclass TestMpesaSettings.

    That pulled every one of its tests in as inherited copies, ran them a
    second time against a setUp built for a different doctype, and then tore
    them down by deleting a Mpesa C2B Payment Register URL named after the
    settings document - which does not exist, so tearDown threw on its own.
    """

    def setUp(self):
        frappe.set_user("Administrator")
        create_mpesa_settings(payment_gateway_name=SETTINGS)

        self.register_url = frappe.new_doc("Mpesa C2B Payment Register URL")
        self.register_url.update(
            {
                "business_shortcode": "174379",
                "mpesa_settings": SETTINGS,
                "till_number": "174379",
                "register_status": "Pending",
            }
        )

    def _register(self, post):
        """Run validate with requests.post standing in for Safaricom."""
        with (
            patch(
                "frappe_mpsa_payments.frappe_mpsa_payments.doctype"
                ".mpesa_c2b_payment_register_url"
                ".mpesa_c2b_payment_register_url.get_token",
                return_value="test_token",
            ),
            patch("requests.post", **post),
        ):
            self.register_url.validate()

        return self.register_url.register_status

    def test_safaricom_accepting_the_registration_is_recorded(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ResponseDescription": "Success"}

        self.assertEqual(self._register({"return_value": response}), "Success")

    def test_safaricom_refusing_the_registration_is_recorded(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ResponseDescription": "Failure"}

        self.assertEqual(self._register({"return_value": response}), "Failed")

    def test_an_http_error_is_recorded_as_a_failure(self):
        """A registration that never landed must not be left looking Pending.

        Worse, a retry over an earlier Success would have left Success
        standing - the status branches did not set anything at all.
        """
        error = requests.exceptions.HTTPError("500 Server Error")
        error.response = Mock(content=b"server exploded")

        self.assertEqual(self._register({"side_effect": error}), "Failed")

    def test_a_connection_error_is_recorded_as_a_failure(self):
        self.assertEqual(
            self._register(
                {"side_effect": requests.exceptions.ConnectionError("no route")}
            ),
            "Failed",
        )

    def test_a_timeout_is_recorded_as_a_failure(self):
        self.assertEqual(
            self._register({"side_effect": requests.exceptions.Timeout("too slow")}),
            "Failed",
        )

    def test_a_success_is_not_left_standing_after_a_later_failure(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ResponseDescription": "Success"}
        self.assertEqual(self._register({"return_value": response}), "Success")

        self.assertEqual(
            self._register(
                {"side_effect": requests.exceptions.ConnectionError("no route")}
            ),
            "Failed",
        )
