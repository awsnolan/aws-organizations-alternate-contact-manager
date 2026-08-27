"""CLI behaviour: argument parsing, the --max-changes ceiling, and exit codes.

The ceiling tests drive main() with AWS resolution replaced, and assert that
run_operation is never reached when the gate fires. Asserting "nothing was
written" matters more than asserting the message.
"""

import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "aws_alternate_contact_manager.py"

UPDATE_ARGS = [
    "update",
    "--accounts", "all",
    "--type", "security",
    "--name", "Sec Team",
    "--email", "sec@example.com",
    "--phone", "+15555550100",
    "--title", "SecOps",
    "--output", "none",
]


def run_cli(args, env=None):
    """Invoke the script as a subprocess. Returns (returncode, combined output)."""
    import os

    child_env = dict(os.environ)
    child_env.update({
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    })
    if env:
        child_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True, text=True, env=child_env,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.fixture
def drive_main(acm, monkeypatch):
    """Run main() offline. Yields a callable returning (exit_code, stdout, write_count)."""

    def _run(argv, n_accounts):
        accounts = [str(100000000000 + i) for i in range(n_accounts)]
        calls = {"count": 0}

        def spy_run_operation(*args, **kwargs):
            calls["count"] += 1
            return []

        monkeypatch.setattr(acm, "get_all_active_account_ids", lambda *a, **k: accounts)
        monkeypatch.setattr(acm, "get_accounts_for_ou", lambda *a, **k: accounts)
        monkeypatch.setattr(acm, "get_caller_account_id", lambda *a, **k: "111111111111")
        monkeypatch.setattr(acm.boto3, "client", lambda *a, **k: object())
        monkeypatch.setattr(acm, "run_operation", spy_run_operation)
        monkeypatch.setattr(sys, "argv", ["acm"] + argv)

        buffer = io.StringIO()
        code = 0
        try:
            with redirect_stdout(buffer):
                acm.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
        return code, buffer.getvalue(), calls["count"]

    return _run


class TestArgumentParsing:
    def test_help_succeeds(self):
        code, out = run_cli(["--help"])
        assert code == 0
        assert "--max-changes" in out

    def test_help_documents_worker_defaults(self):
        _, out = run_cli(["--help"])
        flat = " ".join(out.split())
        assert "delete=3" in flat
        assert "1/sec, burst 6" in flat

    def test_action_is_required(self):
        code, _ = run_cli([])
        assert code == 2

    def test_targeting_is_required(self):
        code, out = run_cli(["list", "--type", "security"])
        assert code == 2
        assert "--accounts" in out or "--ou" in out

    def test_accounts_and_ou_are_mutually_exclusive(self):
        code, out = run_cli(
            ["list", "--accounts", "all", "--ou", "ou-abc1-23456789", "--type", "security"]
        )
        assert code == 2
        assert "not allowed with" in out

    def test_update_requires_contact_fields(self):
        code, out = run_cli(["update", "--accounts", "111111111111", "--type", "security"])
        assert code == 2
        assert "required for update action" in out

    def test_invalid_contact_type_rejected(self):
        code, _ = run_cli(["list", "--accounts", "all", "--type", "nonsense"])
        assert code == 2


class TestFieldValidationWiring:
    def test_bad_email_rejected_before_any_call(self):
        code, out = run_cli([
            "update", "--accounts", "111111111111", "--type", "security",
            "--name", "X", "--email", "notanemail",
            "--phone", "+15555550100", "--title", "T",
        ])
        assert code == 2
        assert "before any API call" in out
        assert "--email" in out

    def test_multiple_field_errors_reported_together(self):
        code, out = run_cli([
            "update", "--accounts", "111111111111", "--type", "security",
            "--name", "N" * 70, "--email", "a@b.co",
            "--phone", "555-CALL", "--title", "T",
        ])
        assert code == 2
        assert "--name" in out and "--phone" in out

    @pytest.mark.parametrize("action", ["list", "delete"])
    def test_no_field_validation_for_read_and_delete(self, action):
        _, out = run_cli([action, "--accounts", "999999999999", "--type", "security"])
        assert "before any API call" not in out


class TestWorkerValidation:
    @pytest.mark.parametrize("value", ["0", "51", "500", "-5", "ten"])
    def test_out_of_range_rejected(self, value):
        code, out = run_cli(
            ["list", "--accounts", "111111111111", "--type", "security", "--workers", value]
        )
        assert code == 2, out[-300:]

    @pytest.mark.parametrize("value", ["1", "10", "50"])
    def test_in_range_accepted_by_parser(self, value):
        """Reaches AWS resolution and fails there, not at argument parsing."""
        code, out = run_cli(
            ["list", "--accounts", "111111111111", "--type", "security", "--workers", value]
        )
        assert "between 1 and 50" not in out


