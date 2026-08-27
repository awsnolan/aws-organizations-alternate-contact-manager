"""process_update / process_delete / process_list behaviour.

Covers idempotency, the before-state recorded for the audit trail, and the
--force / --dry-run interaction.
"""

import pytest
from conftest import EXISTING_CONTACT


def _stub_get(stub, member, contact=EXISTING_CONTACT, contact_type="SECURITY"):
    stub.add_response(
        "get_alternate_contact",
        {"AlternateContact": contact},
        {"AlternateContactType": contact_type, "AccountId": member},
    )


def _stub_get_missing(stub, contact_type="SECURITY"):
    stub.add_client_error(
        "get_alternate_contact", service_error_code="ResourceNotFoundException"
    )


def _stub_put(stub, member, desired, contact_type="SECURITY"):
    stub.add_response(
        "put_alternate_contact",
        {},
        {"AlternateContactType": contact_type, "AccountId": member, **desired},
    )


class TestIdempotency:
    def test_matching_contact_is_skipped_without_write(
        self, acm, stub, account_client, caller, member, desired
    ):
        _stub_get(stub, member, {"AlternateContactType": "SECURITY", **desired})
        result = acm.process_update(account_client, member, "SECURITY", desired, caller)
        assert result["status"] == "skipped"
        assert result["reason"] == "already_configured"
        # No put stubbed; the fixture's assert_no_pending_responses proves none
        # was expected, and Stubber would raise if one had been issued.

    def test_differing_contact_is_updated(
        self, acm, stub, account_client, caller, member, desired
    ):
        _stub_get(stub, member)
        _stub_put(stub, member, desired)
        assert (
            acm.process_update(account_client, member, "SECURITY", desired, caller)["status"]
            == "updated"
        )

    def test_whitespace_only_difference_is_skipped(
        self, acm, stub, account_client, caller, member, desired
    ):
        """contact_matches trims, so trailing spaces must not trigger a write."""
        padded = {k: f"  {v}  " for k, v in desired.items()}
        _stub_get(stub, member, {"AlternateContactType": "SECURITY", **padded})
        result = acm.process_update(account_client, member, "SECURITY", desired, caller)
        assert result["status"] == "skipped"

    @pytest.mark.parametrize(
        "field", ["EmailAddress", "Name", "PhoneNumber", "Title"]
    )
    def test_any_single_field_difference_triggers_update(
        self, acm, stub, account_client, caller, member, desired, field
    ):
        current = {"AlternateContactType": "SECURITY", **desired}
        current[field] = "something-else"
        _stub_get(stub, member, current)
        _stub_put(stub, member, desired)
        assert (
            acm.process_update(account_client, member, "SECURITY", desired, caller)["status"]
            == "updated"
        )

    def test_contact_matches_handles_none(self, acm, desired):
        assert acm.contact_matches(None, desired) is False


class TestBeforeStateRecorded:
    """The report must be usable as a recovery source."""

    def test_update_records_previous(
        self, acm, stub, account_client, caller, member, desired
    ):
        _stub_get(stub, member)
        _stub_put(stub, member, desired)
        result = acm.process_update(account_client, member, "SECURITY", desired, caller)
        assert result["previous"]["EmailAddress"] == "old@example.com"
        assert result["previous"]["Name"] == "Old Team"
        assert result["previous"]["PhoneNumber"] == "+15555550199"
        assert result["previous"]["Title"] == "Old Title"

    def test_delete_records_what_was_removed(
        self, acm, stub, account_client, caller, member
    ):
        _stub_get(stub, member)
        stub.add_response(
            "delete_alternate_contact",
            {},
            {"AlternateContactType": "SECURITY", "AccountId": member},
        )
        result = acm.process_delete(account_client, member, "SECURITY", caller)
        assert result["previous"]["EmailAddress"] == "old@example.com"

    def test_skipped_still_records_previous(
        self, acm, stub, account_client, caller, member, desired
    ):
        _stub_get(stub, member, {"AlternateContactType": "SECURITY", **desired})
        result = acm.process_update(account_client, member, "SECURITY", desired, caller)
        assert result["previous"] is not None

    def test_create_case_has_no_previous(
        self, acm, stub, account_client, caller, member, desired
    ):
        """A contact that was never set has nothing to record."""
        _stub_get_missing(stub)
        _stub_put(stub, member, desired)
        result = acm.process_update(account_client, member, "SECURITY", desired, caller)
        assert result["status"] == "updated"
        assert result["previous"] is None


