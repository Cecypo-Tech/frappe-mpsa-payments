from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from .m_pesa_api import (
    MpesaTokenError,
    confirmation,
    get_mpesa_draft_c2b_payments,
    get_mpesa_mode_of_payment,
    get_token,
    initiate_stk_push,
    submit_mpesa_payment,
    transaction_status_error_callback,
    validation,
)
from .mpesa_response_handler import transaction_status_on_success

EXPRESS = "Mpesa Express Request"
REQUEST_CLASS = (
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_express_request"
    ".mpesa_express_request.MpesaExpressRequest"
)


class TestMPesaAPI(FrappeTestCase):
    @patch("requests.get")
    def test_get_token(self, mock_get):
        mock_response = Mock()
        mock_response.json.return_value = {"access_token": "dummy_token"}
        mock_get.return_value = mock_response

        app_key = "dummy_key"
        app_secret = "dummy_secret"
        base_url = "https://example.com"

        token = get_token(app_key, app_secret, base_url)

        self.assertEqual(token, "dummy_token")

    @patch("requests.get")
    def test_get_token_turns_an_empty_400_into_a_readable_error(self, mock_get):
        """Safaricom answers a wrong key or secret with an empty text/plain 400.

        get_token used to call .json() on it regardless, and the JSONDecodeError
        escaped as an HTTP 500 the desk could only show as a bare "Not Saved".
        """
        refusal = Mock(status_code=400, text="", headers={"content-type": "text/plain"})
        refusal.json.side_effect = ValueError("Expecting value")
        mock_get.return_value = refusal

        with self.assertRaises(MpesaTokenError) as caught:
            get_token("bad_key", "bad_secret", "https://sandbox.safaricom.co.ke")

        self.assertIn("400", str(caught.exception))

    @patch("requests.get")
    def test_get_token_surfaces_safaricoms_own_error_message(self, mock_get):
        refusal = Mock(status_code=400, headers={"content-type": "application/json"})
        refusal.json.return_value = {
            "requestId": "1",
            "errorCode": "400.008.01",
            "errorMessage": "Invalid Authentication passed",
        }
        mock_get.return_value = refusal

        with self.assertRaises(MpesaTokenError) as caught:
            get_token("bad_key", "bad_secret", "https://api.safaricom.co.ke")

        self.assertIn("Invalid Authentication passed", str(caught.exception))

    @patch("requests.get")
    def test_get_token_turns_a_transport_error_into_a_readable_error(self, mock_get):
        """A timeout or refused connection is as much "no token" as a 400."""
        mock_get.side_effect = requests.exceptions.ConnectionError("no route to host")

        with self.assertRaises(MpesaTokenError) as caught:
            get_token("key", "secret", "https://api.safaricom.co.ke")

        self.assertIn("no route to host", str(caught.exception))

    @patch("requests.get")
    def test_get_token_names_an_html_block_page_for_what_it_is(self, mock_get):
        """Safaricom sits behind Imperva; a blocked request gets an HTML 403."""
        block = Mock(
            status_code=403,
            text="<html>Request unsuccessful. Incapsula incident ID: 1-2</html>",
            headers={"content-type": "text/html"},
        )
        block.json.side_effect = ValueError("Expecting value")
        mock_get.return_value = block

        with self.assertRaises(MpesaTokenError) as caught:
            get_token("key", "secret", "https://api.safaricom.co.ke")

        self.assertIn("403", str(caught.exception))
        self.assertNotIn("<html>", str(caught.exception))

    def test_confirmation(self):
        # Test accepted case
        args = {
            "TransactionType": "Payment",
            "TransID": "123456",
            "TransTime": "2024-05-01T12:00:00",
            "TransAmount": 100.0,
            "BusinessShortCode": "123456",
            "BillRefNumber": "BILL001",
            "InvoiceNumber": "INV001",
            "OrgAccountBalance": 500.0,
            "ThirdPartyTransID": "789012",
            "MSISDN": "1234567890",
            "FirstName": "John",
            "MiddleName": "Doe",
            "LastName": "Smith",
        }
        result = confirmation(**args)
        self.assertEqual(result["ResultCode"], 0)
        self.assertEqual(result["ResultDesc"], "Accepted")

        # Test rejected case
        args["TransAmount"] = "invalid_amount"
        result = confirmation(**args)
        self.assertEqual(result["ResultCode"], 1)
        self.assertEqual(result["ResultDesc"], "Rejected")

    def test_validation(self):
        # Test validation always returns accepted
        result = validation()
        self.assertEqual(result["ResultCode"], 0)
        self.assertEqual(result["ResultDesc"], "Accepted")

    def _stk_payload(self, mock_process, **overrides):
        """Fire initiate_stk_push with process_request stubbed, return the payload."""
        # Mirrors initiate_request(): every key is sent, None included.
        args = {
            "payment_gateway": "Mpesa-898102",
            "phone_number": "254727870777",
            "request_amount": 1,
            "doctype": "Mpesa Express Request",
            "document_name": "MEXP-26-08-000004",
            "reference_name": None,
            "account_reference": None,
        }
        args.update(overrides)

        initiate_stk_push(**args)

        self.assertTrue(
            mock_process.called,
            "process_request was never reached - the push was not built",
        )
        return mock_process.call_args.kwargs["payload"]

    @patch("frappe.get_doc")
    @patch(
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.build_callback_url"
    )
    @patch("frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.process_request")
    def test_stk_push_account_reference_never_null(
        self, mock_process, mock_callback, mock_get_doc
    ):
        """A standalone push - no linked document - must still carry a reference.

        Regression: initiate_request() always passes the reference_name key, with
        None as its value for a push that isn't tied to an invoice. dict.get()
        only falls back to its default when the key is absent, so the default
        never applied and Daraja rejected the null AccountReference with
        400.002.02 "Bad Request - Invalid Remarks".
        """
        mock_callback.return_value = "https://example.com/callback"
        settings = Mock()
        settings.name = "898102"
        settings.business_shortcode = "898102"
        settings.paybill_type = "Pay Bill"
        settings.get_password.return_value = "passkey"
        mock_get_doc.return_value = settings

        # reference_name present but None - the exact shape initiate_request sends.
        payload = self._stk_payload(mock_process, reference_name=None)

        self.assertIsNotNone(payload["AccountReference"])
        self.assertIsNotNone(payload["TransactionDesc"])
        self.assertEqual(payload["AccountReference"], "MEXP-26-08-000004")
        self.assertEqual(payload["TransactionDesc"], "MEXP-26-08-000004")

    @patch("frappe.get_doc")
    @patch(
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.build_callback_url"
    )
    @patch("frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.process_request")
    def test_stk_push_reference_name_wins_over_document_name(
        self, mock_process, mock_callback, mock_get_doc
    ):
        """A linked document still supplies the reference - unchanged behaviour."""
        mock_callback.return_value = "https://example.com/callback"
        settings = Mock()
        settings.name = "898102"
        settings.business_shortcode = "898102"
        settings.paybill_type = "Pay Bill"
        settings.get_password.return_value = "passkey"
        mock_get_doc.return_value = settings

        payload = self._stk_payload(mock_process, reference_name="SINV-00042")

        self.assertEqual(payload["AccountReference"], "SINV-00042")

    @patch("frappe.get_doc")
    @patch(
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.build_callback_url"
    )
    @patch("frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.process_request")
    def test_stk_push_prefers_caller_supplied_account_reference(
        self, mock_process, mock_callback, mock_get_doc
    ):
        """The Pay Bill account number wins over anything we could infer.

        A POS supplies this before the sale exists as a document, and it is what
        the customer's statement shows and what returns as BillRefNumber.
        """
        mock_callback.return_value = "https://example.com/callback"
        settings = Mock()
        settings.name = "898102"
        settings.business_shortcode = "898102"
        settings.paybill_type = "Pay Bill"
        settings.get_password.return_value = "passkey"
        mock_get_doc.return_value = settings

        payload = self._stk_payload(
            mock_process,
            account_reference="ACC-2026-001",
            reference_name="SINV-00042",
        )

        self.assertEqual(payload["AccountReference"], "ACC-2026-001")
        self.assertEqual(payload["TransactionDesc"], "ACC-2026-001")

    @patch("frappe.get_doc")
    @patch(
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.build_callback_url"
    )
    @patch("frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.process_request")
    def test_stk_push_falls_back_when_nothing_identifies_the_push(
        self, mock_process, mock_callback, mock_get_doc
    ):
        """With neither reference nor document name, fall back to a literal."""
        mock_callback.return_value = "https://example.com/callback"
        settings = Mock()
        settings.name = "898102"
        settings.business_shortcode = "898102"
        settings.paybill_type = "Pay Bill"
        settings.get_password.return_value = "passkey"
        mock_get_doc.return_value = settings

        payload = self._stk_payload(
            mock_process, reference_name=None, document_name=None
        )

        self.assertEqual(payload["AccountReference"], "Online Payment")

    @patch("frappe.get_all")
    def test_get_mpesa_mode_of_payment(self, mock_get_all):
        mock_get_all.return_value = [frappe._dict(mode_of_payment="Cash")]

        company = "Test Company"

        modes_of_payment = get_mpesa_mode_of_payment(company)

        self.assertEqual(modes_of_payment, ["Cash"])

    @patch("frappe.get_all")
    def test_get_mpesa_draft_payments(self, mock_get_all):
        mock_get_all.return_value = [{"name": "MP001", "amount": 100.0}]

        company = "Test Company"
        mode_of_payment = "Cash"

        payments = get_mpesa_draft_c2b_payments(
            company, mode_of_payment=mode_of_payment
        )

        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]["name"], "MP001")
        self.assertEqual(payments[0]["amount"], 100.0)

    @patch(
        "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.process_mpesa_payment"
    )
    @patch("frappe.get_doc")
    def test_submit_mpesa_payment(self, mock_get_doc, mock_process):
        """It hands back the Payment Entry the register produced.

        Mocking frappe.get_doc alone was not enough: process_mpesa_payment
        went on to query the register by a Mock attribute, which reached the
        database as SQL and failed there.
        """
        mock_process.return_value = frappe._dict(payment_entry="PE001")
        payment_entry = Mock()
        mock_get_doc.return_value = payment_entry

        result = submit_mpesa_payment("MP001", "Test Customer")

        self.assertIs(result, payment_entry)
        mock_process.assert_called_once_with(
            "MP001", "Test Customer", submit_payment=True
        )
        mock_get_doc.assert_called_once_with("Payment Entry", "PE001")


