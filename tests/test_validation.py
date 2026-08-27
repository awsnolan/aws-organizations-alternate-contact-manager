"""Account ID validation, contact field constraints, and argparse types."""

import pytest


class TestAccountIdValidation:
    @pytest.mark.parametrize("account_id", ["111111111111", "000000000000", "999999999999"])
    def test_valid(self, acm, account_id):
        assert acm.validate_account_id(account_id) is True

    @pytest.mark.parametrize(
        "account_id",
        [
            "11111111111",       # 11 digits
            "1111111111111",     # 13 digits
            "",                  # empty
            "abcdefghijkl",      # 12 non-digits
            "1111 1111 1111",    # spaces
            "11111111111a",      # trailing letter
            "-11111111111",      # sign
        ],
    )
    def test_invalid(self, acm, account_id):
        assert acm.validate_account_id(account_id) is False

    def test_twelve_non_digits_rejected_on_digits_not_length(self, acm):
        """Regression: a length-only check accepted this and then misreported
        it downstream as 'not in your organization'."""
        assert len("abcdefghijkl") == 12
        assert acm.validate_account_id("abcdefghijkl") is False


class TestDeduplication:
    def test_removes_duplicates_preserving_order(self, acm):
        assert acm.deduplicate_accounts(
            ["333333333333", "111111111111", "333333333333", "222222222222"]
        ) == ["333333333333", "111111111111", "222222222222"]

    def test_no_duplicates_is_unchanged(self, acm):
        ids = ["111111111111", "222222222222"]
        assert acm.deduplicate_accounts(ids) == ids

    def test_empty(self, acm):
        assert acm.deduplicate_accounts([]) == []


class TestContactFieldConstraints:
    """Limits are transcribed from the PutAlternateContact reference."""

    def test_documented_examples_are_all_accepted(self, acm):
        """Every example value shipped in the README and docstring must pass,
        or the docs tell users to do something the tool rejects."""
        for case in [
            {
                "email": "security@company.com",
                "name": "Security Team",
                "phone": "+61-2-1234-5678",
                "title": "Security Operations",
            },
            {
                "email": "cloudsec@company.com",
                "name": "Cloud Security",
                "phone": "+1-555-0100",
                "title": "Cloud Security Team",
            },
            {
                "email": "sec@co.com",
                "name": "Sec Team",
                "phone": "+1-555-0100",
                "title": "SecOps",
            },
            # From the AWS documentation's own example.
            {
                "email": "mateo_jackson@amazon.com",
                "name": "Mateo Jackson",
                "phone": "+1(206)555-1234",
                "title": "Operations Manager",
            },
        ]:
            assert acm.validate_contact_fields(case) == [], case

    @pytest.mark.parametrize(
        "email",
        [
            "a+b@example.com",
            "a=b@example.com",
            "a.b@example.com",
            "a#b@example.com",
            "a|b@example.com",
            "a!b@example.com",
            "a&b@example.com",
            "a-b@example.com",
            "a_b@example.com",
            "user@sub.domain.example.com",
        ],
    )
    def test_permitted_email_characters(self, acm, email):
        assert acm.validate_contact_fields({"email": email}) == []

    @pytest.mark.parametrize(
        "email", ["notanemail", "user@localhost", "@example.com", "user@", "a b@example.com"]
    )
    def test_malformed_emails_rejected(self, acm, email):
        assert acm.validate_contact_fields({"email": email}) != []

    @pytest.mark.parametrize(
        "phone",
        ["+61-2-1234-5678", "+1 (206) 555 1234", "0212345678", "+15555550100", "(02) 1234 5678"],
    )
    def test_permitted_phone_formats(self, acm, phone):
        assert acm.validate_contact_fields({"phone": phone}) == []

    @pytest.mark.parametrize("phone", ["+1-555-CALL-NOW", "ext. 1234", "555.123.4567"])
    def test_phone_rejects_letters_and_dots(self, acm, phone):
        """The documented pattern is [\\s0-9()+-]+ — no letters, no dots."""
        assert acm.validate_contact_fields({"phone": phone}) != []

    @pytest.mark.parametrize(
        "field,limit",
        [("email", 254), ("name", 64), ("phone", 25), ("title", 50)],
    )
    def test_length_boundaries(self, acm, field, limit):
        # Build a value of exactly `limit` that also satisfies the pattern.
        if field == "email":
            value = "a" * (limit - len("@example.com")) + "@example.com"
        elif field == "phone":
            value = "+" + "1" * (limit - 1)
        else:
            value = "x" * limit
        assert len(value) == limit
        assert acm.validate_contact_fields({field: value}) == [], f"{limit} should pass"

        over = value + ("1" if field == "phone" else "x")
        errors = acm.validate_contact_fields({field: over})
        assert errors, f"{limit + 1} should fail"
        assert str(limit) in errors[0]
        assert str(limit + 1) in errors[0], "error should state the actual length"

    @pytest.mark.parametrize("field", ["email", "name", "phone", "title"])
    def test_empty_rejected(self, acm, field):
        errors = acm.validate_contact_fields({field: ""})
        assert len(errors) == 1
        assert "empty" in errors[0]

    def test_absent_and_none_are_skipped(self, acm):
        assert acm.validate_contact_fields({}) == []
        assert (
            acm.validate_contact_fields(
                {"email": None, "name": None, "phone": None, "title": None}
            )
            == []
        )

    def test_all_problems_reported_together(self, acm):
        """One usage error listing everything beats four sequential failures."""
        errors = acm.validate_contact_fields(
            {"email": "bad", "name": "N" * 100, "phone": "abc", "title": "T" * 99}
        )
        assert len(errors) == 4
        for flag in ("--email", "--name", "--phone", "--title"):
            assert any(flag in e for e in errors), flag

    @pytest.mark.parametrize(
        "field,value",
        [
            ("email", "junk user@example.com junk"),
            ("phone", "call +15555550100 now"),
        ],
    )
    def test_patterns_are_full_match_not_search(self, acm, field, value):
        """The API applies these as full matches, so a valid substring inside
        junk must be rejected."""
        assert acm.validate_contact_fields({field: value}) != []

    def test_rules_match_documented_api_limits(self, acm):
        rules = acm.CONTACT_FIELD_RULES
        assert rules["email"]["max_length"] == 254
        assert rules["name"]["max_length"] == 64
        assert rules["phone"]["max_length"] == 25
        assert rules["title"]["max_length"] == 50
        assert rules["email"]["api_field"] == "EmailAddress"
        assert rules["phone"]["api_field"] == "PhoneNumber"


