# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals

import frappe
import requests
from frappe import _
from frappe.model.document import Document

from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import get_token
from frappe_mpsa_payments.frappe_mpsa_payments.connectors.connectors import (
    DEFAULT_TIMEOUT,
)
from frappe_mpsa_payments.utils.utils import build_callback_url

CALLBACK_MODULE = "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api"
API_VERSIONS = ("v2", "v1")
PRODUCT_MISMATCH_ERROR_CODE = "401.003.01"

SUCCESS = "success"
FAILED = "failed"
PRODUCT_MISMATCH = "product mismatch"


class MpesaC2BPaymentRegisterURL(Document):
    def validate(self):
        sandbox_url = "https://sandbox.safaricom.co.ke"
        live_url = "https://api.safaricom.co.ke"
        mpesa_settings = frappe.get_doc("Mpesa Settings", self.mpesa_settings)
        env = "production" if not mpesa_settings.sandbox else "sandbox"
        business_shortcode = (
            mpesa_settings.business_shortcode
            if env == "production"
            else mpesa_settings.till_number
        )
        if env == "sandbox":
            base_url = sandbox_url
        else:
            base_url = live_url

        token = get_token(
            app_key=mpesa_settings.consumer_key,
            app_secret=mpesa_settings.get_password("consumer_secret"),
            base_url=base_url,
        )

        # build_callback_url drops the port frappe.utils.get_url appends on a
        # non-production bench (https://host:443/...). A production C2B
        # registration is one-shot, so the URL has to be right first time.
        validation_url = build_callback_url(f"{CALLBACK_MODULE}.validation")
        confirmation_url = build_callback_url(f"{CALLBACK_MODULE}.confirmation")

        payload = {
            "ShortCode": business_shortcode,
            "ResponseType": "Completed",
            "ConfirmationURL": confirmation_url,
            "ValidationURL": validation_url,
        }
        headers = {
            "Authorization": "Bearer {0}".format(token),
            "Content-Type": "application/json",
        }

        # An app that is not subscribed to the v2 C2B product gets 401.003.01
        # "No apiproduct match found" from v2; the same registration goes
        # through on v1. Any other refusal says nothing about the version, so
        # it stands.
        for version in API_VERSIONS:
            outcome = self._register(base_url, version, headers, payload)
            if outcome != PRODUCT_MISMATCH:
                break
        else:
            frappe.msgprint(
                _(
                    "Safaricom answered {0} (no apiproduct match found) on both the "
                    "v2 and v1 C2B register URL endpoints. The app behind these "
                    "credentials is not subscribed to the C2B product."
                ).format(PRODUCT_MISMATCH_ERROR_CODE)
            )

        self.register_status = "Success" if outcome == SUCCESS else "Failed"

    def _register(self, base_url, version, headers, payload):
        register_url = f"{base_url}/mpesa/c2b/{version}/registerurl"

        try:
            r = requests.post(
                register_url,
                headers=headers,
                json=payload,
                timeout=DEFAULT_TIMEOUT,
            )
            r.raise_for_status()  # Raise an HTTPError for bad responses
            res = r.json()
        except requests.exceptions.HTTPError as errh:
            if is_product_mismatch(_json_or_none(errh.response)):
                return PRODUCT_MISMATCH
            frappe.msgprint(
                f"Response Content: {getattr(errh.response, 'content', errh)}"
            )
            return FAILED
        except requests.exceptions.ConnectionError as errc:
            frappe.msgprint(f"Error Connecting: {errc}")
            return FAILED
        except requests.exceptions.Timeout as errt:
            frappe.msgprint(f"Timeout Error: {errt}")
            return FAILED
        except requests.exceptions.RequestException as err:
            frappe.msgprint(f"Request Exception: {err}")
            return FAILED

        if res.get("ResponseDescription") == "Success":
            return SUCCESS
        if is_product_mismatch(res):
            return PRODUCT_MISMATCH
        frappe.msgprint(str(res))
        return FAILED


def is_product_mismatch(response) -> bool:
    if not isinstance(response, dict):
        return False
    return (
        response.get("errorCode") == PRODUCT_MISMATCH_ERROR_CODE
        or "no apiproduct match found" in str(response).lower()
    )


def _json_or_none(response):
    try:
        return response.json()
    except (ValueError, AttributeError):
        return None
