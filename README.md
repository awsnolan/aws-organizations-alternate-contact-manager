# AWS Organizations Alternate Contact Manager

Bulk manage (list, update, delete) alternate contacts across all member accounts in an AWS Organization. Designed for organizations with hundreds of accounts where manual updates via the console aren't practical.

## Features

- **Concurrent** — Thread pool execution, with worker defaults tuned per action against the documented API rate limits
- **Safe** — Dry-run mode previews all changes before applying, a `--max-changes` ceiling blocks accidentally org-wide runs, and reports record the values that were replaced
- **Idempotent** — Skips accounts that already have the correct contact configured
- **Flexible targeting** — All accounts, specific account IDs, or by Organizational Unit (recursive)
- **Audit trail** — Automatic CSV/JSON report of every action taken
- **Zero dependencies** — Only requires boto3 (pre-installed in CloudShell)
- **Resilient** — Adaptive retry with exponential backoff handles API throttling gracefully

## Prerequisites

0. **Python 3.9 or later**, with `boto3` available (both are preinstalled in CloudShell).
   3.10+ is recommended — [boto3 dropped Python 3.9 support in April 2026](https://aws.amazon.com/blogs/developer/python-support-policy-updates-for-aws-sdks-and-tools/).

1. **AWS Organizations with all features enabled** ([docs](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_org_support-all-features.html))

2. **Trusted access enabled for Account Management**:
   ```bash
   aws organizations enable-aws-service-access --service-principal account.amazonaws.com
   ```

3. **IAM permissions** on the calling principal (management account or delegated admin).

   Replace `111111111111` with your management account ID, `o-exampleorgid` with your
   organization ID, and `r-exam` with your root ID:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "ListAllAccountsInOrg",
         "Effect": "Allow",
         "Action": "organizations:ListAccounts",
         "Resource": "*"
       },
       {
         "Sid": "ResolveOuMembership",
         "Effect": "Allow",
         "Action": [
           "organizations:ListAccountsForParent",
           "organizations:ListOrganizationalUnitsForParent"
         ],
         "Resource": [
           "arn:aws:organizations::111111111111:root/o-exampleorgid/r-exam",
           "arn:aws:organizations::111111111111:ou/o-exampleorgid/*"
         ]
       },
       {
         "Sid": "ManageAlternateContacts",
         "Effect": "Allow",
         "Action": [
           "account:GetAlternateContact",
           "account:PutAlternateContact",
           "account:DeleteAlternateContact"
         ],
         "Resource": [
           "arn:aws:account::111111111111:account",
           "arn:aws:account::111111111111:account/o-exampleorgid/*"
         ]
       }
     ]
   }
   ```

   Notes on scoping:

   - `organizations:ListAccounts` defines no resource types, so `"Resource": "*"` is the
     only valid option for it. Every other action here can be scoped, and is.
   - The bare `arn:aws:account::111111111111:account` entry is the *calling* account.
     Omit it if this principal should not be able to change the management account's own
     contacts, and keep only the `o-.../*` entry for member accounts.
   - To restrict writes to part of the organization, add an
     `account:AccountResourceOrgPaths` condition on the third statement. `--ou` is a
     client-side flag, so IAM is the only place this can actually be enforced.
   - To restrict which contact types a principal may touch, add an
     `account:AlternateContactTypes` condition.
   - `sts:GetCallerIdentity` is also called, but requires no IAM permission.

   For read-only use (`list` action), grant only `organizations:ListAccounts`,
   the two OU-resolution actions, and `account:GetAlternateContact`.

   > This policy is derived from the [Account Management](https://docs.aws.amazon.com/service-authorization/latest/reference/list_account.html)
   > and [Organizations](https://docs.aws.amazon.com/service-authorization/latest/reference/list_organizations.html)
   > service authorization references. Validate it against your organization with IAM
   > Access Analyzer before relying on it.

## Quick Start

```bash
# Download the script (or clone this repo)
wget https://raw.githubusercontent.com/awsnolan/aws-organizations-alternate-contact-manager/main/aws_alternate_contact_manager.py

# Dry-run first — preview what would change
python3 aws_alternate_contact_manager.py update --accounts all --type security \
    --name "Security Team" \
    --email security@company.com \
    --phone "+61-2-1234-5678" \
    --title "Security Operations" \
    --dry-run

# Apply for real. Org-wide runs exceed the default --max-changes ceiling of 50,
# so the intended scope has to be stated explicitly — use the operation count
# the dry-run reported.
python3 aws_alternate_contact_manager.py update --accounts all --type security \
    --name "Security Team" \
    --email security@company.com \
    --phone "+61-2-1234-5678" \
    --title "Security Operations" \
    --max-changes 500
```

The two-step shape is deliberate: preview with `--dry-run`, then re-run with a ceiling
that matches what the preview reported. See
[Guarding against over-broad runs](#guarding-against-over-broad-runs).

## Usage

```
usage: aws_alternate_contact_manager.py [-h] (--accounts ACCOUNTS | --ou OU)
                                        --type
                                        {billing,operations,security,all}
                                        [--name NAME] [--email EMAIL]
                                        [--phone PHONE] [--title TITLE]
                                        [--dry-run] [--force] [--workers N]
                                        [--max-changes N]
                                        [--output {csv,json,both,none}]
                                        [--output-dir OUTPUT_DIR] [--verbose]
                                        {list,update,delete}
```

### Actions

| Action | Description |
|--------|-------------|
| `list` | Display current alternate contacts across accounts |
| `update` | Set or update alternate contacts (requires `--name`, `--email`, `--phone`, `--title`) |
| `delete` | Remove alternate contacts from specified accounts |

### Contact field limits

`update` validates these locally before making any API call, so a malformed value fails
once as a usage error rather than once per account mid-run. Limits are from the
[`PutAlternateContact` reference](https://docs.aws.amazon.com/accounts/latest/APIReference/API_PutAlternateContact.html):

| Flag | Max length | Accepted format |
|------|-----------|-----------------|
| `--email` | 254 | `user@example.com`; local part also allows `+ = . # \| ! & - _` |
| `--name` | 64 | any |
| `--phone` | 25 | digits, spaces, and `+ - ( )` only — no letters |
| `--title` | 50 | any |

### Targeting accounts

| Flag | Description |
|------|-------------|
| `--accounts all` | All active accounts in the organization |
| `--accounts 111111111111,222222222222` | Specific account IDs (comma-separated) |
| `--ou ou-xxxx-yyyyyyyy` | All accounts under an OU (recursively includes nested OUs) |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--dry-run` | off | Preview changes without applying them. Always reads current state, even with `--force`, so the preview shows what each account would change *from* |
| `--force` | off | Skip idempotency check — apply without checking current state (halves API calls). **Also forfeits the audit before-state**: previous contact values cannot be recorded in the report because they are never read. Omit `--force` if you want a recovery record |
| `--workers N` | per action | Parallel threads, 1-50. Defaults: `list` 10, `update` 10, `delete` 3. See [API rate limits](#api-rate-limits) |
| `--max-changes N` | 50 | Refuse to run if more than N writes would be performed. `0` disables. Applies to `update`/`delete`; `list` and `--dry-run` exempt |
| `--output {csv,json,both,none}` | csv | Report format |
| `--output-dir PATH` | `.` | Directory for report files |
| `--verbose` / `-v` | off | Enable debug logging |

## Examples

### Fastest bulk update (skip idempotency check)

When you know the contacts are unset (e.g. first-time setup across 500 accounts), use `--force` to skip the GET call before each PUT — halves the total API calls. Because nothing is read first, the report will not record previous values; that's an acceptable trade only when you know there's nothing to overwrite:

```bash
python3 aws_alternate_contact_manager.py update \
    --accounts all \
    --type security \
    --name "Security Team" \
    --email security@company.com \
    --phone "+61-2-1234-5678" \
    --title "Security Operations" \
    --force \
    --max-changes 500
```

### Update security contact for an entire OU

```bash
python3 aws_alternate_contact_manager.py update \
    --ou ou-abc1-23456789 \
    --type security \
    --name "Cloud Security" \
    --email cloudsec@company.com \
    --phone "+1-555-0100" \
    --title "Cloud Security Team"
```

If the OU holds more than 50 accounts this will abort until you pass a matching
`--max-changes`; the error message reports the exact count to use.

### List all alternate contacts and export to JSON

```bash
python3 aws_alternate_contact_manager.py list \
    --accounts all \
    --type all \
    --output json
```

### Delete billing contact from specific accounts

```bash
python3 aws_alternate_contact_manager.py delete \
    --accounts 111111111111,222222222222 \
    --type billing
```

## Running in CloudShell

1. Open [AWS CloudShell](https://console.aws.amazon.com/cloudshell/) in the **management account**
2. Download and run:
   ```bash
   wget https://raw.githubusercontent.com/awsnolan/aws-organizations-alternate-contact-manager/main/aws_alternate_contact_manager.py
   python3 aws_alternate_contact_manager.py update --accounts all --type security \
       --name "Security Team" --email security@company.com \
       --phone "+61-2-1234-5678" --title "Security Operations" --dry-run
   ```

> **Note**: CloudShell has a 20-minute idle timeout. The script outputs progress continuously to prevent disconnection, but for very large organizations (1000+ accounts), consider running from an EC2 instance or locally with configured credentials.

## Output

The script produces a summary on completion (shape shown; elapsed time depends on
account count, action, and how the API rate limits apply to your organization):

```
══════════════════════════════════════════════════════════════
  RESULTS SUMMARY
══════════════════════════════════════════════════════════════
  Total operations:  500
  Time elapsed:      <varies>
──────────────────────────────────────────────────────────────
  ✓ Updated: 483
  ─ Skipped (already correct): 17
══════════════════════════════════════════════════════════════
```

A CSV/JSON report is saved with per-account details for audit purposes.

For `update` and `delete`, the report also records the values that were replaced or
removed in `previous_name`, `previous_email`, `previous_phone`, and `previous_title`.
Keep these reports — they are the only record of what a run overwrote, and the source
you would restore from. They contain contact PII, so treat them accordingly; the
included `.gitignore` keeps them out of version control.

## API rate limits

Rates differ substantially per operation — there is no single figure. From the
[Account Management quotas](https://docs.aws.amazon.com/accounts/latest/reference/quotas.html):

| Operation | Rate | Burst |
|---|---|---|
| `GetAlternateContact` | 10/sec | 15 |
| `PutAlternateContact` | 5/sec | 8 |
| `DeleteAlternateContact` | **1/sec** | 6 |

Delete is an order of magnitude tighter than the others, so worker defaults are set
per action rather than globally:

| Action | Default workers |
|---|---|
| `list` | 10 |
| `update` | 10 |
| `delete` | 3 |

`--workers` accepts 1-50 and is validated. Raising it past the defaults for `delete`
prints a warning, because threads above the sustained rate spend their time in retry
backoff rather than doing work.

Throttling is absorbed by boto3's **adaptive retry mode** (8 attempts, exponential
backoff), so runs over the rate degrade in speed rather than failing.

> Worker count is not a rate limiter. The quotas above are documented "per account",
> and the docs distinguish that from "per caller account" elsewhere in the same table
> without defining it for these operations — so whether the ceiling applies to the
> calling principal or to each target account is unclear. If it is per caller,
> concurrency past the sustained rate buys nothing. Measure against your own
> organization before tuning `--workers` upward.

## Guarding against over-broad runs

`update` and `delete` refuse to run if they would perform more than `--max-changes`
write operations (default **50**). The check happens before any write is issued.

```
  ✗ ABORTED: this run would perform 1500 write operations,
             above the --max-changes ceiling of 50.

             500 account(s) × 3 contact type(s) = 1500 writes

             Nothing has been changed. Review the scope first:
                 --dry-run

             If this is intended, raise the ceiling explicitly:
                 --max-changes 1500
```

This exists because `delete --accounts all --type all` is otherwise a single command
that clears every alternate contact in the organization. `list` and `--dry-run` are
exempt — neither changes anything, and `--dry-run` is how you inspect scope. Pass
`--max-changes 0` to disable the ceiling entirely.

Note that this is a client-side guard. To enforce scope in a way that cannot be
bypassed by editing the script or calling the API directly, use the
`account:AccountResourceOrgPaths` IAM condition described in the Prerequisites.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | One or more operations failed (see report) |
| 2 | Usage error — bad account ID, account not in organization |
| 3 | Aborted by the `--max-changes` ceiling; nothing was changed |
| 130 | Interrupted with Ctrl+C; partial results saved to report |

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

pytest                      # test suite with coverage floor
ruff check .                # lint
bandit -r aws_alternate_contact_manager.py
```

The suite uses `botocore.stub.Stubber`, which validates *exact outgoing request
parameters* rather than resulting state. That matters most for the rule that the
caller's own account must not receive an `AccountId` parameter — a test can assert
the absence of a field directly. Dummy credentials are set for the whole session so
that any call escaping a stub fails loudly instead of reaching AWS. No AWS account is
needed to run the tests.

CI runs lint, the suite across Python 3.9-3.13, and three documentation checks: that
the IAM policy in this README is valid JSON, that the usage block above still matches
`--help`, and that no unexpected 12-digit identifiers have been committed.

### What the tests do not cover

- Whether the organization has all-features and trusted access enabled
- Real IAM evaluation — Stubber has no authorization layer, so the policy above is
  unverified against a live organization
- Actual throttling behaviour under the documented rate limits
- The `KeyboardInterrupt` path in `run_operation`, which cannot be triggered
  deterministically

A run against a non-production organization is still the meaningful end-to-end check,
particularly for the management-account path.

## License

This project is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