class TestArgparseTypes:
    @pytest.mark.parametrize("value,expected", [("1", 1), ("10", 10), ("50", 50)])
    def test_worker_count_accepts_range(self, acm, value, expected):
        assert acm.worker_count(value) == expected

    @pytest.mark.parametrize("value", ["0", "51", "-1", "abc", "", "1.5"])
    def test_worker_count_rejects_out_of_range(self, acm, value):
        import argparse

        with pytest.raises(argparse.ArgumentTypeError):
            acm.worker_count(value)

    @pytest.mark.parametrize("value,expected", [("0", 0), ("1", 1), ("1500", 1500)])
    def test_non_negative_int_accepts(self, acm, value, expected):
        assert acm.non_negative_int(value) == expected

    @pytest.mark.parametrize("value", ["-1", "abc", ""])
    def test_non_negative_int_rejects(self, acm, value):
        import argparse

        with pytest.raises(argparse.ArgumentTypeError):
            acm.non_negative_int(value)


class TestRateLimitConstants:
    def test_match_documented_quotas(self, acm):
        """From https://docs.aws.amazon.com/accounts/latest/reference/quotas.html
        Delete is an order of magnitude tighter than the others."""
        assert acm.API_RATE_LIMITS["GetAlternateContact"] == "10/sec, burst 15"
        assert acm.API_RATE_LIMITS["PutAlternateContact"] == "5/sec, burst 8"
        assert acm.API_RATE_LIMITS["DeleteAlternateContact"] == "1/sec, burst 6"

    def test_delete_default_is_lower_than_update(self, acm):
        assert acm.DEFAULT_WORKERS["delete"] < acm.DEFAULT_WORKERS["update"]

    def test_every_action_has_a_worker_default(self, acm):
        assert set(acm.DEFAULT_WORKERS) == {"list", "update", "delete"}
