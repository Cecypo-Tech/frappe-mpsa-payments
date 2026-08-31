"""Pull transaction regression tests.

Deliberately self-contained: these mock at the frappe boundary rather than
building Mpesa Settings / Company / POS Profile fixtures, so they run on any
site. test_mpesa_settings.py needs a specific company fixture and cannot run
on most benches, which is how the 1001-reported-as-success bug survived.
"""

import re
import unittest
from unittest.mock import patch

import frappe
import requests

from frappe_mpsa_payments.frappe_mpsa_payments.api import m_pesa_api as api
from frappe_mpsa_payments.frappe_mpsa_payments.api import (
    mpesa_response_handler as handler,
)
from frappe_mpsa_payments.frappe_mpsa_payments.connectors import connectors

SETTINGS_NAME = "_Pull Test"


class _StubSettings:
    name = SETTINGS_NAME
    sandbox = 0
    business_shortcode = "898048"
    till_number = None


class TestPullTransactionResponses(unittest.TestCase):
    """Safaricom signals failure in the body while returning HTTP 200."""

    def _run(self, response, batch=False):
        published = []
        logged = []

        frappe.flags[handler.BULK_PULL_BATCH_FLAG] = batch
        try:
            with (
                patch.object(handler.frappe, "get_doc", return_value=_StubSettings()),
                patch.object(handler, "record_pull_outcome"),
                patch.object(
                    handler, "publish_pull_result", side_effect=published.append
                ),
                patch.object(
                    handler.frappe,
                    "log_error",
                    side_effect=lambda *a, **k: logged.append(k.get("title")),
                ),
            ):
                handler.pull_transaction_on_success(
                    response=response,
                    document_name=SETTINGS_NAME,
                    settings_name=SETTINGS_NAME,
                    integration_request=None,
                    payload={"ShortCode": "898048"},
                )
        finally:
            frappe.flags[handler.BULK_PULL_BATCH_FLAG] = False

        return published[0] if published else {}, logged

    def test_1001_is_not_reported_as_a_successful_import(self):
        msg, logged = self._run(
            {
                "ResponseCode": "1001",
                "ResponseMessage": "No records found or Organization Name not available",
            }
        )
        self.assertEqual(msg.get("status"), "warning")
        self.assertEqual(msg.get("response_code"), "1001")
        self.assertEqual(msg.get("count"), 0)
        self.assertIn("1001", msg.get("message", ""))
        self.assertNotIn("record(s) imported", msg.get("message", ""))
        self.assertEqual(len(logged), 1)

    def test_other_error_codes_report_as_error(self):
        msg, _ = self._run(
            {"ResponseCode": "500.003.02", "ResponseMessage": "Internal server error"}
        )
        self.assertEqual(msg.get("status"), "error")
        self.assertEqual(msg.get("count"), 0)

    def test_1000_with_empty_response_is_a_quiet_window(self):
        msg, _ = self._run(
            {"ResponseCode": "1000", "ResponseMessage": "Success", "Response": []}
        )
        self.assertIn("0 record(s) imported", msg.get("message", ""))
        self.assertEqual(msg.get("count"), 0)

    def test_batch_mode_suppresses_the_per_shortcode_log(self):
        """Dozens of shortcodes, pulled hourly, made these logs unreadable."""
        msg, logged = self._run(
            {"ResponseCode": "1001", "ResponseMessage": "No records found"}, batch=True
        )
        self.assertEqual(logged, [])
        self.assertEqual(msg.get("status"), "warning")


class TestPullPagination(unittest.TestCase):
    """Safaricom paginates; we used to import only the first page."""

    def _walk(self, total_pages):
        requested = []

        def fake_request(**kwargs):
            requested.append(int(kwargs["payload"]["OffSetValue"]))
            kwargs["success_callback"](
                response={
                    "ResponseCode": "1000",
                    "Response": [],
                    "TotalPages": total_pages,
                },
                document_name=SETTINGS_NAME,
                settings_name=SETTINGS_NAME,
                integration_request=None,
            )
            return {"ResponseCode": "1000", "TotalPages": total_pages}

        with (
            patch.object(api, "process_request", side_effect=fake_request),
            patch.object(api.frappe, "publish_realtime"),
            patch.object(handler.frappe, "get_doc", return_value=_StubSettings()),
            patch.object(handler, "record_pull_outcome"),
            patch.object(handler.frappe, "log_error"),
        ):
            api.execute_pull_transactions(
                mpesa_settings=SETTINGS_NAME,
                payload={"ShortCode": "898048", "OffSetValue": "0"},
            )
        return requested

    def test_every_page_is_fetched(self):
        self.assertEqual(self._walk(4), [0, 1, 2, 3])

    def test_single_page_stops_immediately(self):
        self.assertEqual(self._walk(1), [0])

    def test_runaway_total_pages_is_capped(self):
        self.assertEqual(len(self._walk(9999)), api.PULL_MAX_PAGES)


