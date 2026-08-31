# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from json import dumps

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import verify_transaction
from frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings import (
    create_mode_of_payment,
    process_balance_info,
)

# The setUp below leans on ERPNext's own test records - Wind Power LLC and its
# accounts - which the runner only creates for doctypes a module declares.
test_dependencies = ["Company", "Item", "Customer", "POS Profile"]

#: ERPNext's own test company, which owns the accounts these tests post to.
POS_COMPANY = "Wind Power LLC"

#: The name erpnext's make_pos_profile defaults to when no name is passed.
POS_PROFILE = "_Test POS Profile"


class TestMpesaSettings(FrappeTestCase):
    def setUp(self):
        # The runner does not guarantee a user, and these tests write
        # accounting documents.
        frappe.set_user("Administrator")

        from erpnext.accounts.doctype.payment_entry.test_payment_entry import (
            create_customer,
        )
        from erpnext.accounts.doctype.pos_opening_entry.test_pos_opening_entry import (
            create_opening_entry,
        )
        from erpnext.accounts.doctype.pos_profile.test_pos_profile import (
            make_pos_profile,
        )
        from erpnext.stock.doctype.item.test_item import make_item

        # Integration Request rows are committed outside the test transaction
        # by handle_success, so a previous run leaves one behind and the next
        # skips creating it - and then asserts against stale data.
        for stale in frappe.get_all(
            "Integration Request", filters={"integration_request_service": "Mpesa"}
        ):
            frappe.delete_doc("Integration Request", stale.name, force=1)

        # create payment gateway in setup
        create_mpesa_settings(payment_gateway_name="_Test")
        create_mpesa_settings(payment_gateway_name="_Account Balance")
        create_mpesa_settings(payment_gateway_name="Payment", company=POS_COMPANY)

        self.customer = create_customer("_Test Customer", "USD")
        self.item = make_item(properties={"is_stock_item": 1}).name
        # Reuse the profile when one is already there. make_pos_profile guards
        # its own insert, but only against the committed row - inside the test
        # transaction the guard can miss, and inserting a POS Profile fires
        # POSProfile.on_update -> set_defaults -> clear_default("is_pos"), which
        # is a keyed DELETE across the small, global tabDefaultValue. Paying that
        # on all twelve tests is what made this class deadlock intermittently.
        if frappe.db.exists("POS Profile", POS_PROFILE):
            pos_profile = frappe.get_doc("POS Profile", POS_PROFILE)
        else:
            pos_profile = make_pos_profile(
                company=POS_COMPANY,
                cost_center="Main - WP",
                currency="USD",
                expense_account="Cost of Goods Sold - WP",
                income_account="Sales - WP",
                selling_price_list="Standard Selling",
                territory="United States",
                warehouse="Stores - WP",
                write_off_account="Write Off - WP",
                write_off_cost_center="Main - WP",
            )
        self.pos_profile = pos_profile.name

        # These tests are about POS Invoice payments, so POS has to be in POS
        # Invoice mode - a site switched to Sales Invoice mode refuses them.
        #
        # Only written when it actually differs. Writing it unconditionally
        # locked the POS Settings row in tabSingles on every one of these tests,
        # which deadlocked against anything else touching the site while the
        # suite ran - reproducible, and the cause of an intermittent failure in
        # this class.
        if frappe.db.get_single_value("POS Settings", "invoice_type") != "POS Invoice":
            frappe.db.set_single_value("POS Settings", "invoice_type", "POS Invoice")

        # A POS Invoice will not submit against a profile with no open shift.
        # The shift is opened for a cashier of this suite's own, because a
        # user may hold only one open shift at a time and Administrator often
        # already has one on a site that has seen real use.
        #
        # Reusing any open shift is not enough. Sales Invoice's
        # validate_pos_opening_entry demands one whose period_start_date is
        # *today*, and throws outright if the profile has more than one open at
        # once. A shift committed by an earlier run therefore goes stale at
        # midnight and takes the whole suite red with it. Retire whatever is
        # lying around, then open today's.
        today = frappe.utils.today()
        shift_for_today = None
        for entry in frappe.get_all(
            "POS Opening Entry",
            filters={"pos_profile": pos_profile.name, "status": "Open"},
            fields=["name", "period_start_date"],
        ):
            if shift_for_today is None and (
                frappe.utils.get_date_str(entry.period_start_date) == today
            ):
                shift_for_today = entry.name
            else:
                frappe.db.set_value("POS Opening Entry", entry.name, "status", "Closed")

        if not shift_for_today:
            create_opening_entry(pos_profile, _test_cashier())

        self.mpesa_account = _ensure_mode_of_payment_account(
            "Mpesa-Payment",
            POS_COMPANY,
            _ensure_gateway_account("Mpesa-Payment", POS_COMPANY),
        )

    def test_creation_of_payment_gateway(self):
        mode_of_payment = create_mode_of_payment("Mpesa-_Test", payment_type="Phone")
        self.assertTrue(
            frappe.db.exists(
                "Payment Gateway Account", {"payment_gateway": "Mpesa-_Test"}
            )
        )
        self.assertTrue(mode_of_payment.name)
        self.assertEqual(mode_of_payment.type, "Phone")

    def test_processing_of_account_balance(self):
        mpesa_doc = create_mpesa_settings(payment_gateway_name="_Account Balance")
        mpesa_doc.get_account_balance_info()

        callback_response = get_account_balance_callback_payload()
        process_balance_info(**callback_response)
        integration_request = frappe.get_doc(
            "Integration Request", "AG_20200927_00007cdb1f9fb6494315"
        )

        # test integration request creation and successful update of the status on receiving callback response
        self.assertTrue(integration_request)
        self.assertEqual(integration_request.status, "Completed")

        # test formatting of account balance received as string to json with appropriate currency symbol
        mpesa_doc.reload()
        self.assertEqual(
            mpesa_doc.account_balance,
            dumps(
                {
                    "Working Account": {
                        "current_balance": "Sh 481,000.00",
                        "available_balance": "Sh 481,000.00",
                        "reserved_balance": "Sh 0.00",
                        "uncleared_balance": "Sh 0.00",
                    }
                }
            ),
        )

        integration_request.delete()

    def test_processing_of_callback_payload(self):
        from erpnext.accounts.doctype.pos_invoice.test_pos_invoice import (
            create_pos_invoice,
        )

        mpesa_account = self.mpesa_account
        frappe.db.set_value("Account", mpesa_account, "account_currency", "KES")
        frappe.db.set_value("Customer", "_Test Customer", "default_currency", "KES")
        pos_invoice = create_pos_invoice(
            item=self.item,
            customer=self.customer,
            debit_to="Debtors - WP",
            warehouse="Stores - WP",
            cost_center="Main - WP",
            company=POS_COMPANY,
            income_account="Sales - WP",
            pos_profile=self.pos_profile,
            account_for_change_amount="Cash - WP",
            expense_account="Cost of Goods Sold - WP",
            do_not_submit=1,
        )
        pos_invoice.append(
            "payments",
            {
                "mode_of_payment": "Mpesa-Payment",
                "account": mpesa_account,
                "amount": 500,
            },
        )
        pos_invoice.contact_mobile = "093456543894"
        pos_invoice.currency = "KES"
        pos_invoice.save()

        pr = pos_invoice.create_payment_request()
        # test payment request creation
        self.assertEqual(pr.payment_gateway, "Mpesa-Payment")

        # submitting payment request creates integration requests with random id
        integration_req_ids = frappe.get_all(
            "Integration Request",
            filters={
                "reference_doctype": pr.doctype,
                "reference_docname": pr.name,
            },
            pluck="name",
        )

        callback_response = get_payment_callback_payload(
            Amount=500, CheckoutRequestID=integration_req_ids[0]
        )
        verify_transaction(**callback_response)
        # test creation of integration request
        integration_request = frappe.get_doc(
            "Integration Request", integration_req_ids[0]
        )

        # test integration request creation and successful update of the status on receiving callback response
        self.assertTrue(integration_request)
        self.assertEqual(integration_request.status, "Completed")

        pos_invoice.reload()
        integration_request.reload()
        self.assertEqual(pos_invoice.mpesa_receipt_number, "LGR7OWQX0R")
        self.assertEqual(integration_request.status, "Completed")

        frappe.db.set_value("Customer", "_Test Customer", "default_currency", "")
        integration_request.delete()
        pr.reload()
        pr.cancel()
        pr.delete()
        pos_invoice.delete()

    def test_processing_of_multiple_callback_payload(self):
        from erpnext.accounts.doctype.pos_invoice.test_pos_invoice import (
            create_pos_invoice,
        )

        mpesa_account = self.mpesa_account
        frappe.db.set_value("Account", mpesa_account, "account_currency", "KES")
        frappe.db.set_value("Mpesa Settings", "Payment", "transaction_limit", "500")
        frappe.db.set_value("Customer", "_Test Customer", "default_currency", "KES")

        pos_invoice = create_pos_invoice(
            item=self.item,
            customer=self.customer,
            debit_to="Debtors - WP",
            warehouse="Stores - WP",
            cost_center="Main - WP",
            company=POS_COMPANY,
            income_account="Sales - WP",
            pos_profile=self.pos_profile,
            account_for_change_amount="Cash - WP",
            expense_account="Cost of Goods Sold - WP",
            do_not_submit=1,
        )
        pos_invoice.append(
            "payments",
            {
                "mode_of_payment": "Mpesa-Payment",
                "account": mpesa_account,
                "amount": 1000,
            },
        )
        pos_invoice.contact_mobile = "093456543894"
        pos_invoice.currency = "KES"
        pos_invoice.save()

        pr = pos_invoice.create_payment_request()
        # test payment request creation
        self.assertEqual(pr.payment_gateway, "Mpesa-Payment")

        # submitting payment request creates integration requests with random id
        integration_req_ids = frappe.get_all(
            "Integration Request",
            filters={
                "reference_doctype": pr.doctype,
                "reference_docname": pr.name,
            },
            pluck="name",
        )

        # create random receipt nos and send it as response to callback handler
        mpesa_receipt_numbers = [
            frappe.utils.random_string(5) for d in integration_req_ids
        ]

        integration_requests = []
        for i in range(len(integration_req_ids)):
            callback_response = get_payment_callback_payload(
                Amount=500,
                CheckoutRequestID=integration_req_ids[i],
                MpesaReceiptNumber=mpesa_receipt_numbers[i],
            )
            # handle response manually
            verify_transaction(**callback_response)
            # test completion of integration request
            integration_request = frappe.get_doc(
                "Integration Request", integration_req_ids[i]
            )
            self.assertEqual(integration_request.status, "Completed")
            integration_requests.append(integration_request)

        # check receipt number once all the integration requests are completed
        pos_invoice.reload()
        self.assertEqual(
            pos_invoice.mpesa_receipt_number, ", ".join(mpesa_receipt_numbers)
        )

        frappe.db.set_value("Customer", "_Test Customer", "default_currency", "")
        [d.delete() for d in integration_requests]
        pr.reload()
        pr.cancel()
        pr.delete()
        pos_invoice.delete()

    def test_register_pull_transaction_missing_nominated_number(self):
        from frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings import (
            register_pull_transaction,
        )

        # _Test settings has no pull_transaction_nominated_number
        with self.assertRaises(frappe.exceptions.ValidationError):
            register_pull_transaction("_Test")

    def test_register_pull_transaction_success(self):
        from unittest.mock import MagicMock, patch

        from frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings import (
            register_pull_transaction,
        )

        frappe.db.set_value(
            "Mpesa Settings",
            "_Test",
            "pull_transaction_nominated_number",
            "254712345678",
        )

        mock_response = MagicMock()
        # The register endpoint answers with spaced keys and a 1000 status,
        # which is what register_pull_transaction reads. The old ResponseCode
        # payload below never matched, so a success looked like a failure.
        mock_response.json.return_value = {
            "Response Status": "1000",
            "Response Description": "Accept the service request successfully.",
        }
        mock_response.raise_for_status = MagicMock()

        with (
            patch(
                "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.get_token",
                return_value="test_token",
            ),
            patch("requests.post", return_value=mock_response),
        ):
            result = register_pull_transaction("_Test")

        self.assertEqual(result["status"], "success")
        self.assertIn("Accept", result["message"])

    def test_pull_transaction_on_success_creates_c2b_records(self):
        from frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_response_handler import (
            pull_transaction_on_success,
        )

        unique_txn_id = f"PULL{frappe.generate_hash()[:8].upper()}"
        response = {
            "ResponseCode": "1000",
            "ResponseMessage": "Success",
            "Response": [
                [
                    {
                        "transactionId": unique_txn_id,
                        "trxDate": "2026-06-01T12:00:00+03:00",
                        "msisdn": "254712345678",
                        "sender": "MPESA",
                        "transactiontype": "c2b-paybill-debi",
                        "billreference": "TEST-REF-001",
                        "amount": "250.00",
                        "organizationname": "Safaricom Daraja 978",
                    }
                ]
            ],
            "CurrentPage": 0,
            "PageSize": 10,
            "TotalPages": 1,
            "TotalRecords": 1,
        }

        pull_transaction_on_success(
            response=response,
            document_name="_Test",
            settings_name="_Test",
            integration_request=None,
        )

        exists = frappe.db.exists(
            "Mpesa C2B Payment Register", {"transid": unique_txn_id}
        )
        self.assertTrue(exists)
        frappe.db.delete("Mpesa C2B Payment Register", {"transid": unique_txn_id})

    def test_pull_transaction_on_success_skips_duplicates(self):
        from frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_response_handler import (
            pull_transaction_on_success,
        )

        unique_txn_id = f"DUP{frappe.generate_hash()[:8].upper()}"

        # Pre-insert so it already exists
        existing = frappe.get_doc(
            {
                "doctype": "Mpesa C2B Payment Register",
                "transid": unique_txn_id,
                "transamount": 100.0,
                "msisdn": "254700000001",
                "businessshortcode": "174379",
            }
        )
        existing.insert(ignore_permissions=True)

        response = {
            "ResponseCode": "1000",
            "Response": [
                [
                    {
                        "transactionId": unique_txn_id,
                        "trxDate": "2026-06-01T12:00:00+03:00",
                        "msisdn": "254712345678",
                        "sender": "MPESA",
                        "transactiontype": "c2b-paybill-debi",
                        "billreference": "",
                        "amount": "100.00",
                        "organizationname": "Safaricom Daraja 978",
                    }
                ]
            ],
        }

        # Should not raise; duplicate is silently skipped
        pull_transaction_on_success(
            response=response,
            document_name="_Test",
            settings_name="_Test",
            integration_request=None,
        )

        count = frappe.db.count(
            "Mpesa C2B Payment Register", {"transid": unique_txn_id}
        )
        self.assertEqual(count, 1)
        frappe.db.delete("Mpesa C2B Payment Register", {"transid": unique_txn_id})

    def _capture_pull_realtime(self, response):
        """Run pull_transaction_on_success and return the realtime message it published."""
        from unittest.mock import patch

        from frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_response_handler import (
            pull_transaction_on_success,
        )

        published = {}

        def fake_publish(*args, **kwargs):
            if kwargs.get("event") == "mpesa_pull_transaction_complete":
                published.update(kwargs.get("message") or {})

        with patch(
            "frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_response_handler.frappe.publish_realtime",
            side_effect=fake_publish,
        ):
            pull_transaction_on_success(
                response=response,
                document_name="_Test",
                settings_name="_Test",
                integration_request=None,
            )
        return published

    def test_pull_transaction_1001_is_not_reported_as_success(self):
        """1001 arrives as HTTP 200 and must not read as a successful import.

        Regression: it used to fall through to the import loop, find no
        "Response" key, and publish "0 record(s) imported" - making an
        unprovisioned shortcode indistinguishable from a quiet window.
        """
        published = self._capture_pull_realtime(
            {
                "ResponseCode": "1001",
                "ResponseMessage": "No records found or Organization Name not available",
            }
        )

        self.assertEqual(published.get("status"), "warning")
        self.assertEqual(published.get("response_code"), "1001")
        self.assertIn("1001", published.get("message", ""))
        self.assertNotIn("record(s) imported", published.get("message", ""))

        self.assertEqual(
            frappe.db.get_value("Mpesa Settings", "_Test", "last_pull_status"),
            "No Data",
        )
        self.assertEqual(
            frappe.db.get_value("Mpesa Settings", "_Test", "last_pull_response_code"),
            "1001",
        )

    def test_pull_transaction_other_error_code_is_reported_as_error(self):
        published = self._capture_pull_realtime(
            {"ResponseCode": "500.003.02", "ResponseMessage": "Internal server error"}
        )

        self.assertEqual(published.get("status"), "error")
        self.assertEqual(published.get("count"), 0)
        self.assertEqual(
            frappe.db.get_value("Mpesa Settings", "_Test", "last_pull_status"), "Error"
        )

    def test_pull_transaction_1000_with_empty_response_still_reports_zero(self):
        """A genuinely quiet window is still a success, just with nothing to import."""
        published = self._capture_pull_realtime(
            {"ResponseCode": "1000", "ResponseMessage": "Success", "Response": []}
        )

        self.assertIn("0 record(s) imported", published.get("message", ""))
        self.assertEqual(published.get("count"), 0)
        self.assertEqual(
            frappe.db.get_value("Mpesa Settings", "_Test", "last_pull_status"),
            "Success",
        )

    def test_processing_of_only_one_succes_callback_payload(self):
        from erpnext.accounts.doctype.pos_invoice.test_pos_invoice import (
            create_pos_invoice,
        )

        mpesa_account = self.mpesa_account
        frappe.db.set_value("Account", mpesa_account, "account_currency", "KES")
        frappe.db.set_value("Mpesa Settings", "Payment", "transaction_limit", "500")
        frappe.db.set_value("Customer", "_Test Customer", "default_currency", "KES")

        pos_invoice = create_pos_invoice(
            item=self.item,
            customer=self.customer,
            debit_to="Debtors - WP",
            warehouse="Stores - WP",
            cost_center="Main - WP",
            company=POS_COMPANY,
            income_account="Sales - WP",
            pos_profile=self.pos_profile,
            account_for_change_amount="Cash - WP",
            expense_account="Cost of Goods Sold - WP",
            do_not_submit=1,
        )
        pos_invoice.append(
            "payments",
            {
                "mode_of_payment": "Mpesa-Payment",
                "account": mpesa_account,
                "amount": 1000,
            },
        )
        pos_invoice.contact_mobile = "093456543894"
        pos_invoice.currency = "KES"
        pos_invoice.save()

        pr = pos_invoice.create_payment_request()
        # test payment request creation
        self.assertEqual(pr.payment_gateway, "Mpesa-Payment")

        # submitting payment request creates integration requests with random id
        integration_req_ids = frappe.get_all(
            "Integration Request",
            filters={
                "reference_doctype": pr.doctype,
                "reference_docname": pr.name,
            },
            pluck="name",
        )

        # create random receipt nos and send it as response to callback handler
        mpesa_receipt_numbers = [
            frappe.utils.random_string(5) for d in integration_req_ids
        ]

        callback_response = get_payment_callback_payload(
            Amount=500,
            CheckoutRequestID=integration_req_ids[0],
            MpesaReceiptNumber=mpesa_receipt_numbers[0],
        )
        # handle response manually
        verify_transaction(**callback_response)
        # test completion of integration request
        integration_request = frappe.get_doc(
            "Integration Request", integration_req_ids[0]
        )
        self.assertEqual(integration_request.status, "Completed")

        # now one request is completed
        # second integration request fails
        # now retrying payment request should make only one integration request again
        pr = pos_invoice.create_payment_request()
        new_integration_req_ids = frappe.get_all(
            "Integration Request",
            filters={
                "reference_doctype": pr.doctype,
                "reference_docname": pr.name,
                "name": ["not in", integration_req_ids],
            },
            pluck="name",
        )

        self.assertEqual(len(new_integration_req_ids), 1)

        frappe.db.set_value("Customer", "_Test Customer", "default_currency", "")
        frappe.db.sql(
            "delete from `tabIntegration Request` where integration_request_service = 'Mpesa'"
        )
        pr.reload()
        pr.cancel()
        pr.delete()
        pos_invoice.delete()


