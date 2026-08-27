"""Shared fixtures.

The module under test is a single script at the repo root, so it is imported by
path rather than as an installed package.

Credentials are set to dummy values for the whole session so that any call that
escapes a stub fails loudly instead of reaching AWS.
"""

import sys
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session", autouse=True)
def dummy_aws_credentials():
    """Guarantee no test can reach real AWS."""
    import os

    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_PROFILE": "",
    }.items():
        os.environ[key] = value
    os.environ.pop("AWS_PROFILE", None)


@pytest.fixture(scope="session")
def acm(dummy_aws_credentials):
    """The module under test."""
    import aws_alternate_contact_manager

    return aws_alternate_contact_manager


# --- Constants used across tests -------------------------------------------

CALLER_ACCOUNT = "111111111111"
MEMBER_ACCOUNT = "222222222222"
OTHER_MEMBER = "333333333333"

DESIRED_CONTACT = {
    "EmailAddress": "new@example.com",
    "Name": "New Team",
    "PhoneNumber": "+15555550100",
    "Title": "Security Operations",
}

EXISTING_CONTACT = {
    "AlternateContactType": "SECURITY",
    "EmailAddress": "old@example.com",
    "Name": "Old Team",
    "PhoneNumber": "+15555550199",
    "Title": "Old Title",
}


@pytest.fixture
def caller():
    return CALLER_ACCOUNT


@pytest.fixture
def member():
    return MEMBER_ACCOUNT


@pytest.fixture
def desired():
    return dict(DESIRED_CONTACT)


@pytest.fixture
def existing():
    return dict(EXISTING_CONTACT)


@pytest.fixture
def account_client():
    """A real botocore client with no credentials resolution, for stubbing."""
    return boto3.client(
        "account",
        region_name="us-east-1",
        config=Config(retries={"max_attempts": 1}),
    )


@pytest.fixture
def stub(account_client):
    """Stubber that asserts exact request parameters and full consumption."""
    stubber = Stubber(account_client)
    stubber.activate()
    yield stubber
    stubber.assert_no_pending_responses()
    stubber.deactivate()
