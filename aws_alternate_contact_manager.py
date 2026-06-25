#!/usr/bin/env python3
"""
AWS Organizations Alternate Contact Manager - Production Ready
==============================================================

Bulk manage (list/update/delete) alternate contacts across all member accounts
in an AWS Organization. Designed for orgs with 500+ accounts.

Features:
  - Concurrent execution (thread pool) for fast processing
  - Adaptive retry with exponential backoff (handles API throttling)
  - Dry-run mode to preview changes before applying
  - Idempotency — skips accounts that already have the correct contact (use --force to override)
  - OU-based targeting (in addition to 'all' or comma-separated IDs)
  - CSV/JSON audit report of all actions taken
  - Progress indicator to prevent CloudShell idle timeout

Requirements:
  - Run from the management account or a delegated admin for Account Management
  - Trusted access enabled: aws organizations enable-aws-service-access \
        --service-principal account.amazonaws.com
  - IAM permissions: account:PutAlternateContact, account:GetAlternateContact,
    account:DeleteAlternateContact, organizations:ListAccounts,
    organizations:ListAccountsForParent, organizations:ListOrganizationalUnitsForParent

Usage examples:
  # Update security contact for ALL accounts (dry-run first)
  python3 aws_alternate_contact_manager.py update --accounts all --type security \\
      --name "Security Team" --email security@company.com \\
      --phone "+61-2-1234-5678" --title "Security Operations" --dry-run

  # Apply for real (skip idempotency check when you know contacts are unset)
  python3 aws_alternate_contact_manager.py update --accounts all --type security \\
      --name "Security Team" --email security@company.com \\
      --phone "+61-2-1234-5678" --title "Security Operations" --force

  # Target a specific OU
  python3 aws_alternate_contact_manager.py update --ou ou-abc1-23456789 --type security \\
      --name "Security Team" --email security@company.com \\
      --phone "+61-2-1234-5678" --title "Security Operations"

  # List current security contacts for all accounts, export to CSV
  python3 aws_alternate_contact_manager.py list --accounts all --type security

  # Delete billing contact for specific accounts
  python3 aws_alternate_contact_manager.py delete --accounts 111111111111,222222222222 --type billing

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""

import argparse
import boto3
import csv
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from botocore.config import Config
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOTO_CONFIG = Config(
    retries={"max_attempts": 8, "mode": "adaptive"},
    max_pool_connections=25,
)

MAX_WORKERS = 10  # Parallel threads (stay under 5 TPS with retries)
CONTACT_TYPES_ALL = ["BILLING", "OPERATIONS", "SECURITY"]

logger = logging.getLogger("alt-contact-mgr")


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------


def get_all_active_account_ids(org_client):
    """Paginate all ACTIVE account IDs in the organization."""
    paginator = org_client.get_paginator("list_accounts")
    return [
        acct["Id"]
        for page in paginator.paginate()
        for acct in page["Accounts"]
        if acct["Status"] == "ACTIVE"
    ]


def get_accounts_for_ou(org_client, ou_id):
    """
    Recursively collect all ACTIVE account IDs under an OU (including nested OUs).
    """
    accounts = []
    paginator_accounts = org_client.get_paginator("list_accounts_for_parent")
    paginator_ous = org_client.get_paginator("list_organizational_units_for_parent")

    # Direct accounts under this OU
    for page in paginator_accounts.paginate(ParentId=ou_id):
        for acct in page["Accounts"]:
            if acct["Status"] == "ACTIVE":
                accounts.append(acct["Id"])

    # Recurse into child OUs
    for page in paginator_ous.paginate(ParentId=ou_id):
        for child_ou in page["OrganizationalUnits"]:
            accounts.extend(get_accounts_for_ou(org_client, child_ou["Id"]))

    return accounts


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def get_current_contact(client, account_id, contact_type):
    """Fetch the current alternate contact. Returns dict or None if not set."""
    try:
        resp = client.get_alternate_contact(
            AccountId=account_id,
            AlternateContactType=contact_type,
        )
        return resp["AlternateContact"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def contact_matches(current, desired):
    """Check if the current contact already matches the desired state."""
    if current is None:
        return False
    return (
        current.get("EmailAddress") == desired.get("EmailAddress")
        and current.get("Name") == desired.get("Name")
        and current.get("PhoneNumber") == desired.get("PhoneNumber")
        and current.get("Title") == desired.get("Title")
    )


def process_update(client, account_id, contact_type, contact_info, force=False, dry_run=False):
    """
    Update a single account+type. Returns a result dict.
    Skips if already matching (idempotent) unless --force is set.
    """
    # Check current state for idempotency (skip if --force)
    if not force:
        current = get_current_contact(client, account_id, contact_type)
        if contact_matches(current, contact_info):
            return {
                "account_id": account_id,
                "contact_type": contact_type,
                "status": "skipped",
                "reason": "already_configured",
            }

        if dry_run:
            return {
                "account_id": account_id,
                "contact_type": contact_type,
                "status": "would_update",
                "current": current,
            }
    elif dry_run:
        return {
            "account_id": account_id,
            "contact_type": contact_type,
            "status": "would_update",
            "current": None,
        }

    # Apply the update
    client.put_alternate_contact(
        AccountId=account_id,
        AlternateContactType=contact_type,
        **contact_info,
    )
    return {
        "account_id": account_id,
        "contact_type": contact_type,
        "status": "updated",
    }


def process_delete(client, account_id, contact_type, force=False, dry_run=False):
    """Delete a single account+type alternate contact."""
    # Check if it exists first (skip if --force)
    if not force:
        current = get_current_contact(client, account_id, contact_type)
        if current is None:
            return {
                "account_id": account_id,
                "contact_type": contact_type,
                "status": "skipped",
                "reason": "not_set",
            }

        if dry_run:
            return {
                "account_id": account_id,
                "contact_type": contact_type,
                "status": "would_delete",
                "current": current,
            }
    elif dry_run:
        return {
            "account_id": account_id,
            "contact_type": contact_type,
            "status": "would_delete",
            "current": None,
        }

    client.delete_alternate_contact(
        AccountId=account_id,
        AlternateContactType=contact_type,
    )
    return {
        "account_id": account_id,
        "contact_type": contact_type,
        "status": "deleted",
    }


def process_list(client, account_id, contact_type):
    """List the current alternate contact for an account+type."""
    current = get_current_contact(client, account_id, contact_type)
    return {
        "account_id": account_id,
        "contact_type": contact_type,
        "status": "found" if current else "not_set",
        "contact": current,
    }


# ---------------------------------------------------------------------------
# Orchestrator (thread pool)
# ---------------------------------------------------------------------------


def run_operation(action, accounts, contact_types, contact_info=None, force=False, dry_run=False):
    """
    Execute the chosen action across all accounts × contact types using a thread pool.
    Returns a list of result dicts.
    """
    client = boto3.client("account", config=BOTO_CONFIG)
    results = []
    total_tasks = len(accounts) * len(contact_types)
    completed = 0

    def worker(account_id, ctype):
        if action == "update":
            return process_update(client, account_id, ctype, contact_info, force, dry_run)
        elif action == "delete":
            return process_delete(client, account_id, ctype, force, dry_run)
        elif action == "list":
            return process_list(client, account_id, ctype)

    # Build task list
    tasks = [(acct, ctype) for acct in accounts for ctype in contact_types]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_task = {
            executor.submit(worker, acct, ctype): (acct, ctype)
            for acct, ctype in tasks
        }

        for future in as_completed(future_to_task):
            acct, ctype = future_to_task[future]
            completed += 1

            try:
                result = future.result()
                results.append(result)
                status_icon = {
                    "updated": "✓",
                    "deleted": "✓",
                    "skipped": "─",
                    "found": "•",
                    "not_set": "○",
                    "would_update": "~",
                    "would_delete": "~",
                }.get(result["status"], "?")
                print(f"\r  [{completed}/{total_tasks}] {status_icon} {acct} [{ctype}]", end="", flush=True)

            except ClientError as e:
                error_msg = str(e)
                results.append({
                    "account_id": acct,
                    "contact_type": ctype,
                    "status": "error",
                    "error": error_msg,
                })
                print(f"\r  [{completed}/{total_tasks}] ✗ {acct} [{ctype}]: {error_msg}", end="", flush=True)

            except Exception as e:
                results.append({
                    "account_id": acct,
                    "contact_type": ctype,
                    "status": "error",
                    "error": str(e),
                })

    print()  # Newline after progress
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def write_csv_report(results, output_path):
    """Write results to a CSV file for audit trail."""
    fieldnames = ["timestamp", "account_id", "contact_type", "status", "reason",
                  "name", "email", "phone", "title", "error"]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        timestamp = datetime.now(timezone.utc).isoformat()
        for r in results:
            row = {
                "timestamp": timestamp,
                "account_id": r.get("account_id"),
                "contact_type": r.get("contact_type"),
                "status": r.get("status"),
                "reason": r.get("reason", ""),
                "error": r.get("error", ""),
            }
            # Include contact details for list action
            if r.get("contact"):
                row["name"] = r["contact"].get("Name", "")
                row["email"] = r["contact"].get("EmailAddress", "")
                row["phone"] = r["contact"].get("PhoneNumber", "")
                row["title"] = r["contact"].get("Title", "")
            writer.writerow(row)

    return output_path


def write_json_report(results, output_path):
    """Write results to a JSON file."""
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_accounts": len(set(r["account_id"] for r in results)),
        "results": results,
        "summary": summarize_results(results),
    }
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    return output_path


def summarize_results(results):
    """Generate a summary breakdown of statuses."""
    summary = {}
    for r in results:
        status = r.get("status", "unknown")
        summary[status] = summary.get(status, 0) + 1
    return summary


def print_summary(results, elapsed):
    """Print a human-readable summary."""
    summary = summarize_results(results)
    total = len(results)

    print(f"\n{'='*60}")
    print(f"  RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"  Total operations:  {total}")
    print(f"  Time elapsed:      {elapsed:.1f}s")
    print(f"{'─'*60}")

    status_labels = {
        "updated": ("  ✓ Updated:", "\033[92m"),
        "deleted": ("  ✓ Deleted:", "\033[92m"),
        "skipped": ("  ─ Skipped (already correct):", "\033[90m"),
        "would_update": ("  ~ Would update (dry-run):", "\033[93m"),
        "would_delete": ("  ~ Would delete (dry-run):", "\033[93m"),
        "found": ("  • Found:", "\033[0m"),
        "not_set": ("  ○ Not set:", "\033[90m"),
        "error": ("  ✗ Errors:", "\033[91m"),
    }

    reset = "\033[0m"
    for status, count in summary.items():
        label, color = status_labels.get(status, (f"  ? {status}:", "\033[0m"))
        print(f"{color}{label} {count}{reset}")

    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Bulk manage AWS alternate contacts across an Organization.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run update for all accounts
  %(prog)s update --accounts all --type security \\
      --name "Sec Team" --email sec@co.com --phone "+1-555-0100" --title "SecOps" --dry-run

  # Apply update, skip idempotency check (fastest for known-unset accounts)
  %(prog)s update --accounts all --type security \\
      --name "Sec Team" --email sec@co.com --phone "+1-555-0100" --title "SecOps" --force

  # Apply update to a specific OU
  %(prog)s update --ou ou-xxxx-yyyyyyyy --type security \\
      --name "Sec Team" --email sec@co.com --phone "+1-555-0100" --title "SecOps"

  # List all security contacts
  %(prog)s list --accounts all --type security

  # Delete operations contact for specific accounts
  %(prog)s delete --accounts 111111111111,222222222222 --type operations
        """,
    )

    parser.add_argument("action", choices=["list", "update", "delete"],
                        help="Action to perform")

    # Account targeting (mutually exclusive)
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--accounts", type=str,
                             help="Comma-separated account IDs, or 'all'")
    target_group.add_argument("--ou", type=str,
                             help="Organizational Unit ID (recursively includes nested OUs)")

    parser.add_argument("--type", choices=["billing", "operations", "security", "all"],
                        required=True, help="Alternate contact type")

    # Contact details (required for update)
    parser.add_argument("--name", help="Contact name")
    parser.add_argument("--email", help="Contact email address")
    parser.add_argument("--phone", help="Contact phone number (e.g. +61-2-1234-5678)")
    parser.add_argument("--title", help="Contact title/role")

    # Options
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without applying them")
    parser.add_argument("--force", action="store_true",
                        help="Skip idempotency check — apply to all accounts without "
                             "checking current state first (halves API calls)")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS,
                        help=f"Number of parallel threads (default: {MAX_WORKERS})")
    parser.add_argument("--output", choices=["csv", "json", "both", "none"], default="csv",
                        help="Report output format (default: csv)")
    parser.add_argument("--output-dir", type=str, default=".",
                        help="Directory for report files (default: current dir)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable verbose logging")

    args = parser.parse_args()

    # Validate: update requires contact info
    if args.action == "update":
        missing = [f for f in ["name", "email", "phone", "title"] if not getattr(args, f)]
        if missing:
            parser.error(f"--{', --'.join(missing)} required for update action")

    return args