def create_mpesa_settings(payment_gateway_name="Express", company=None):
    """The company decides where the gateway account and mode of payment land.

    on_update passes self.company straight to create_payment_gateway_account,
    so a settings document with none falls back to whatever the site's default
    company happens to be - which is not the company these tests post to.
    """
    if frappe.db.exists("Mpesa Settings", payment_gateway_name):
        doc = frappe.get_doc("Mpesa Settings", payment_gateway_name)
        if company and doc.company != company:
            doc.company = company
            doc.save(ignore_permissions=True)
        return doc

    doc = frappe.get_doc(
        dict(  # nosec
            doctype="Mpesa Settings",
            sandbox=1,
            payment_gateway_name=payment_gateway_name,
            company=company,
            consumer_key="5sMu9LVI1oS3oBGPJfh3JyvLHwZOdTKn",
            consumer_secret="VI1oS3oBGPJfh3JyvLHw",
            online_passkey="LVI1oS3oBGPJfh3JyvLHwZOd",
            # api_type and paybill_type are mandatory. Without them this only
            # ever worked on a site where the settings already existed, and
            # inserting one on a fresh site - CI - threw MandatoryError.
            api_type="MPesa Express",
            paybill_type="Buy Goods",
            till_number="174379",
        )
    )

    doc.insert(ignore_permissions=True)
    return doc