class TestTransactionStatusAnswers(FrappeTestCase):
    """Only Safaricom's answer about a push changes it.

    The status query - check_transaction_status, also the desk's Check Transaction
    Status button - marked the push Failed on any error reply, "The transaction is
    being processed" included, which unlocked a second prompt while the customer
    could still pay the first. And it ran the paid handling for every answer.
    """

    def setUp(self):
        self.addCleanup(frappe.db.rollback)
        # transaction_status_on_success raises this for the rest of the request.
        self.addCleanup(frappe.flags.pop, "ignore_permissions", None)
        self.name = f"_Test STK Status {frappe.generate_hash(length=6)}"
        frappe.get_doc(
            {
                "doctype": EXPRESS,
                "name": self.name,
                "docstatus": 1,
                "status": "In Progress",
                "phone_number": "254700000123",
                "currency": "KES",
                "base_amount": 450,
                "amount": 450,
            }
        ).db_insert()

    def _push(self):
        return frappe.db.get_value(EXPRESS, self.name, ["status", "result_desc"])

    def _answer(self, **response):
        """Safaricom's 200 reply, through the success handler."""
        with patch(f"{REQUEST_CLASS}.reconcile_payment") as reconcile:
            transaction_status_on_success(response=response, document_name=self.name)
        return reconcile

    def _error(self, **response):
        """Safaricom's error reply (any non-2xx), through the error handler."""
        transaction_status_error_callback(
            response=response, payload={}, document_name=self.name
        )

    def test_paid_completes_the_push_and_reconciles_it(self):
        reconcile = self._answer(
            ResultCode="0", ResultDesc="The service request is processed successfully."
        )

        self.assertEqual(
            self._push(),
            ("Completed", "The service request is processed successfully."),
        )
        reconcile.assert_called_once()

    def test_a_cancelled_prompt_fails_the_push_and_reconciles_nothing(self):
        reconcile = self._answer(
            ResultCode="1032", ResultDesc="Request cancelled by user"
        )

        self.assertEqual(self._push(), ("Failed", "Request cancelled by user"))
        reconcile.assert_not_called()

    def test_no_result_yet_leaves_the_push_waiting(self):
        for response in (
            {
                "ResultCode": "4999",
                "ResultDesc": "The transaction is still under processing",
            },
            {"ResponseCode": "0"},
        ):
            with self.subTest(response=response):
                reconcile = self._answer(**response)

                self.assertEqual(self._push(), ("In Progress", None))
                reconcile.assert_not_called()

    def test_being_processed_leaves_the_push_waiting(self):
        self._error(
            requestId="1",
            errorCode="500.001.1001",
            errorMessage="The transaction is being processed",
        )

        self.assertEqual(self._push(), ("In Progress", None))

    def test_an_authentication_error_leaves_the_push_waiting(self):
        self._error(
            requestId="2", errorCode="404.001.03", errorMessage="Invalid Access Token"
        )

        self.assertEqual(self._push(), ("In Progress", None))
