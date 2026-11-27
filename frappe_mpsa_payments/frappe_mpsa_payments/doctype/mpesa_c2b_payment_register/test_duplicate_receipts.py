"""One M-Pesa receipt, one live register row.

The C2B callback and a Pull Transactions both insert register rows, each after an
"exists?" check. Arriving a second apart, neither saw the other's uncommitted row,
and the receipt landed twice: a draft copy the till offered as unspent money.
"""

import threading
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_mpsa_payments.frappe_mpsa_payments.api import m_pesa_api
from frappe_mpsa_payments.frappe_mpsa_payments.api import (
    mpesa_response_handler as handler,
)

from . import mpesa_c2b_payment_register as reg
from .mpesa_c2b_payment_register import DuplicateReceiptError, claim_receipt

REGISTER = "Mpesa C2B Payment Register"
SHORTCODE = "DUPTEST"


def _row(transid, **fields):
    return frappe.get_doc(
        {
            "doctype": REGISTER,
            "transid": transid,
            "transamount": 100,
            "businessshortcode": SHORTCODE,
            "billrefnumber": "DUP-REF",
            **fields,
        }
    ).insert(ignore_permissions=True)


def _count(transid):
    return frappe.db.count(REGISTER, {"transid": transid, "docstatus": ["<", 2]})


class TestOneLiveRowPerReceipt(FrappeTestCase):
    """Same connection; everything is rolled back after each test."""

    def test_second_live_row_refused(self):
        _row("DUPTEST01")
        with self.assertRaises(DuplicateReceiptError):
            _row("DUPTEST01")
        self.assertEqual(_count("DUPTEST01"), 1)

    def test_cancelled_twin_does_not_block(self):
        first = _row("DUPTEST02")
        frappe.db.set_value(REGISTER, first.name, "docstatus", 2)
        _row("DUPTEST02")
        self.assertEqual(_count("DUPTEST02"), 1)

    def test_blank_transid_not_locked(self):
        with patch.object(reg, "_get_lock") as get_lock:
            _row("")
            _row("")
        get_lock.assert_not_called()

    def test_callback_retry_is_quiet(self):
        data = {
            "transactiontype": "Pay Bill",
            "transid": "DUPTEST07",
            "transamount": 100,
            "businessshortcode": SHORTCODE,
            "billrefnumber": "DUP-REF",
        }
        logs_before = frappe.db.count("Error Log", {"method": "Delayed C2B Insert Error"})
        # The test's transaction holds the first row, so neither call may end it.
        with (
            patch.object(m_pesa_api.time, "sleep"),
            patch.object(frappe.db, "commit"),
            patch.object(frappe.db, "rollback"),
        ):
            m_pesa_api.delayed_insert_c2b(dict(data))
            m_pesa_api.delayed_insert_c2b(dict(data))
        self.assertEqual(_count("DUPTEST07"), 1)
        self.assertEqual(
            frappe.db.count("Error Log", {"method": "Delayed C2B Insert Error"}), logs_before
        )


class _StubSettings:
    name = "_Dup Pull Test"
    sandbox = 0
    business_shortcode = SHORTCODE
    till_number = None


class _LosingRow:
    """A register row whose insert loses the race to the callback's row."""

    def insert(self, **kwargs):
        raise DuplicateReceiptError("DUPTEST08")


class TestPullLosesTheRace(FrappeTestCase):
    def test_pull_counts_duplicate_as_skipped(self):
        published = []
        real_exists = frappe.db.exists

        def exists(doctype, *args, **kwargs):
            # The callback's row is not committed yet: the pull's own check misses it.
            return False if doctype == REGISTER else real_exists(doctype, *args, **kwargs)

        with (
            patch.object(handler.frappe, "get_doc", return_value=_StubSettings()),
            patch.object(handler.frappe, "new_doc", return_value=_LosingRow()),
            patch.object(handler.frappe.db, "exists", side_effect=exists),
            patch.object(handler.frappe.db, "commit"),
            patch.object(handler.frappe, "log_error"),
            patch.object(handler, "record_pull_outcome"),
            patch.object(handler, "publish_pull_result", side_effect=published.append),
        ):
            handler.pull_transaction_on_success(
                response={
                    "ResponseCode": "1000",
                    "Response": [[{"transactionId": "DUPTEST08", "amount": "100"}]],
                },
                document_name=_StubSettings.name,
                settings_name=_StubSettings.name,
                payload={},
            )

        self.assertEqual(published[0]["skipped"], 1)
        self.assertEqual(published[0]["failed"], 0)


def _in_connection(site, sites_path, work, results, key):
    """Run work() on a connection of its own, committing what it did."""
    frappe.init(site=site, sites_path=sites_path)
    frappe.connect()
    frappe.set_user("Administrator")
    try:
        results[key] = work()
        frappe.db.commit()
    except Exception as e:
        results[key] = e
        if getattr(frappe.local, "db", None):
            frappe.db.rollback()
    finally:
        frappe.destroy()


class TestReceiptLockAcrossConnections(FrappeTestCase):
    """Real threads, real connections: rows are committed and purged by hand."""

    TRANSIDS = ("DUPTEST03", "DUPTEST04", "DUPTEST05")

    def tearDown(self):
        frappe.db.rollback()
        frappe.db.delete(REGISTER, {"transid": ["in", self.TRANSIDS]})
        frappe.db.commit()

    def _thread(self, work, results, key):
        t = threading.Thread(
            target=_in_connection,
            args=(frappe.local.site, frappe.local.sites_path, work, results, key),
        )
        t.start()
        return t

    def test_concurrent_inserts_one_wins(self):
        real_after_insert = reg.MpesaC2BPaymentRegister.after_insert

        def slow_after_insert(doc):
            real_after_insert(doc)
            time.sleep(1.5)  # the callback's insert is still uncommitted

        results = {}
        with patch.object(reg.MpesaC2BPaymentRegister, "after_insert", slow_after_insert):
            first = self._thread(lambda: _row("DUPTEST03").name, results, "first")
            time.sleep(0.3)
            second = self._thread(lambda: _row("DUPTEST03").name, results, "second")
            first.join(20)
            second.join(20)

        self.assertIsInstance(results["first"], str)
        self.assertIsInstance(results["second"], DuplicateReceiptError)
        self.assertEqual(_count("DUPTEST03"), 1)

    def test_rollback_releases_lock(self):
        claim_receipt("DUPTEST04")
        frappe.db.rollback()

        results = {}
        started = time.monotonic()
        with patch.object(reg, "RECEIPT_LOCK_TIMEOUT", 3):
            self._thread(lambda: claim_receipt("DUPTEST04"), results, "other").join(10)
        self.assertIsNone(results["other"])
        self.assertLess(time.monotonic() - started, 2)

    def test_lock_timeout_raises(self):
        claim_receipt("DUPTEST05")
        results = {}
        with patch.object(reg, "RECEIPT_LOCK_TIMEOUT", 1):
            self._thread(lambda: claim_receipt("DUPTEST05"), results, "other").join(10)
        self.assertIsInstance(results["other"], frappe.QueryTimeoutError)
