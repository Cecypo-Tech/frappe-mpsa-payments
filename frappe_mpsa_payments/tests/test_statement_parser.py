"""Unit tests for :mod:`frappe_mpsa_payments.statement_parser`.

These are plain pytest tests -- the parser must not import ``frappe``, so no
site or bench context is needed::

    env/bin/python -m pytest \
        apps/frappe_mpsa_payments/frappe_mpsa_payments/tests/

All fixture data is synthetic (see ``fixtures/make_fixture.py``).  No real
statement content is committed to this repository.
"""

from __future__ import annotations

import os

import pytest

from frappe_mpsa_payments.statement_parser import (
	ParsedStatement,
	StatementParseError,
	StatementRow,
	_parse_sheet,
	_split_other_party_info,
	_to_clean_id,
	parse_statement,
)
from frappe_mpsa_payments.tests.fixtures import make_fixture

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeSheet:
	"""Minimal stand-in for an ``xlrd`` sheet.

	Exposes exactly the surface ``_parse_sheet`` is allowed to touch, which
	lets tests build arbitrary layouts (missing headers, ragged rows, numeric
	cells) without writing a workbook to disk.
	"""

	def __init__(self, rows: list[list[object]], name: str = "transaction"):
		self.name = name
		self._rows = rows
		self.nrows = len(rows)
		self.ncols = max((len(row) for row in rows), default=0)

	def cell_value(self, row: int, col: int) -> object:
		values = self._rows[row]
		if col >= len(values):
			return ""
		return values[col]


@pytest.fixture(scope="module")
def fixture_path(tmp_path_factory) -> str:
	"""The committed synthetic fixture, regenerated if it is missing."""

	path = make_fixture.default_path()
	if not os.path.exists(path):
		make_fixture.write_xls(path)
	return path


@pytest.fixture(scope="module")
def parsed(fixture_path) -> ParsedStatement:
	return parse_statement(fixture_path)


def parse_rows(rows: list[list[object]]) -> ParsedStatement:
	return _parse_sheet(FakeSheet(rows))


# ---------------------------------------------------------------------------
# 1. The double-row structure collapses correctly
# ---------------------------------------------------------------------------


def test_every_payment_appears_twice_in_the_source(fixture_path):
	"""Guard the premise: the fixture really does duplicate each receipt."""

	import xlrd

	sheet = xlrd.open_workbook(fixture_path).sheet_by_index(0)
	receipts = [sheet.cell_value(row, 0) for row in range(7, sheet.nrows)]
	for payment in make_fixture.PAYMENTS:
		assert receipts.count(payment[0]) == 2


def test_only_paid_in_rows_are_returned(parsed):
	expected_data_rows = len(make_fixture.PAYMENTS) * 2 + len(make_fixture.SETTLEMENTS)
	assert parsed.total_rows == expected_data_rows
	assert len(parsed.payment_rows) == len(make_fixture.PAYMENTS)
	assert all(row.amount > 0 for row in parsed.payment_rows)


def test_charge_rows_and_settlements_are_excluded(parsed):
	returned = {row.receipt_no for row in parsed.payment_rows}
	assert returned == {payment[0] for payment in make_fixture.PAYMENTS}
	for receipt, _completion, _amount in make_fixture.SETTLEMENTS:
		assert receipt not in returned


def test_row_index_points_at_the_paid_in_row(parsed, fixture_path):
	"""``row_index`` must address the real payment row, not its charge twin."""

	import xlrd

	sheet = xlrd.open_workbook(fixture_path).sheet_by_index(0)
	for row in parsed.payment_rows:
		assert sheet.cell_value(row.row_index, 0) == row.receipt_no
		assert sheet.cell_value(row.row_index, 5) == row.amount


def test_blank_rows_are_not_counted_as_data_rows():
	rows = make_fixture.build_rows()
	rows.append([""] * len(make_fixture.COLUMN_HEADERS))
	rows.append([""] * len(make_fixture.COLUMN_HEADERS))
	result = parse_rows(rows)
	assert result.total_rows == len(make_fixture.PAYMENTS) * 2 + len(make_fixture.SETTLEMENTS)


# ---------------------------------------------------------------------------
# 2. duplicate_receipts
# ---------------------------------------------------------------------------


def test_no_duplicates_in_a_well_formed_file(parsed):
	assert parsed.duplicate_receipts == []


def test_shared_receipt_with_the_charge_twin_is_not_a_duplicate(parsed):
	"""The charge row shares the receipt by design -- that is not a duplicate."""

	receipts = [row.receipt_no for row in parsed.payment_rows]
	assert len(receipts) == len(set(receipts))
	assert parsed.duplicate_receipts == []