def main():
    args = parse_args()

    # Logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print("\n┌─────────────────────────────────────────────────────────┐")
    print("│   AWS Organizations Alternate Contact Manager           │")
    print("└─────────────────────────────────────────────────────────┘\n")

    if args.dry_run:
        print("  ⚠️  DRY-RUN MODE — no changes will be made\n")

    if args.force:
        print("  ⚡ FORCE MODE — skipping idempotency checks\n")

    # -----------------------------------------------------------------------
    # Resolve target accounts
    # -----------------------------------------------------------------------
    org_client = boto3.client("organizations", config=BOTO_CONFIG)

    print("  Resolving target accounts...")
    if args.ou:
        accounts = get_accounts_for_ou(org_client, args.ou)
        print(f"  Found {len(accounts)} active accounts under OU {args.ou}")
    elif args.accounts == "all":
        accounts = get_all_active_account_ids(org_client)
        print(f"  Found {len(accounts)} active accounts in the organization")
    else:
        accounts = [a.strip() for a in args.accounts.split(",")]
        # Validate
        org_accounts = set(get_all_active_account_ids(org_client))
        invalid = [a for a in accounts if a not in org_accounts]
        if invalid:
            print(f"\n  ✗ ERROR: These accounts are not in your organization:")
            for a in invalid:
                print(f"      - {a}")
            sys.exit(1)
        print(f"  Targeting {len(accounts)} specified accounts")

    if not accounts:
        print("  No accounts found. Exiting.")
        sys.exit(0)

    # -----------------------------------------------------------------------
    # Resolve contact types
    # -----------------------------------------------------------------------
    if args.type == "all":
        contact_types = CONTACT_TYPES_ALL
    else:
        contact_types = [args.type.upper()]

    total_ops = len(accounts) * len(contact_types)
    print(f"  Action: {args.action.upper()}")
    print(f"  Contact types: {', '.join(contact_types)}")
    print(f"  Total operations: {total_ops}")
    print(f"  Parallel workers: {args.workers}")
    print()

    # -----------------------------------------------------------------------
    # Build contact info (for update)
    # -----------------------------------------------------------------------
    contact_info = None
    if args.action == "update":
        contact_info = {
            "EmailAddress": args.email,
            "Name": args.name,
            "PhoneNumber": args.phone,
            "Title": args.title,
        }

    # -----------------------------------------------------------------------
    # Execute
    # -----------------------------------------------------------------------
    global MAX_WORKERS
    MAX_WORKERS = args.workers

    tic = time.perf_counter()
    results = run_operation(
        action=args.action,
        accounts=accounts,
        contact_types=contact_types,
        contact_info=contact_info,
        force=args.force,
        dry_run=args.dry_run,
    )
    elapsed = time.perf_counter() - tic

    # -----------------------------------------------------------------------
    # Report
    # -----------------------------------------------------------------------
    print_summary(results, elapsed)

    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_base = f"alternate_contact_report_{timestamp_str}"

    if args.output in ("csv", "both"):
        csv_path = write_csv_report(results, f"{args.output_dir}/{report_base}.csv")
        print(f"  📄 CSV report: {csv_path}")

    if args.output in ("json", "both"):
        json_path = write_json_report(results, f"{args.output_dir}/{report_base}.json")
        print(f"  📄 JSON report: {json_path}")

    # Exit code based on errors
    errors = [r for r in results if r.get("status") == "error"]
    if errors:
        print(f"\n  ⚠️  {len(errors)} operations failed. Check the report for details.")
        sys.exit(1)

    print()


if __name__ == "__main__":
    main()
