"""The AccountId parameter must be omitted for the caller's own account.

This is the highest-value behaviour in the suite. The Account Management API
rejects an explicit AccountId when the target is the calling account — it must
be called in "standalone context" by leaving the parameter out entirely.

    https://docs.aws.amazon.com/accounts/latest/APIReference/API_PutAlternateContact.html

Because ListAccounts includes the management account, '--accounts all' run from
the management account always hits this case. A previous revision passed
AccountId unconditionally, which meant the management account silently failed
on every org-wide run.

Stubber asserts exact request parameters, so a policy of "expected_params has
no AccountId" is a direct assertion of the required behaviour.
"""

import pytest
from conftest import EXISTING_CONTACT


class TestKwargsBuilder:
    """Unit-level checks on the helper, independent of any client."""

    def test_self_omits_account_id(self, acm, caller):
        assert acm.contact_api_kwargs(caller, "SECURITY", caller) == {
            "AlternateContactType": "SECURITY"
        }

    def test_member_includes_account_id(self, acm, caller, member):
        assert acm.contact_api_kwargs(member, "SECURITY", caller) == {
            "AlternateContactType": "SECURITY",
            "AccountId": member,
        }

    @pytest.mark.parametrize("contact_type", ["BILLING", "OPERATIONS", "SECURITY"])
    def test_holds_for_every_contact_type(self, acm, caller, member, contact_type):
        assert "AccountId" not in acm.contact_api_kwargs(caller, contact_type, caller)
        assert "AccountId" in acm.contact_api_kwargs(member, contact_type, caller)


class TestGetAlternateContact:
    def test_self_omits_account_id(self, acm, stub, account_client, caller):
        stub.add_response(
            "get_alternate_contact",
            {"AlternateContact": EXISTING_CONTACT},
            {"AlternateContactType": "SECURITY"},
        )
        acm.get_current_contact(account_client, caller, "SECURITY", caller)

    def test_member_includes_account_id(self, acm, stub, account_client, caller, member):
        stub.add_response(
            "get_alternate_contact",
            {"AlternateContact": EXISTING_CONTACT},
            {"AlternateContactType": "SECURITY", "AccountId": member},
        )
        acm.get_current_contact(account_client, member, "SECURITY", caller)


class TestPutAlternateContact:
    def test_self_omits_account_id(self, acm, stub, account_client, caller, desired):
        stub.add_response(
            "get_alternate_contact",
            {"AlternateContact": EXISTING_CONTACT},
            {"AlternateContactType": "SECURITY"},
        )
        stub.add_response(
            "put_alternate_contact", {}, {"AlternateContactType": "SECURITY", **desired}
        )
        result = acm.process_update(account_client, caller, "SECURITY", desired, caller)
        assert result["status"] == "updated"

    def test_member_includes_account_id(
        self, acm, stub, account_client, caller, member, desired
    ):
        stub.add_response(
            "get_alternate_contact",
            {"AlternateContact": EXISTING_CONTACT},
            {"AlternateContactType": "SECURITY", "AccountId": member},
        )
        stub.add_response(
            "put_alternate_contact",
            {},
            {"AlternateContactType": "SECURITY", "AccountId": member, **desired},
        )
        result = acm.process_update(account_client, member, "SECURITY", desired, caller)
        assert result["status"] == "updated"


class TestDeleteAlternateContact:
    def test_self_omits_account_id(self, acm, stub, account_client, caller):
        stub.add_response(
            "get_alternate_contact",
            {"AlternateContact": EXISTING_CONTACT},
            {"AlternateContactType": "BILLING"},
        )
        stub.add_response("delete_alternate_contact", {}, {"AlternateContactType": "BILLING"})
        result = acm.process_delete(account_client, caller, "BILLING", caller)
        assert result["status"] == "deleted"

    def test_member_includes_account_id(self, acm, stub, account_client, caller, member):
        stub.add_response(
            "get_alternate_contact",
            {"AlternateContact": EXISTING_CONTACT},
            {"AlternateContactType": "BILLING", "AccountId": member},
        )
        stub.add_response(
            "delete_alternate_contact",
            {},
            {"AlternateContactType": "BILLING", "AccountId": member},
        )
        result = acm.process_delete(account_client, member, "BILLING", caller)
        assert result["status"] == "deleted"


class TestListAlternateContact:
    def test_self_omits_account_id(self, acm, stub, account_client, caller):
        stub.add_response(
            "get_alternate_contact",
            {"AlternateContact": EXISTING_CONTACT},
            {"AlternateContactType": "SECURITY"},
        )
        assert acm.process_list(account_client, caller, "SECURITY", caller)["status"] == "found"


class TestMixedBatch:
    """A realistic batch: the caller plus two members in one run."""

    def test_caller_and_members_in_same_batch(
        self, acm, stub, account_client, caller, member, desired
    ):
        from conftest import OTHER_MEMBER

        # Caller: no AccountId on either call.
        stub.add_response(
            "get_alternate_contact",
            {"AlternateContact": EXISTING_CONTACT},
            {"AlternateContactType": "SECURITY"},
        )
        stub.add_response(
            "put_alternate_contact", {}, {"AlternateContactType": "SECURITY", **desired}
        )
        # Members: AccountId present.
        for account in (member, OTHER_MEMBER):
            stub.add_response(
                "get_alternate_contact",
                {"AlternateContact": EXISTING_CONTACT},
                {"AlternateContactType": "SECURITY", "AccountId": account},
            )
            stub.add_response(
                "put_alternate_contact",
                {},
                {"AlternateContactType": "SECURITY", "AccountId": account, **desired},
            )

        for account in (caller, member, OTHER_MEMBER):
            result = acm.process_update(
                account_client, account, "SECURITY", desired, caller
            )
            assert result["status"] == "updated"