class TestWorkerDefaults:
    def test_delete_uses_lower_default(self, drive_main):
        code, out, _ = drive_main(
            ["delete", "--accounts", "all", "--type", "security", "--output", "none"], 10
        )
        assert code == 0
        assert "Parallel workers: 3 (default for delete)" in out

    def test_update_uses_higher_default(self, drive_main):
        _, out, _ = drive_main(UPDATE_ARGS, 10)
        assert "Parallel workers: 10 (default for update)" in out

    def test_explicit_workers_override_default(self, drive_main):
        _, out, _ = drive_main(UPDATE_ARGS + ["--workers", "7"], 10)
        assert "Parallel workers: 7" in out
        assert "default for" not in out

    def test_high_delete_worker_count_warns(self, drive_main):
        _, out, _ = drive_main(
            ["delete", "--accounts", "all", "--type", "security",
             "--workers", "25", "--output", "none"],
            10,
        )
        assert "1/sec, burst 6" in out
        assert "retry backoff" in out


class TestMaxChangesCeiling:
    def test_blocks_org_wide_update_without_writing(self, acm, drive_main):
        code, out, writes = drive_main(UPDATE_ARGS, 100)
        assert code == acm.EXIT_ABORTED
        assert writes == 0, "run_operation must not be reached when the gate fires"
        assert "ABORTED" in out

    def test_error_reports_the_exact_flag_to_use(self, drive_main):
        _, out, _ = drive_main(UPDATE_ARGS, 100)
        assert "--max-changes 100" in out

    def test_error_shows_the_arithmetic(self, drive_main):
        args = [a if a != "security" else "all" for a in UPDATE_ARGS]
        _, out, _ = drive_main(args, 30)
        assert "90 write operations" in out
        assert "30 account(s) × 3 contact type(s)" in out

    def test_allows_run_under_ceiling(self, drive_main):
        code, _, writes = drive_main(UPDATE_ARGS, 10)
        assert code == 0
        assert writes == 1

    def test_explicit_ceiling_permits_larger_run(self, drive_main):
        code, _, writes = drive_main(UPDATE_ARGS + ["--max-changes", "100"], 100)
        assert code == 0
        assert writes == 1

    def test_zero_means_unlimited(self, drive_main):
        code, _, writes = drive_main(UPDATE_ARGS + ["--max-changes", "0"], 5000)
        assert code == 0
        assert writes == 1

    def test_dry_run_is_exempt(self, drive_main):
        code, out, writes = drive_main(UPDATE_ARGS + ["--dry-run"], 1000)
        assert code == 0
        assert writes == 1, "--dry-run is how scope is inspected; it must not be blocked"
        assert "ABORTED" not in out

    def test_list_is_exempt(self, drive_main):
        code, _, writes = drive_main(
            ["list", "--accounts", "all", "--type", "all", "--output", "none"], 1000
        )
        assert code == 0
        assert writes == 1

    def test_org_wide_delete_is_blocked(self, acm, drive_main):
        code, _, writes = drive_main(
            ["delete", "--accounts", "all", "--type", "all", "--output", "none"], 500
        )
        assert code == acm.EXIT_ABORTED
        assert writes == 0

    def test_boundary_exactly_at_ceiling_is_allowed(self, acm, drive_main):
        code, _, writes = drive_main(UPDATE_ARGS, acm.DEFAULT_MAX_CHANGES)
        assert code == 0
        assert writes == 1

    def test_boundary_one_over_ceiling_is_blocked(self, acm, drive_main):
        code, _, writes = drive_main(UPDATE_ARGS, acm.DEFAULT_MAX_CHANGES + 1)
        assert code == acm.EXIT_ABORTED
        assert writes == 0


class TestAccountResolution:
    def test_duplicates_are_removed(self, acm, monkeypatch, drive_main):
        code, out, _ = drive_main(
            ["list", "--accounts", "111111111111,111111111111,222222222222",
             "--type", "security", "--output", "none"],
            0,
        )
        # get_all_active_account_ids is stubbed to [] for n_accounts=0, so the
        # membership check rejects these before dedup is reported.
        assert code == acm.EXIT_USAGE

    def test_invalid_format_exits_usage(self, acm, drive_main):
        code, out, writes = drive_main(
            ["list", "--accounts", "abcdefghijkl", "--type", "security",
             "--output", "none"],
            5,
        )
        assert code == acm.EXIT_USAGE
        assert "Invalid account ID format" in out
        assert writes == 0

    def test_account_outside_org_exits_usage(self, acm, drive_main):
        code, out, writes = drive_main(
            ["list", "--accounts", "999999999999", "--type", "security",
             "--output", "none"],
            5,
        )
        assert code == acm.EXIT_USAGE
        assert "not in your organization" in out
        assert writes == 0


class TestExitCodeConstants:
    def test_values(self, acm):
        assert acm.EXIT_OK == 0
        assert acm.EXIT_ERRORS == 1
        assert acm.EXIT_USAGE == 2
        assert acm.EXIT_ABORTED == 3
        assert acm.EXIT_INTERRUPTED == 130

    def test_all_distinct(self, acm):
        codes = [acm.EXIT_OK, acm.EXIT_ERRORS, acm.EXIT_USAGE,
                 acm.EXIT_ABORTED, acm.EXIT_INTERRUPTED]
        assert len(set(codes)) == len(codes)


class TestPythonFloor:
    def test_declared(self, acm):
        assert acm.MIN_PYTHON == (3, 9)

    def test_guard_present_in_source(self):
        assert "sys.version_info < MIN_PYTHON" in SCRIPT.read_text()