def test_genuine_duplicate_payment_row_is_reported():
	rows = make_fixture.build_rows(duplicate_receipt="TEST0000B2")
	result = parse_rows(rows)

	assert result.duplicate_receipts == ["TEST0000B2"]
	assert len(result.payment_rows) == len(make_fixture.PAYMENTS) + 1


def test_duplicate_reported_once_even_when_repeated_three_times():
	rows = make_fixture.build_rows(duplicate_receipt="TEST0000A1")
	rows.extend(make_fixture.build_rows(duplicate_receipt="TEST0000A1")[-2:])
	result = parse_rows(rows)
	assert result.duplicate_receipts == ["TEST0000A1"]


def test_duplicates_from_the_xls_writer_too(tmp_path):
	"""The duplicate path works end to end through a real .xls, not just a fake."""

	path = str(tmp_path / "dupe.xls")
	make_fixture.write_xls(path, duplicate_receipt="TEST0000C3")
	result = parse_statement(path)
	assert result.duplicate_receipts == ["TEST0000C3"]


# ---------------------------------------------------------------------------
# 3. Amounts reconcile against the header total
# ---------------------------------------------------------------------------


def test_amount_sum_matches_header_total(parsed):
	assert parsed.header_total_paid_in == make_fixture.TOTAL_PAID_IN
	assert round(sum(row.amount for row in parsed.payment_rows), 2) == parsed.header_total_paid_in


def test_header_total_is_none_when_the_label_is_absent():
	rows = [row for row in make_fixture.build_rows() if "Total Paid In:" not in row]
	result = parse_rows(rows)
	assert result.header_total_paid_in is None


def test_amounts_are_floats(parsed):
	assert all(isinstance(row.amount, float) for row in parsed.payment_rows)


# ---------------------------------------------------------------------------
# 4. transtime / completion_time conversion
# ---------------------------------------------------------------------------


def test_transtime_conversion():
	rows = make_fixture.build_rows()
	rows[8][1] = "28-07-2026 17:16:40"
	rows[8][2] = "28-07-2026 17:16:40"
	result = parse_rows(rows)
	row = result.payment_rows[0]

	assert row.transtime == "20260728171640"
	assert row.completion_time == "2026-07-28 17:16:40"


def test_transtime_is_day_first_not_month_first():
	"""``04-01-2020`` is 4 January, not 1 April."""

	first = parse_rows(make_fixture.build_rows()).payment_rows[0]
	assert first.transtime == "20200104171640"
	assert first.completion_time == "2020-01-04 17:16:40"


def test_unparseable_completion_time_yields_empty_strings():
	rows = make_fixture.build_rows()
	rows[8][1] = "not a date"
	row = parse_rows(rows).payment_rows[0]
	assert row.transtime == ""
	assert row.completion_time == ""
	# The row itself is still returned -- the amount is what makes it a payment.
	assert row.amount == make_fixture.PAYMENTS[0][2]


def test_transtime_is_always_fourteen_digits(parsed):
	for row in parsed.payment_rows:
		assert len(row.transtime) == 14
		assert row.transtime.isdigit()


# ---------------------------------------------------------------------------
# 5. Other Party Info -> msisdn + names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
	"raw,expected",
	[
		# Masked middle name, full country-code msisdn.
		("25470****111 - ALICE **** EXAMPLE", ("25470****111", "ALICE", "", "EXAMPLE")),
		# No middle name at all.
		("25470****222 - CAROL FICTION", ("25470****222", "CAROL", "", "FICTION")),
		# Lowercase names must survive unchanged.
		("010****333 - bob **** sample", ("010****333", "bob", "", "sample")),
		# Short (local-format) msisdn prefix.
		("070****444 - DAVE **** MADEUP", ("070****444", "DAVE", "", "MADEUP")),
	],
)
def test_other_party_info_shapes(raw, expected):
	"""The four ``Other Party Info`` shapes that occur in real exports.

	The *shapes* are reproduced faithfully; the names and numbers are
	fictional, because this repository is published publicly and real
	statements contain customer names and partially masked phone numbers.
	"""

	assert _split_other_party_info(raw) == expected


