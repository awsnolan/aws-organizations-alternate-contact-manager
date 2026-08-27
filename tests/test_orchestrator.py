"""run_operation: aggregation, error isolation, and shutdown behaviour.

The per-account operations are covered against stubbed API calls in
test_operations.py. What matters here is the orchestrator's own contract:

  - every account x contact type produces exactly one result
  - one account failing does not abandon the rest of the batch
  - failures are recorded rather than dropped, because the exit code is
    derived from the error count

The process_* functions are replaced with fakes so the concurrency and error
handling can be tested deterministically, without Stubber's ordered-response
model fighting a thread pool.
"""

import pytest
from botocore.exceptions import ClientError

CONTACT_INFO = {
    "EmailAddress": "new@example.com",
    "Name": "New Team",
    "PhoneNumber": "+15555550100",
    "Title": "SecOps",
}


def make_client_error(code="TooManyRequestsException", message="slow down"):
    return ClientError(
        {"Error": {"Code": code, "Message": message}}, "PutAlternateContact"
    )


@pytest.fixture(autouse=True)
def reset_shutdown_flag(acm):
    """run_operation reads a module-level flag; keep tests independent."""
    acm._shutdown_requested = False
    yield
    acm._shutdown_requested = False


@pytest.fixture
def no_real_clients(acm, monkeypatch):
    monkeypatch.setattr(acm.boto3, "client", lambda *a, **k: object())


def accounts(n):
    return [str(100000000000 + i) for i in range(n)]


class TestAggregation:
    def test_one_result_per_account_and_type(self, acm, monkeypatch, no_real_clients):
        monkeypatch.setattr(
            acm, "process_update",
            lambda client, account_id, ctype, *a, **k: {
                "account_id": account_id, "contact_type": ctype, "status": "updated"},
        )
        results = acm.run_operation(
            "update", accounts(10), ["BILLING", "SECURITY"], "111111111111",
            contact_info=CONTACT_INFO, max_workers=4,
        )
        assert len(results) == 20
        pairs = {(r["account_id"], r["contact_type"]) for r in results}
        assert len(pairs) == 20, "each account/type pair should appear exactly once"

    def test_single_account_single_type(self, acm, monkeypatch, no_real_clients):
        monkeypatch.setattr(
            acm, "process_list",
            lambda client, account_id, ctype, *a, **k: {
                "account_id": account_id, "contact_type": ctype, "status": "found"},
        )
        results = acm.run_operation("list", accounts(1), ["SECURITY"], "111111111111")
        assert len(results) == 1

    def test_dispatches_to_the_right_operation(self, acm, monkeypatch, no_real_clients):
        called = []
        for name in ("process_update", "process_delete", "process_list"):
            monkeypatch.setattr(
                acm, name,
                lambda client, account_id, ctype, *a, _n=name, **k: (
                    called.append(_n)
                    or {"account_id": account_id, "contact_type": ctype, "status": "ok"}
                ),
            )
        for action in ("update", "delete", "list"):
            acm.run_operation(
                action, accounts(1), ["SECURITY"], "111111111111",
                contact_info=CONTACT_INFO,
            )
        assert called == ["process_update", "process_delete", "process_list"]

    def test_caller_account_is_passed_through(self, acm, monkeypatch, no_real_clients):
        """The self-account fix depends on this reaching process_update."""
        seen = {}

        def fake(client, account_id, ctype, contact_info, caller_account_id, *a, **k):
            seen["caller"] = caller_account_id
            return {"account_id": account_id, "contact_type": ctype, "status": "updated"}

        monkeypatch.setattr(acm, "process_update", fake)
        acm.run_operation(
            "update", accounts(1), ["SECURITY"], "999999999999",
            contact_info=CONTACT_INFO,
        )
        assert seen["caller"] == "999999999999"

    def test_flags_are_forwarded(self, acm, monkeypatch, no_real_clients):
        seen = {}

        def fake(client, account_id, ctype, contact_info, caller, force=False,
                 dry_run=False):
            seen["force"], seen["dry_run"] = force, dry_run
            return {"account_id": account_id, "contact_type": ctype, "status": "x"}

        monkeypatch.setattr(acm, "process_update", fake)
        acm.run_operation(
            "update", accounts(1), ["SECURITY"], "111111111111",
            contact_info=CONTACT_INFO, force=True, dry_run=True,
        )
        assert seen == {"force": True, "dry_run": True}


