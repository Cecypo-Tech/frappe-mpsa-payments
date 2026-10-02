"""Generate ``sample_statement.xls``, a synthetic M-Pesa statement fixture.

Every name, phone number, receipt and account number below is **fictional**.
Nothing in this file is derived from a real statement -- real exports contain
customer names and partially masked phone numbers and must never be committed.

The generated workbook mirrors the real export's structure exactly:

* a preamble of label/value pairs (deliberately NOT at fixed row offsets that
  the parser is allowed to assume),
* a ``Receipt No.`` column header row,
* data rows where each customer payment appears twice -- once as a
  ``Pay Bill Charge`` row and once as the real ``Paid In`` row.

Run directly to regenerate the fixture::

    python -m frappe_mpsa_payments.tests.fixtures.make_fixture
"""

from __future__ import annotations

import os

SHEET_NAME = "transaction"
FIXTURE_NAME = "sample_statement.xls"

ACCOUNT_HOLDER = "Fictional Traders Ltd - Testville"
SHORT_CODE = "123456"
PERIOD_FROM = "01-01-2020 00:00:00"
PERIOD_TO = "04-01-2020 23:59:59"
STATEMENT_PERIOD = f"{PERIOD_FROM} to {PERIOD_TO}"

COLUMN_HEADERS = [
    "Receipt No.",
    "Completion Time",
    "Initiation Time",
    "Details",
    "Transaction Status",
    "Paid In",
    "Withdrawn",
    "Balance",
    "Balance Confirmed",
    "Reason Type",
    "Other Party Info",
    "Linked Transaction ID",
    "A/C No.",
    "Currency",
]

#: (receipt, completion time, amount, account no, reason type, other party info)
#:
#: The four ``Other Party Info`` shapes exercised here mirror the shapes that
#: occur in real exports: masked middle name, no middle name at all, lowercase
#: names, and a short (non-country-code) msisdn prefix.
PAYMENTS = [
    (
        "TEST0000A1",
        "04-01-2020 17:16:40",
        7800.0,
        "850060",
        "Pay Utility",
        "25470****111 - ALICE **** EXAMPLE",
    ),
    (
        "TEST0000B2",
        "04-01-2020 16:15:42",
        27573.0,
        "850060",
        "Pay Utility",
        "010****222 - bob **** sample",
    ),
    (
        "TEST0000C3",
        "03-01-2020 15:02:20",
        1200.5,
        "750007",
        "Pay Utility with OD via STK",
        "25470****333 - CAROL FICTION",
    ),
    (
        "TEST0000D4",
        "03-01-2020 09:41:05",
        450.0,
        "750122",
        "Pay Utility",
        "070****444 - DAVE **** MADEUP",
    ),
    (
        "TEST0000E5",
        "02-01-2020 11:30:00",
        15000.0,
        "850002",
        "Pay Utility",
        "25470****555 - ERIN **** VAN DER TEST",
    ),
    (
        "TEST0000F6",
        "01-01-2020 08:05:15",
        99.25,
        "750942",
        "Pay Utility with OD via STK",
        "25470****666 - FRANK",
    ),
]

#: Rows that carry no ``Paid In`` value at all -- the real export emits these
#: for settlements to the organisation account.
SETTLEMENTS = [
    ("TEST0000S1", "04-01-2020 18:00:00", -50000.0),
    ("TEST0000S2", "02-01-2020 18:00:00", -25000.0),
]

TOTAL_PAID_IN = round(sum(payment[2] for payment in PAYMENTS), 2)
CURRENCY = "KES"


