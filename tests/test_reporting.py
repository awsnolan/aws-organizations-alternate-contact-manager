"""CSV and JSON report generation.

The reports are the audit trail and the only record of what a run overwrote,
so their contents matter as much as the API calls.
"""

import csv
import json

import pytest


@pytest.fixture
def updated_result():
    return {
        "account_id": "222222222222",
        "contact_type": "SECURITY",
        "status": "updated",
        "previous": {
            "Name": "Old Team",
            "EmailAddress": "old@example.com",
            "PhoneNumber": "+15555550199",
            "Title": "Old Title",
        },
    }


@pytest.fixture
def listed_result():
    return {
        "account_id": "333333333333",
        "contact_type": "BILLING",
        "status": "found",
        "contact": {
            "Name": "Billing Team",
            "EmailAddress": "billing@example.com",
            "PhoneNumber": "+15555550101",
            "Title": "Finance",
        },
    }


def read_csv(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


class TestCsvBeforeState:
    def test_previous_values_are_recorded(self, acm, tmp_path, updated_result):
        out = tmp_path / "report.csv"
        acm.write_csv_report([updated_result], str(out))
        row = read_csv(out)[0]
        assert row["previous_name"] == "Old Team"
        assert row["previous_email"] == "old@example.com"
        assert row["previous_phone"] == "+15555550199"
        assert row["previous_title"] == "Old Title"

    def test_list_results_use_the_current_columns(self, acm, tmp_path, listed_result):
        out = tmp_path / "report.csv"
        acm.write_csv_report([listed_result], str(out))
        row = read_csv(out)[0]
        assert row["email"] == "billing@example.com"
        assert row["previous_email"] == ""

    def test_header_contains_all_expected_columns(self, acm, tmp_path, updated_result):
        out = tmp_path / "report.csv"
        acm.write_csv_report([updated_result], str(out))
        with open(out, newline="") as handle:
            header = next(csv.reader(handle))
        for column in [
            "timestamp",
            "account_id",
            "contact_type",
            "status",
            "reason",
            "name",
            "email",
            "phone",
            "title",
            "previous_name",
            "previous_email",
            "previous_phone",
            "previous_title",
            "error",
        ]:
            assert column in header, column

    def test_missing_previous_leaves_columns_blank(self, acm, tmp_path):
        out = tmp_path / "report.csv"
        acm.write_csv_report(
            [{"account_id": "1", "contact_type": "SECURITY", "status": "updated",
              "previous": None}],
            str(out),
        )
        row = read_csv(out)[0]
        assert row["previous_name"] == ""

    def test_error_rows_are_written(self, acm, tmp_path):
        out = tmp_path / "report.csv"
        acm.write_csv_report(
            [{"account_id": "1", "contact_type": "SECURITY", "status": "error",
              "error": "AccessDeniedException: nope"}],
            str(out),
        )
        row = read_csv(out)[0]
        assert row["status"] == "error"
        assert "AccessDenied" in row["error"]


class TestCsvFormulaInjection:
    """Reports are opened in spreadsheets, so formula-leading contact data must
    be neutralised — without corrupting the values needed for recovery."""

    @pytest.mark.parametrize("dangerous", ["=", "@", "\t", "\r"])
    def test_formula_prefixes_always_escaped(self, acm, dangerous):
        assert acm.sanitize_csv_field(f"{dangerous}SUM(A1)").startswith("'")

    @pytest.mark.parametrize(
        "payload",
        [
            '=cmd|\' /c calc\'!A1',
            '@SUM(1+1)*cmd|\' /c calc\'!A0',
            '=HYPERLINK("http://evil.example","click")',
            '+HYPERLINK("http://evil.example")',
            '-2+3+cmd|\' /c calc\'!A0',
            '=1+1',
            '+WEBSERVICE("http://evil.example")',
            '-WEBSERVICE("http://evil.example")',
        ],
    )
    def test_known_injection_payloads_escaped(self, acm, payload):
        """Covers the + and - cases too: they contain letters or quotes, so
        they fall outside the numeric-safe exception."""
        assert acm.sanitize_csv_field(payload).startswith("'"), payload

    @pytest.mark.parametrize(
        "phone",
        [
            "+15555550199",
            "+61-2-1234-5678",
            "+1 (206) 555 1234",
            "-15555550199",
            "+1-555-0100",
            "(02) 1234 5678",
        ],
    )
    def test_phone_numbers_are_not_corrupted(self, acm, phone):
        """Regression: escaping every leading + mangled the phone column, so a
        value restored from the report would fail the API's PhoneNumber
        pattern. Anything matching that pattern cannot form a formula."""
        assert acm.sanitize_csv_field(phone) == phone

    def test_numeric_safe_exception_cannot_build_a_formula(self, acm):
        """The exception only admits digits, spaces and ()+- — no letters means
        no function call, no cell reference, no DDE."""
        import re

        allowed = acm._CSV_NUMERIC_SAFE
        for dangerous_char in "=@|!ABCDEFcmdHYPERLINK'\",":
            assert not re.fullmatch(allowed, f"+{dangerous_char}"), dangerous_char

    def test_benign_values_untouched(self, acm):
        assert acm.sanitize_csv_field("Security Team") == "Security Team"

    def test_non_strings_untouched(self, acm):
        assert acm.sanitize_csv_field(None) is None
        assert acm.sanitize_csv_field(42) == 42

    def test_empty_string_untouched(self, acm):
        assert acm.sanitize_csv_field("") == ""

    def test_applied_to_previous_columns(self, acm, tmp_path):
        """The new before-state columns carry attacker-influenced data too."""
        out = tmp_path / "report.csv"
        acm.write_csv_report(
            [{
                "account_id": "1",
                "contact_type": "SECURITY",
                "status": "updated",
                "previous": {
                    "Name": "=cmd|' /c calc'!A1",
                    "EmailAddress": "a@b.co",
                    "PhoneNumber": "+15555550199",
                    "Title": "T",
                },
            }],
            str(out),
        )
        row = read_csv(out)[0]
        assert row["previous_name"].startswith("'=")
        # ...while the phone in the same row stays restorable.
        assert row["previous_phone"] == "+15555550199"

    def test_report_round_trips_as_a_recovery_source(self, acm, tmp_path):
        """The point of the before-state columns: values read back out must be
        acceptable to the API again."""
        original = {
            "Name": "Old Team",
            "EmailAddress": "old@example.com",
            "PhoneNumber": "+61-2-1234-5678",
            "Title": "Old Title",
        }
        out = tmp_path / "report.csv"
        acm.write_csv_report(
            [{"account_id": "1", "contact_type": "SECURITY", "status": "updated",
              "previous": original}],
            str(out),
        )
        row = read_csv(out)[0]
        restored = {
            "name": row["previous_name"],
            "email": row["previous_email"],
            "phone": row["previous_phone"],
            "title": row["previous_title"],
        }
        assert acm.validate_contact_fields(restored) == [], restored

    def test_applied_to_current_columns(self, acm, tmp_path):
        out = tmp_path / "report.csv"
        acm.write_csv_report(
            [{
                "account_id": "1",
                "contact_type": "SECURITY",
                "status": "found",
                "contact": {
                    "Name": "@import",
                    "EmailAddress": "a@b.co",
                    "PhoneNumber": "+1",
                    "Title": "T",
                },
            }],
            str(out),
        )
        assert read_csv(out)[0]["name"].startswith("'@")


class TestJsonReport:
    def test_structure(self, acm, tmp_path, updated_result):
        out = tmp_path / "report.json"
        acm.write_json_report([updated_result], str(out))
        report = json.loads(out.read_text())
        assert "timestamp" in report
        assert report["total_accounts"] == 1
        assert report["summary"] == {"updated": 1}
        assert report["results"][0]["previous"]["Name"] == "Old Team"

    def test_previous_survives_round_trip(self, acm, tmp_path, updated_result):
        """The JSON report is the machine-readable recovery source."""
        out = tmp_path / "report.json"
        acm.write_json_report([updated_result], str(out))
        restored = json.loads(out.read_text())["results"][0]["previous"]
        assert restored == updated_result["previous"]

    def test_counts_distinct_accounts(self, acm, tmp_path):
        results = [
            {"account_id": "111111111111", "contact_type": t, "status": "updated"}
            for t in ("BILLING", "OPERATIONS", "SECURITY")
        ]
        out = tmp_path / "report.json"
        acm.write_json_report(results, str(out))
        assert json.loads(out.read_text())["total_accounts"] == 1


class TestSummary:
    def test_counts_by_status(self, acm):
        results = (
            [{"status": "updated"}] * 3
            + [{"status": "skipped"}] * 2
            + [{"status": "error"}]
        )
        assert acm.summarize_results(results) == {"updated": 3, "skipped": 2, "error": 1}

    def test_empty(self, acm):
        assert acm.summarize_results([]) == {}

    def test_unknown_status_is_counted(self, acm):
        assert acm.summarize_results([{}]) == {"unknown": 1}