def _ensure_gateway_account(gateway: str, company: str) -> str:
    """A KES receipts account for this gateway, in this company.

    create_payment_gateway_account bails out as soon as *any* gateway account
    exists in the same currency, whatever company it belongs to, so a second
    company never gets one of its own.
    """
    from erpnext.setup.setup_wizard.operations.install_fixtures import (
        create_bank_account,
    )

    account = frappe.db.get_value(
        "Account", {"account_name": gateway, "company": company}, "name"
    )
    if not account:
        create_bank_account({"company_name": company, "bank_account": gateway})
        account = frappe.db.get_value(
            "Account", {"account_name": gateway, "company": company}, "name"
        )

    frappe.db.set_value("Account", account, "account_currency", "KES")

    existing = frappe.db.get_value(
        "Payment Gateway Account",
        {"payment_gateway": gateway, "company": company},
        "name",
    )
    doc = (
        frappe.get_doc("Payment Gateway Account", existing)
        if existing
        else frappe.new_doc("Payment Gateway Account")
    )
    doc.update(
        {
            "payment_gateway": gateway,
            "payment_account": account,
            "currency": "KES",
            "company": company,
            # The Payment Request takes its channel from here, and only a
            # Phone request reaches the STK push.
            "payment_channel": "Phone",
            "is_default": 0,
        }
    )
    doc.save(ignore_permissions=True)

    return account


