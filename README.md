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

3. **IAM permissions** on the calling principal (management account or delegated admin):
   ```json
   {
     "Effect": "Allow",
     "Action": [
       "account:PutAlternateContact",
       "account:GetAlternateContact",
       "account:DeleteAlternateContact",
       "organizations:ListAccounts",
       "organizations:ListAccountsForParent",
       "organizations:ListOrganizationalUnitsForParent"
     ],
     "Resource": "*"
   }
   ```

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
                                         --type {billing,operations,security,all}
                                         [--name NAME] [--email EMAIL]
                                         [--phone PHONE] [--title TITLE]
                                         [--dry-run] [--workers WORKERS]
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
| `--dry-run` | off | Preview changes without applying them |
| `--force` | off | Skip idempotency check — apply without checking current state (halves API calls) |
| `--workers N` | 10 | Number of parallel threads |
| `--output {csv,json,both,none}` | csv | Report format |
| `--output-dir PATH` | `.` | Directory for report files |
| `--verbose` / `-v` | off | Enable debug logging |

## Examples

### Fastest bulk update (skip idempotency check)

When you know the contacts are unset (e.g. first-time setup across 500 accounts), use `--force` to skip the GET call before each PUT — halves the total API calls:

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

## API Rate Limits

The [AWS Account Management API](https://docs.aws.amazon.com/accounts/latest/reference/quotas.html) has a default quota of 5 transactions per second. The script uses:
- **Adaptive retry mode** (exponential backoff) built into boto3
- **10 parallel workers** (configurable via `--workers`)

This combination handles throttling gracefully without manual sleep statements.

## License

This project is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