def build_rows(duplicate_receipt: str | None = None) -> list[list[object]]:
    """Build the full sheet as a list of rows of raw cell values.

    Shared by the .xls writer and by the in-memory fake sheet used in tests, so
    both exercise byte-for-byte the same layout.

    :param duplicate_receipt: when given, an extra genuine payment row is
            appended reusing this receipt number, so tests can assert on
            ``duplicate_receipts``.
    """

    width = len(COLUMN_HEADERS)

    def pad(values: list[object]) -> list[object]:
        return list(values) + [""] * (width - len(values))

    rows: list[list[object]] = [
        pad(["Account Holder:", ACCOUNT_HOLDER]),
        pad(["Short Code:", SHORT_CODE]),
        pad(["Account:", "Utility Account"]),
        pad(["Time Period:", "From", PERIOD_FROM, "To", PERIOD_TO]),
        pad(
            [
                "Operator:",
                "Tester",
                "Organization:",
                ACCOUNT_HOLDER,
                "Date of Report:",
                "05-01-2020 07:00:00",
            ]
        ),
        pad(
            [
                "Opening Balance:",
                0.0,
                "Closing Balance:",
                1234.56,
                "Available Balance:",
                "",
                "Total Paid In:",
                TOTAL_PAID_IN,
                "Total Withdrawn:",
                -75000.0,
            ]
        ),
        pad(list(COLUMN_HEADERS)),
    ]

    balance = 0.0

    def payment_pair(receipt, completion, amount, account_no, reason, other_party):
        nonlocal balance
        charge = -round(amount * 0.0027, 2)
        out = [
            pad(
                [
                    receipt,
                    completion,
                    completion,
                    "Pay Bill Charge",
                    "Completed",
                    "",  # blank Paid In, exactly as the real export emits it
                    charge,
                    balance,
                    "true",
                    reason,
                    "SP",
                    "",
                    "",
                    CURRENCY,
                ]
            ),
            pad(
                [
                    receipt,
                    completion,
                    completion,
                    f"Pay Bill from {other_party} Acc. {account_no}",
                    "Completed",
                    amount,
                    "",
                    balance - charge,
                    "true",
                    reason,
                    other_party,
                    "",
                    account_no,
                    CURRENCY,
                ]
            ),
        ]
        balance += amount
        return out

    for payment in PAYMENTS:
        rows.extend(payment_pair(*payment))

    for receipt, completion, amount in SETTLEMENTS:
        rows.append(
            pad(
                [
                    receipt,
                    completion,
                    completion,
                    "Utility Account to Organization Settlement",
                    "Completed",
                    "",
                    amount,
                    balance,
                    "true",
                    "Settlement",
                    "",
                    "",
                    "",
                    CURRENCY,
                ]
            )
        )

    if duplicate_receipt:
        source = next(
            payment for payment in PAYMENTS if payment[0] == duplicate_receipt
        )
        rows.extend(payment_pair(*source))

    return rows


def write_xls(path: str, duplicate_receipt: str | None = None) -> str:
    """Write the fixture workbook to ``path`` using ``xlwt``."""

    import xlwt

    book = xlwt.Workbook()
    sheet = book.add_sheet(SHEET_NAME)
    for row_index, row in enumerate(build_rows(duplicate_receipt=duplicate_receipt)):
        for col_index, value in enumerate(row):
            sheet.write(row_index, col_index, value)
    book.save(path)
    return path


def write_headerless_xls(path: str) -> str:
    """Write a workbook that has no ``Receipt No.`` header row at all."""

    import xlwt

    book = xlwt.Workbook()
    sheet = book.add_sheet("something-else")
    for row_index, row in enumerate(
        [["Account Holder:", "Nobody"], ["Just", "some", "junk"]]
    ):
        for col_index, value in enumerate(row):
            sheet.write(row_index, col_index, value)
    book.save(path)
    return path


# --------------------------------------------------------------------------
# The newer "Statements for <shortcode> from <date> to <date>" export
#
# No preamble: the header is the first row, amounts are text with thousands
# separators, and the payer, account and transaction type are packed into
# ``Details``.  Safaricom-internal transfers (Utility -> Settlement ->
# MMF) appear as Paid In / Withdrawn pairs, and the real money leaving M-Pesa
# is an "Agency Withdrawal of funds to Bank" row.  All values are fictional.
# --------------------------------------------------------------------------

NEW_SHORT_CODE = "654321"
NEW_PERIOD = "2020-01-04 to 2020-01-04"
NEW_FILE_NAME = f"Statements for {NEW_SHORT_CODE} from 2020-01-04 to 2020-01-04"

NEW_COLUMN_HEADERS = [
    "Receipt No",
    "Completion Time",
    "Details",
    "Transaction Status",
    "Paid In (KSHs)",
    "Withdrawn (KSHs)",
    "Balance (KSHs)",
]

