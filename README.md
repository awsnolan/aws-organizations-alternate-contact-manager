# AWS Organizations Alternate Contact Manager

Bulk manage (list, update, delete) alternate contacts across all member accounts in an AWS Organization. Designed for organizations with hundreds of accounts where manual updates via the console aren't practical.

## Features

- **Fast** — Thread pool execution processes 500 accounts in under a minute
- **Safe** — Dry-run mode previews all changes before applying
- **Idempotent** — Skips accounts that already have the correct contact configured
- **Flexible targeting** — All accounts, specific account IDs, or by Organizational Unit (recursive)
- **Audit trail** — Automatic CSV/JSON report of every action taken
- **Zero dependencies** — Only requires boto3 (pre-installed in CloudShell)
- **Resilient** — Adaptive retry with exponential backoff handles API throttling gracefully

## Prerequisites

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

# Apply for real
python3 aws_alternate_contact_manager.py update --accounts all --type security \
    --name "Security Team" \
    --email security@company.com \
    --phone "+61-2-1234-5678" \
    --title "Security Operations"
```

## Usage

```
usage: aws_alternate_contact_manager.py [-h] (--accounts ACCOUNTS | --ou OU)
                                        --type
                                        {billing,operations,security,all}
                                        [--name NAME] [--email EMAIL]
                                        [--phone PHONE] [--title TITLE]
                                        [--dry-run] [--force]
                                        [--workers WORKERS]
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
| `--workers N` | 10 | Number of parallel threads |
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
    --force
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

The script produces a summary on completion:

```
══════════════════════════════════════════════════════════════
  RESULTS SUMMARY
══════════════════════════════════════════════════════════════
  Total operations:  500
  Time elapsed:      47.3s
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

## API Rate Limits

The [AWS Account Management API](https://docs.aws.amazon.com/accounts/latest/reference/quotas.html) has a default quota of 5 transactions per second. The script uses:
- **Adaptive retry mode** (exponential backoff) built into boto3
- **10 parallel workers** (configurable via `--workers`)

This combination handles throttling gracefully without manual sleep statements.

## License

This project is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