def _ensure_mode_of_payment_account(
    mode_of_payment: str, company: str, account: str
) -> str:
    """Give the mode of payment a default account in this company.

    create_mode_of_payment only builds the accounts row when it creates the
    mode of payment. One that already exists - because another company's
    gateway was set up first - never gains a row for this company, and a POS
    Invoice paid through it is then refused for having no default account.
    """
    doc = frappe.get_doc("Mode of Payment", mode_of_payment)
    if not any(row.company == company for row in doc.accounts):
        doc.append("accounts", {"company": company, "default_account": account})
        doc.save(ignore_permissions=True)

    return account


def _test_cashier() -> str:
    """A user that exists only to hold this suite's open POS shift."""
    email = "mpesa-test-cashier@example.com"
    if not frappe.db.exists("User", email):
        user = frappe.new_doc("User")
        user.email = email
        user.first_name = "Mpesa Test Cashier"
        user.send_welcome_email = 0
        user.insert(ignore_permissions=True)
    return email


def get_test_account_balance_response():
    """Response received after calling the account balance API."""
    return {
        "ResultType": 0,
        "ResultCode": 0,
        "ResultDesc": "The service request has been accepted successfully.",
        "OriginatorConversationID": "10816-694520-2",
        "ConversationID": "AG_20200927_00007cdb1f9fb6494315",
        "TransactionID": "LGR0000000",
        "ResultParameters": {
            "ResultParameter": [
                {"Key": "ReceiptNo", "Value": "LGR919G2AV"},
                {"Key": "Conversation ID", "Value": "AG_20170727_00004492b1b6d0078fbe"},
                {"Key": "FinalisedTime", "Value": 20170727101415},
                {"Key": "Amount", "Value": 10},
                {"Key": "TransactionStatus", "Value": "Completed"},
                {"Key": "ReasonType", "Value": "Salary Payment via API"},
                {"Key": "TransactionReason"},
                {"Key": "DebitPartyCharges", "Value": "Fee For B2C Payment|KES|33.00"},
                {"Key": "DebitAccountType", "Value": "Utility Account"},
                {"Key": "InitiatedTime", "Value": 20170727101415},
                {"Key": "Originator Conversation ID", "Value": "19455-773836-1"},
                {"Key": "CreditPartyName", "Value": "254708374149 - John Doe"},
                {"Key": "DebitPartyName", "Value": "600134 - Safaricom157"},
            ]
        },
        "ReferenceData": {"ReferenceItem": {"Key": "Occasion", "Value": "aaaa"}},
    }