@pytest.mark.parametrize(
	"raw,expected",
	[
		# A real (unmasked) middle name is preserved.
		("25470****111 - ANN WANJIRU KAMAU", ("25470****111", "ANN", "WANJIRU", "KAMAU")),
		# Multiple middle tokens are joined.
		("25470****111 - ANN WANJIRU MUTHONI KAMAU", ("25470****111", "ANN", "WANJIRU MUTHONI", "KAMAU")),
		# Mixed mask + real middle: the mask drops, the real token stays.
		("25470****111 - ANN **** WANJIRU KAMAU", ("25470****111", "ANN", "WANJIRU", "KAMAU")),
		# Single name token: firstname only.
		("25470****111 - MADONNA", ("25470****111", "MADONNA", "", "")),
		# Msisdn with no name.
		("25470****111 - ", ("25470****111", "", "", "")),
		# Bare msisdn, no separator.
		("25470****111", ("25470****111", "", "", "")),
		# Empty / missing.
		("", ("", "", "", "")),
		(None, ("", "", "", "")),
		# The charge rows' 'SP' marker degrades to a name, never crashes.
		("SP", ("", "SP", "", "")),
		# Extra whitespace is collapsed.
		("  25470****111  -  ANN   ****   KAMAU  ", ("25470****111", "ANN", "", "KAMAU")),
	],
)
def test_other_party_info_edge_cases(raw, expected):
	assert _split_other_party_info(raw) == expected


def test_mask_tokens_never_leak_into_middlename(parsed):
	for row in parsed.payment_rows:
		assert "*" not in row.middlename


def test_case_is_preserved_across_the_fixture(parsed):
	by_receipt = {row.receipt_no: row for row in parsed.payment_rows}
	assert by_receipt["TEST0000A1"].firstname == "ALICE"
	assert by_receipt["TEST0000B2"].firstname == "bob"
	assert by_receipt["TEST0000B2"].lastname == "sample"


def test_msisdn_stays_masked(parsed):
	by_receipt = {row.receipt_no: row for row in parsed.payment_rows}
	assert by_receipt["TEST0000A1"].msisdn == "25470****111"
	assert by_receipt["TEST0000B2"].msisdn == "010****222"


# ---------------------------------------------------------------------------
# 6. billrefnumber must be a clean string
# ---------------------------------------------------------------------------


def test_billrefnumber_from_a_numeric_cell_has_no_float_suffix():
	rows = make_fixture.build_rows()
	rows[8][12] = 850060.0  # numeric A/C No., as xlrd would hand it back
	row = parse_rows(rows).payment_rows[0]

	assert row.billrefnumber == "850060"
	assert isinstance(row.billrefnumber, str)


def test_billrefnumber_from_a_text_cell_is_unchanged(parsed):
	assert {row.billrefnumber for row in parsed.payment_rows} == {
		"850060",
		"750007",
		"750122",
		"850002",
		"750942",
	}
	assert all(isinstance(row.billrefnumber, str) for row in parsed.payment_rows)
	assert all("." not in row.billrefnumber for row in parsed.payment_rows)


def test_billrefnumber_preserves_leading_zeros():
	rows = make_fixture.build_rows()
	rows[8][12] = "0850060"
	assert parse_rows(rows).payment_rows[0].billrefnumber == "0850060"


def test_billrefnumber_strips_a_float_that_leaked_into_a_text_cell():
	rows = make_fixture.build_rows()
	rows[8][12] = "850060.0"
	assert parse_rows(rows).payment_rows[0].billrefnumber == "850060"


@pytest.mark.parametrize(
	"raw,expected",
	[
		(850060.0, "850060"),
		(850060, "850060"),
		("850060", "850060"),
		("850060.0", "850060"),
		("  850060  ", "850060"),
		("0850060", "0850060"),
		("", ""),
		(None, ""),
	],
)
def test_to_clean_id(raw, expected):
	assert _to_clean_id(raw) == expected


def test_receipt_no_is_also_a_clean_string(parsed):
	assert all(isinstance(row.receipt_no, str) for row in parsed.payment_rows)
	assert all(row.receipt_no for row in parsed.payment_rows)


# ---------------------------------------------------------------------------
# 7. Error handling
# ---------------------------------------------------------------------------


def test_missing_header_row_raises(tmp_path):
	path = str(tmp_path / "no_header.xls")
	make_fixture.write_headerless_xls(path)

	with pytest.raises(StatementParseError) as excinfo:
		parse_statement(path)
	assert "Receipt No." in str(excinfo.value)


def test_missing_header_row_raises_on_a_bare_sheet():
	with pytest.raises(StatementParseError):
		parse_rows([["Account Holder:", "Nobody"], ["a", "b", "c"]])


def test_non_xls_file_raises(tmp_path):
	path = tmp_path / "not_a_workbook.xls"
	path.write_text("receipt,amount\nABC,100\n")

	with pytest.raises(StatementParseError) as excinfo:
		parse_statement(str(path))
	assert ".xls" in str(excinfo.value)