class TestBulkPullCircuitBreaker(unittest.TestCase):
    """When Safaricom is unreachable, stop rather than spend the hour on it."""

    def _bulk(self, outcomes):
        """Run a bulk pull where each shortcode fails/succeeds per `outcomes`.

        `outcomes` maps position -> "network", "other" or None (success).
        Returns the shortcodes actually attempted.
        """
        names = [f"SC{i}" for i in range(len(outcomes))]
        attempted = []

        def fake_pull(mpesa_settings, payload):
            attempted.append(mpesa_settings)
            outcome = outcomes[names.index(mpesa_settings)]
            if outcome is None:
                frappe.flags[api.PULL_NETWORK_FAILURE_FLAG] = False
            else:
                api._handle_pull_failure(
                    mpesa_settings,
                    requests.exceptions.ConnectTimeout("timed out")
                    if outcome == "network"
                    else ValueError("bad config"),
                )

        with (
            patch.object(api, "execute_pull_transactions", side_effect=fake_pull),
            patch.object(api, "build_pull_payload", return_value={"ShortCode": "x"}),
            patch.object(api, "record_pull_outcome"),
            patch.object(api, "publish_pull_result"),
            patch.object(api.frappe, "log_error"),
            patch.object(api.frappe, "publish_realtime"),
            patch.object(api.frappe.db, "commit"),
        ):
            api.execute_bulk_pull_transactions(
                settings_names=names, start_date="2026-01-01", end_date="2026-01-02"
            )
        return attempted, names

    def test_stops_after_consecutive_network_failures(self):
        threshold = api.BULK_PULL_MAX_CONSECUTIVE_NETWORK_FAILURES
        attempted, names = self._bulk(["network"] * (threshold + 5))
        self.assertEqual(len(attempted), threshold)
        self.assertLess(len(attempted), len(names))

    def test_a_success_resets_the_counter(self):
        threshold = api.BULK_PULL_MAX_CONSECUTIVE_NETWORK_FAILURES
        # fail up to one short of the threshold, succeed, then fail again
        outcomes = (
            ["network"] * (threshold - 1) + [None] + ["network"] * (threshold - 1)
        )
        attempted, names = self._bulk(outcomes)
        self.assertEqual(len(attempted), len(names), "must not trip on a flaky one")

    def test_non_network_errors_never_trip_it(self):
        threshold = api.BULK_PULL_MAX_CONSECUTIVE_NETWORK_FAILURES
        attempted, names = self._bulk(["other"] * (threshold + 3))
        self.assertEqual(len(attempted), len(names))


class TestConnectorTimeouts(unittest.TestCase):
    """A hung Safaricom connection must not pin a worker indefinitely."""

    def test_default_is_a_connect_read_tuple(self):
        c = connectors.MpesaConnector(settings_name="_Pull Test")
        self.assertEqual(c._timeout, (10, 30))

    def test_connect_budget_is_shorter_than_read_budget(self):
        connect, read = connectors.DEFAULT_TIMEOUT
        self.assertLess(connect, read)

    def test_a_bare_number_sets_the_read_budget_only(self):
        c = connectors.MpesaConnector(settings_name="_Pull Test")
        c._set_timeout(120)
        self.assertEqual(c._timeout, (connectors.CONNECT_TIMEOUT_SECONDS, 120))

    def test_a_tuple_is_taken_as_given(self):
        c = connectors.MpesaConnector(settings_name="_Pull Test")
        c._set_timeout((5, 45))
        self.assertEqual(c._timeout, (5, 45))

    def test_every_outgoing_request_is_bounded(self):
        """Including authenticate() - it used to have no timeout at all."""
        import inspect

        src = inspect.getsource(connectors)
        call_sites = re.findall(r"requests\.(?:get|post|put|patch)\(", src)
        self.assertTrue(call_sites, "expected to find outgoing requests")
        self.assertEqual(
            src.count("timeout=self._timeout"),
            len(call_sites),
            "a requests call is missing timeout=self._timeout",
        )