class TestForceAndDryRun:
    def test_dry_run_reads_but_does_not_write(
        self, acm, stub, account_client, caller, member, desired
    ):
        _stub_get(stub, member)
        result = acm.process_update(
            account_client, member, "SECURITY", desired, caller, dry_run=True
        )
        assert result["status"] == "would_update"
        assert result["previous"]["EmailAddress"] == "old@example.com"

    def test_force_with_dry_run_still_reads(
        self, acm, stub, account_client, caller, member, desired
    ):
        """Regression: --force --dry-run previously reported no current state,
        making the preview look successful while telling the operator nothing."""
        _stub_get(stub, member)
        result = acm.process_update(
            account_client, member, "SECURITY", desired, caller, force=True, dry_run=True
        )
        assert result["status"] == "would_update"
        assert result["previous"] is not None
        assert result["previous"]["EmailAddress"] == "old@example.com"

    def test_force_with_dry_run_does_not_skip_matching(
        self, acm, stub, account_client, caller, member, desired
    ):
        """Under --force the idempotency skip is bypassed even in a preview."""
        _stub_get(stub, member, {"AlternateContactType": "SECURITY", **desired})
        result = acm.process_update(
            account_client, member, "SECURITY", desired, caller, force=True, dry_run=True
        )
        assert result["status"] == "would_update"

    def test_force_real_run_issues_only_the_write(
        self, acm, stub, account_client, caller, member, desired
    ):
        """Exactly one call: no GET. This is the documented API saving."""
        _stub_put(stub, member, desired)
        result = acm.process_update(
            account_client, member, "SECURITY", desired, caller, force=True
        )
        assert result["status"] == "updated"
        assert result["previous"] is None, "force forfeits before-state by design"

    def test_delete_dry_run_does_not_delete(
        self, acm, stub, account_client, caller, member
    ):
        _stub_get(stub, member)
        result = acm.process_delete(
            account_client, member, "SECURITY", caller, dry_run=True
        )
        assert result["status"] == "would_delete"
        assert result["previous"] is not None

    def test_delete_force_real_run_issues_only_the_delete(
        self, acm, stub, account_client, caller, member
    ):
        stub.add_response(
            "delete_alternate_contact",
            {},
            {"AlternateContactType": "SECURITY", "AccountId": member},
        )
        result = acm.process_delete(account_client, member, "SECURITY", caller, force=True)
        assert result["status"] == "deleted"
        assert result["previous"] is None


class TestDeleteSkips:
    def test_unset_contact_is_skipped(self, acm, stub, account_client, caller, member):
        _stub_get_missing(stub)
        result = acm.process_delete(account_client, member, "SECURITY", caller)
        assert result["status"] == "skipped"
        assert result["reason"] == "not_set"


class TestListResults:
    def test_found(self, acm, stub, account_client, caller, member):
        _stub_get(stub, member)
        result = acm.process_list(account_client, member, "SECURITY", caller)
        assert result["status"] == "found"
        assert result["contact"]["EmailAddress"] == "old@example.com"

    def test_not_set(self, acm, stub, account_client, caller, member):
        _stub_get_missing(stub)
        result = acm.process_list(account_client, member, "SECURITY", caller)
        assert result["status"] == "not_set"
        assert result["contact"] is None


class TestErrorPropagation:
    def test_non_notfound_errors_are_raised(
        self, acm, stub, account_client, caller, member
    ):
        """Only ResourceNotFoundException is treated as 'no contact'. Anything
        else must propagate so the orchestrator records it as an error."""
        stub.add_client_error(
            "get_alternate_contact", service_error_code="AccessDeniedException"
        )
        with pytest.raises(Exception) as exc:
            acm.get_current_contact(account_client, member, "SECURITY", caller)
        assert "AccessDenied" in str(exc.value)
