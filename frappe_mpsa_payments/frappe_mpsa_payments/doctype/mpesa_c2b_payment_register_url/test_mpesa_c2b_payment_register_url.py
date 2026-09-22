# Copyright (c) 2024, Navari Limited and Contributors
# See license.txt

from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import MpesaTokenError

from ..mpesa_settings.test_mpesa_settings import create_mpesa_settings

test_dependencies = ["Company"]

SETTINGS = "_Test Register URL"
GET_TOKEN = (
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype"
    ".mpesa_c2b_payment_register_url"
    ".mpesa_c2b_payment_register_url.get_token"
)
PRODUCT_MISMATCH = {
    "requestId": "1",
    "errorCode": "401.003.01",
    "errorMessage": "Error Occurred - No apiproduct match found",
}


def _reply(body, status_code=200):
    response = Mock(status_code=status_code)
    response.json.return_value = body
    return response


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
            patch(GET_TOKEN, return_value="test_token"),
            patch("requests.post", **post) as mocked_post,
        ):
            self.register_url.validate()

        self.posted_urls = [call.args[0] for call in mocked_post.call_args_list]
        return self.register_url.register_status

    def test_a_token_refusal_is_a_readable_error_not_a_500(self):
        """The bug as reported: save returned 500 {"exc_type":"JSONDecodeError"}.

        Safaricom answers a wrong key or secret with an empty 400, and get_token
        blew up decoding it. The user must see why, and Safaricom must not be
        asked to register anything without a token.
        """
        refusal = Mock(status_code=400, text="", headers={"content-type": "text/plain"})
        refusal.json.side_effect = ValueError("Expecting value")

        with (
            patch("requests.get", return_value=refusal),
            patch("requests.post") as mocked_post,
        ):
            with self.assertRaises(MpesaTokenError):
                self.register_url.validate()

        mocked_post.assert_not_called()

    def test_a_v2_product_mismatch_falls_back_to_v1(self):
        """An app not subscribed to the v2 product gets 401.003.01 from v2."""
        status = self._register(
            {
                "side_effect": [
                    _reply(PRODUCT_MISMATCH),
                    _reply({"ResponseDescription": "Success"}),
                ]
            }
        )

        self.assertEqual(status, "Success")
        self.assertEqual(
            [url.rsplit("/", 2)[-2] for url in self.posted_urls], ["v2", "v1"]
        )

    def test_a_v2_product_mismatch_raised_as_an_http_error_falls_back_to_v1(self):
        error = requests.exceptions.HTTPError("401 Client Error")
        error.response = _reply(PRODUCT_MISMATCH, status_code=401)
        error.response.content = b'{"errorCode": "401.003.01"}'

        status = self._register(
            {"side_effect": [error, _reply({"ResponseDescription": "Success"})]}
        )

        self.assertEqual(status, "Success")
        self.assertEqual(
            [url.rsplit("/", 2)[-2] for url in self.posted_urls], ["v2", "v1"]
        )

    def test_a_v1_product_mismatch_is_a_failure_not_a_loop(self):
        frappe.local.message_log = []

        status = self._register(
            {"side_effect": [_reply(PRODUCT_MISMATCH), _reply(PRODUCT_MISMATCH)]}
        )

        self.assertEqual(status, "Failed")
        self.assertEqual(len(self.posted_urls), 2)
        self.assertTrue(
            any("401.003.01" in str(m) for m in frappe.local.message_log),
            "the user must be told why neither version took the registration",
        )

    def test_an_ordinary_v2_refusal_does_not_retry_on_v1(self):
        """Only a product mismatch says anything about the API version."""
        status = self._register(
            {"side_effect": [_reply({"ResponseDescription": "Failure"})]}
        )

        self.assertEqual(status, "Failed")
        self.assertEqual(len(self.posted_urls), 1)

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