#: (receipt, completion, details, amount, expected parse) -- one per Details shape.
NEW_PAYMENTS = [
    (
        "TESTN00001",
        "04/01/2020 16:15:18",
        "Pay Bill from 2547****111 - ALICE WANJIRU EXAMPLE Acc. alice",
        1100.0,
        {
            "transactiontype": "Pay Bill",
            "msisdn": "2547****111",
            "firstname": "ALICE",
            "middlename": "WANJIRU",
            "lastname": "EXAMPLE",
            "billrefnumber": "alice",
        },
    ),
    (
        "TESTN00002",
        "04/01/2020 16:14:48",
        "Pay Bill Online from 07****222 - Bob Sample Acc. bob shop",
        1900.0,
        {
            "transactiontype": "Pay Bill Online",
            "msisdn": "07****222",
            "firstname": "Bob",
            "middlename": "",
            "lastname": "Sample",
            "billrefnumber": "bob shop",
        },
    ),
    (
        "TESTN00003",
        "04/01/2020 12:05:20",
        "Small Business Pay Bill from 2547****333 - CAROL FICTION Acc. Carol",
        1200.0,
        {
            "transactiontype": "Small Business Pay Bill",
            "msisdn": "2547****333",
            "firstname": "CAROL",
            "middlename": "",
            "lastname": "FICTION",
            "billrefnumber": "Carol",
        },
    ),
    (
        "TESTN00004",
        "04/01/2020 15:45:10",
        "Merchant Pay Utility Received from 1234567 - FAKE AUTO TECH Acc. dave",
        2150.0,
        {
            "transactiontype": "Merchant Pay Utility Received",
            "msisdn": "1234567",
            "firstname": "FAKE",
            "middlename": "AUTO",
            "lastname": "TECH",
            "billrefnumber": "dave",
        },
    ),
    (
        "TESTN00005",
        "04/01/2020 14:30:00",
        "Business Pay Bill via API by 999001 - TEST BANK TO PAYBILL Acc. erin",
        60840.0,
        {
            "transactiontype": "Business Pay Bill via API",
            "msisdn": "999001",
            "firstname": "TEST",
            "middlename": "BANK TO",
            "lastname": "PAYBILL",
            "billrefnumber": "erin",
        },
    ),
    (
        "TESTN00006",
        "04/01/2020 14:09:17",
        "FSI to Pay Bill by 999002 - MOCK BANK LTD - BANK TO TILL Acc. Frank "
        "via WEB by MOCK BANK LTD - BANK TO TILL\\B2BUser1",
        2350.0,
        {
            "transactiontype": "FSI to Pay Bill",
            "msisdn": "999002",
            "firstname": "MOCK",
            "middlename": "BANK LTD - BANK TO",
            "lastname": "TILL",
            "billrefnumber": "Frank",
        },
    ),
    (
        "TESTN00007",
        "04/01/2020 16:11:41",
        "Pay Bill from 2547****777 - GRACE NOACC Acc.",
        1500.0,
        {
            "transactiontype": "Pay Bill",
            "msisdn": "2547****777",
            "firstname": "GRACE",
            "middlename": "",
            "lastname": "NOACC",
            "billrefnumber": "",
        },
    ),
    (
        "TESTN00008",
        "04/01/2020 09:00:00",
        "Merchant Pay Utility Received from 7654321 - TRAILING SPACE CO  Acc.",
        530.0,
        {
            "transactiontype": "Merchant Pay Utility Received",
            "msisdn": "7654321",
            "firstname": "TRAILING",
            "middlename": "SPACE",
            "lastname": "CO",
            "billrefnumber": "",
        },
    ),
]

NEW_TOTAL_PAID_IN = round(sum(payment[3] for payment in NEW_PAYMENTS), 2)

NEW_BANK_WITHDRAWAL = (
    "TESTNWD001",
    "04/01/2020 23:09:30",
    "Agency Withdrawal of funds to Bank",
)

#: Safaricom-internal sweeps: each is an in/out pair with the same receipt.
NEW_INTERNAL_TRANSFERS = [
    (
        "TESTNIT001",
        "04/01/2020 23:09:07",
        "Organization Settlement Account to Organization MMF Account",
    ),
    (
        "TESTNIT002",
        "04/01/2020 23:09:07",
        "Utility Account to Organization Settlement Account",
    ),
]


def _money(value: float) -> str:
    return f"{value:,.2f}"


def build_new_template_rows() -> list[list[str]]:
    """Header plus data rows, newest first, as the portal emits them."""

    total = NEW_TOTAL_PAID_IN
    rows: list[list[str]] = [list(NEW_COLUMN_HEADERS)]

    receipt, completion, details = NEW_BANK_WITHDRAWAL
    rows.append(
        [receipt, completion, details, "Completed", "0.00", _money(-total), "0.00"]
    )

    for receipt, completion, details in NEW_INTERNAL_TRANSFERS:
        rows.append(
            [
                receipt,
                completion,
                details,
                "Completed",
                _money(total),
                "0.00",
                _money(total),
            ]
        )
        rows.append(
            [receipt, completion, details, "Completed", "0.00", _money(-total), "0.00"]
        )

    balance = total
    for receipt, completion, details, amount, _expected in NEW_PAYMENTS:
        rows.append(
            [
                receipt,
                completion,
                details,
                "Completed",
                _money(amount),
                "0.00",
                _money(balance),
            ]
        )
        balance = round(balance - amount, 2)

    return rows


def write_new_template_csv(path: str) -> str:
    """Write the new-template fixture as CSV (CRLF, like the portal)."""

    import csv

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(build_new_template_rows())
    return path


def write_new_template_xlsx(path: str) -> str:
    """Write the new-template fixture as .xlsx; every cell is text, like the portal."""

    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Statements"
    for row in build_new_template_rows():
        sheet.append(row)
    book.save(path)
    return path


def default_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), FIXTURE_NAME)


def main() -> None:
    path = write_xls(default_path())
    print(f"wrote {path}")
    print(f"  payments: {len(PAYMENTS)}  total paid in: {TOTAL_PAID_IN}")


if __name__ == "__main__":
    main()
