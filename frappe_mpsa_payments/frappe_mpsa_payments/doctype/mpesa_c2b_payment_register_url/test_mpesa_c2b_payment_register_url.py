# Copyright (c) 2024, Navari Limited and Contributors
# See license.txt

import frappe

from ..mpesa_settings.test_mpesa_settings import (
    TestMpesaSettings,
)


def create_mpesa_setting_doc():
    if not frappe.db.exists("Mpesa Settings", "Test Mpesa Settings"):
        mpesa_settings1 = frappe.new_doc("Mpesa Settings")
        mpesa_settings1.payment_gateway_name = "Test Mpesa Settings"
        mpesa_settings1.mpesa_environment = "sandbox"
        mpesa_settings1.consumer_key = (
            "xMPJE16CDdAfBmOWvbqRsqlioAcQT77sWw2JD9OcceHp8fHv"
        )
        mpesa_settings1.consumer_secret = (
            "NDXh2tdne9bMrnOEZXd8gQZiHPMWSpfWc2YXBLGQxiz66OGbcn5S79DKakgt3LQN"
        )
        mpesa_settings1.shortcode = "123456"
        mpesa_settings1.business_shortcode = "123456"
        mpesa_settings1.online_passkey = (
            "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"
        )
        mpesa_settings1.paybill_type = "Pay Bill"
        mpesa_settings1.till_number = "123456"
        mpesa_settings1.initiator_name = "test_initiator_name"
        mpesa_settings1.transaction_limit = 1000
        mpesa_settings1.security_credential = "test_security_credential"
        mpesa_settings1.sandbox = 1
        mpesa_settings1.save()


def create_mpesa_c2b_payment_register_url_doc():
    if not frappe.db.exists(
        "Mpesa C2B Payment Register URL", "Test Mpesa C2B Payment Register URL"
    ):
        mpesa_c2b_payment_register_url = frappe.new_doc(
            "Mpesa C2B Payment Register URL"
        )
        mpesa_c2b_payment_register_url.business_shortcode = "123456"
        mpesa_c2b_payment_register_url.mpesa_settings = "Test Mpesa Settings"
        mpesa_c2b_payment_register_url.register_status = "Success"
        mpesa_c2b_payment_register_url.till_number = "123456"
        mpesa_c2b_payment_register_url.mode_of_payment = "Cash"
        mpesa_c2b_payment_register_url.company = "Navari Limited"
        mpesa_c2b_payment_register_url.save()


class TestMpesaC2BPaymentRegisterURL(TestMpesaSettings):
    pass