def get_payment_request_response_payload(Amount=500):
    """Response received after successfully calling the stk push process request API."""

    CheckoutRequestID = frappe.utils.random_string(10)

    return {
        "MerchantRequestID": "8071-27184008-1",
        "CheckoutRequestID": CheckoutRequestID,
        "ResultCode": 0,
        "ResultDesc": "The service request is processed successfully.",
        "CallbackMetadata": {
            "Item": [
                {"Name": "Amount", "Value": Amount},
                {"Name": "MpesaReceiptNumber", "Value": "LGR7OWQX0R"},
                {"Name": "TransactionDate", "Value": 20201006113336},
                {"Name": "PhoneNumber", "Value": 254723575670},
            ]
        },
    }


def get_payment_callback_payload(
    Amount=500,
    CheckoutRequestID="ws_CO_061020201133231972",
    MpesaReceiptNumber="LGR7OWQX0R",
):
    """Response received from the server as callback after calling the stkpush process request API."""
    return {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "19465-780693-1",
                "CheckoutRequestID": CheckoutRequestID,
                "ResultCode": 0,
                "ResultDesc": "The service request is processed successfully.",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": Amount},
                        {"Name": "MpesaReceiptNumber", "Value": MpesaReceiptNumber},
                        {"Name": "Balance"},
                        {"Name": "TransactionDate", "Value": 20170727154800},
                        {"Name": "PhoneNumber", "Value": 254721566839},
                    ]
                },
            }
        }
    }


def get_account_balance_callback_payload():
    """Response received from the server as callback after calling the account balance API."""
    return {
        "Result": {
            "ResultType": 0,
            "ResultCode": 0,
            "ResultDesc": "The service request is processed successfully.",
            "OriginatorConversationID": "16470-170099139-1",
            "ConversationID": "AG_20200927_00007cdb1f9fb6494315",
            "TransactionID": "OIR0000000",
            "ResultParameters": {
                "ResultParameter": [
                    {
                        "Key": "AccountBalance",
                        "Value": "Working Account|KES|481000.00|481000.00|0.00|0.00",
                    },
                    {"Key": "BOCompletedTime", "Value": 20200927234123},
                ]
            },
            "ReferenceData": {
                "ReferenceItem": {
                    "Key": "QueueTimeoutURL",
                    "Value": "https://internalsandbox.safaricom.co.ke/mpesa/abresults/v1/submit",
                }
            },
        }
    }