class TestErrorIsolation:
    """A previous revision returned on the first ClientError, abandoning the
    rest of the batch after already modifying part of the organisation."""

    def test_one_failure_does_not_abandon_the_batch(
        self, acm, monkeypatch, no_real_clients
    ):
        target = accounts(5)[2]

        def fake(client, account_id, ctype, *a, **k):
            if account_id == target:
                raise make_client_error("AccessDeniedException", "nope")
            return {"account_id": account_id, "contact_type": ctype, "status": "updated"}

        monkeypatch.setattr(acm, "process_update", fake)
        results = acm.run_operation(
            "update", accounts(5), ["SECURITY"], "111111111111",
            contact_info=CONTACT_INFO, max_workers=1,
        )
        assert len(results) == 5, "all five accounts should be accounted for"
        summary = acm.summarize_results(results)
        assert summary == {"updated": 4, "error": 1}

    def test_client_error_detail_is_recorded(self, acm, monkeypatch, no_real_clients):
        monkeypatch.setattr(
            acm, "process_update",
            lambda *a, **k: (_ for _ in ()).throw(
                make_client_error("TooManyRequestsException", "slow down")),
        )
        results = acm.run_operation(
            "update", accounts(1), ["SECURITY"], "111111111111",
            contact_info=CONTACT_INFO,
        )
        assert results[0]["status"] == "error"
        assert "TooManyRequestsException" in results[0]["error"]
        assert "slow down" in results[0]["error"]

    def test_unexpected_exception_is_isolated_too(
        self, acm, monkeypatch, no_real_clients
    ):
        def fake(client, account_id, ctype, *a, **k):
            if account_id.endswith("03"):
                raise RuntimeError("something odd")
            return {"account_id": account_id, "contact_type": ctype, "status": "updated"}

        monkeypatch.setattr(acm, "process_update", fake)
        results = acm.run_operation(
            "update", accounts(5), ["SECURITY"], "111111111111",
            contact_info=CONTACT_INFO, max_workers=2,
        )
        assert len(results) == 5
        errors = [r for r in results if r["status"] == "error"]
        assert len(errors) == 1
        assert errors[0]["error"] == "RuntimeError"

    def test_all_failing_still_returns_every_task(
        self, acm, monkeypatch, no_real_clients
    ):
        monkeypatch.setattr(
            acm, "process_update",
            lambda *a, **k: (_ for _ in ()).throw(make_client_error()),
        )
        results = acm.run_operation(
            "update", accounts(6), ["SECURITY"], "111111111111",
            contact_info=CONTACT_INFO, max_workers=3,
        )
        assert len(results) == 6
        assert all(r["status"] == "error" for r in results)

    def test_errors_carry_account_and_type_for_the_report(
        self, acm, monkeypatch, no_real_clients
    ):
        monkeypatch.setattr(
            acm, "process_delete",
            lambda *a, **k: (_ for _ in ()).throw(make_client_error()),
        )
        results = acm.run_operation(
            "delete", ["222222222222"], ["BILLING"], "111111111111"
        )
        assert results[0]["account_id"] == "222222222222"
        assert results[0]["contact_type"] == "BILLING"


class TestShutdownFlag:
    def test_pending_tasks_are_cancelled_when_flag_is_set(
        self, acm, monkeypatch, no_real_clients
    ):
        """Workers check the flag on entry, so a requested shutdown short-circuits
        anything not yet started."""
        acm._shutdown_requested = True
        monkeypatch.setattr(
            acm, "process_update",
            lambda *a, **k: pytest.fail("should not run while shutting down"),
        )
        results = acm.run_operation(
            "update", accounts(4), ["SECURITY"], "111111111111",
            contact_info=CONTACT_INFO, max_workers=1,
        )
        assert len(results) == 4
        assert all(r["status"] == "cancelled" for r in results)
        assert all(r["reason"] == "shutdown_requested" for r in results)


class TestConcurrency:
    @pytest.mark.parametrize("workers", [1, 2, 5, 10])
    def test_result_count_is_independent_of_worker_count(
        self, acm, monkeypatch, no_real_clients, workers
    ):
        monkeypatch.setattr(
            acm, "process_list",
            lambda client, account_id, ctype, *a, **k: {
                "account_id": account_id, "contact_type": ctype, "status": "found"},
        )
        results = acm.run_operation(
            "list", accounts(20), ["BILLING", "SECURITY"], "111111111111",
            max_workers=workers,
        )
        assert len(results) == 40

    def test_concurrent_appends_do_not_lose_results(
        self, acm, monkeypatch, no_real_clients
    ):
        """Results are appended from the main thread as futures complete; verify
        nothing is dropped under contention."""
        monkeypatch.setattr(
            acm, "process_list",
            lambda client, account_id, ctype, *a, **k: {
                "account_id": account_id, "contact_type": ctype, "status": "found"},
        )
        results = acm.run_operation(
            "list", accounts(200), ["BILLING", "OPERATIONS", "SECURITY"],
            "111111111111", max_workers=10,
        )
        assert len(results) == 600
        assert len({(r["account_id"], r["contact_type"]) for r in results}) == 600