def test_missing_file_raises():
	with pytest.raises(StatementParseError) as excinfo:
		parse_statement("/nonexistent/path/statement.xls")
	assert "not found" in str(excinfo.value).lower()


def test_header_row_present_but_paid_in_column_missing():
	with pytest.raises(StatementParseError) as excinfo:
		parse_rows([["Receipt No.", "Completion Time", "Details"], ["ABC", "01-01-2020 00:00:00", "x"]])
	assert "Paid In" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Preamble parsing
# ---------------------------------------------------------------------------


def test_preamble_fields(parsed):
	assert parsed.business_shortcode == make_fixture.SHORT_CODE
	assert parsed.account_holder == make_fixture.ACCOUNT_HOLDER
	assert parsed.statement_period == make_fixture.STATEMENT_PERIOD


def test_business_shortcode_is_stripped():
	"""Trailing whitespace on the short code must never reach the doctype."""

	rows = make_fixture.build_rows()
	rows[1][1] = "  778899 "
	assert parse_rows(rows).business_shortcode == "778899"


def test_business_shortcode_from_a_numeric_cell():
	rows = make_fixture.build_rows()
	rows[1][1] = 778899.0
	assert parse_rows(rows).business_shortcode == "778899"


def test_header_row_index_is_not_hardcoded():
	"""Extra preamble rows must not shift the parser off the header."""

	rows = make_fixture.build_rows()
	blank = [""] * len(make_fixture.COLUMN_HEADERS)
	rows.insert(0, list(blank))
	rows.insert(0, ["Some New Label:", "some value", *blank[2:]])
	rows.insert(3, list(blank))

	result = parse_rows(rows)
	assert result.business_shortcode == make_fixture.SHORT_CODE
	assert result.account_holder == make_fixture.ACCOUNT_HOLDER
	assert result.statement_period == make_fixture.STATEMENT_PERIOD
	assert result.total_rows == len(make_fixture.PAYMENTS) * 2 + len(make_fixture.SETTLEMENTS)
	assert len(result.payment_rows) == len(make_fixture.PAYMENTS)


def test_columns_are_derived_from_the_header_not_by_position():
	"""Reordering the data columns must not change the parsed output."""

	rows = make_fixture.build_rows()
	order = [13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0]
	header_row = 6
	reordered = rows[:header_row]
	for row in rows[header_row:]:
		reordered.append([row[i] for i in order])

	result = parse_rows(reordered)
	assert len(result.payment_rows) == len(make_fixture.PAYMENTS)
	assert result.payment_rows[0].receipt_no == "TEST0000A1"
	assert result.payment_rows[0].billrefnumber == "850060"
	assert result.payment_rows[0].transtime == "20200104171640"


def test_missing_preamble_labels_degrade_gracefully():
	rows = make_fixture.build_rows()
	del rows[0:6]  # drop the whole preamble, keep the header + data
	result = parse_rows(rows)

	assert result.business_shortcode == ""
	assert result.account_holder == ""
	assert result.statement_period == ""
	assert result.header_total_paid_in is None
	assert len(result.payment_rows) == len(make_fixture.PAYMENTS)


# ---------------------------------------------------------------------------
# Remaining contract surface
# ---------------------------------------------------------------------------


def test_transactiontype_and_currency(parsed):
	by_receipt = {row.receipt_no: row for row in parsed.payment_rows}
	assert by_receipt["TEST0000A1"].transactiontype == "Pay Utility"
	assert by_receipt["TEST0000C3"].transactiontype == "Pay Utility with OD via STK"
	assert all(row.currency == "KES" for row in parsed.payment_rows)


def test_returned_types(parsed):
	assert isinstance(parsed, ParsedStatement)
	assert all(isinstance(row, StatementRow) for row in parsed.payment_rows)
	assert isinstance(parsed.payment_rows, list)
	assert isinstance(parsed.duplicate_receipts, list)


def test_parser_module_does_not_import_frappe():
	"""The parser must stay unit-testable without a Frappe site."""

	import inspect

	from frappe_mpsa_payments import statement_parser

	source = inspect.getsource(statement_parser)
	assert "import frappe" not in source
	assert "from frappe" not in source


def test_zero_and_negative_paid_in_are_excluded():
	rows = make_fixture.build_rows()
	rows[8][5] = 0.0
	rows[10][5] = -5.0
	result = parse_rows(rows)
	assert len(result.payment_rows) == len(make_fixture.PAYMENTS) - 2


def test_paid_in_given_as_text_is_still_parsed():
	rows = make_fixture.build_rows()
	rows[8][5] = "7,800.00"
	row = parse_rows(rows).payment_rows[0]
	assert row.amount == 7800.0
