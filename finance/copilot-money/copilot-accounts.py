#!/usr/bin/env python3
"""
Copilot Money — account list
Reads per-account widget JSON files and Copilot's own credit/other classification.
No auth needed — local files only.
"""

import json
import glob
import sys
import os
from agentos import claims, returns, test

WIDGET_DIR = os.path.expanduser(
    "~/Library/Group Containers/group.com.copilot.production/widget-data"
)


def _load_credit_ids():
    """Load the set of account IDs that Copilot classifies as credit accounts."""
    path = os.path.join(WIDGET_DIR, "widgets-account-credit_accounts.json")
    try:
        with open(path) as f:
            return {a["id"] for a in json.load(f)}
    except Exception:
        return set()


def _classify_account(data, credit_ids):
    """
    Classify an account into tags based on Copilot's own groupings
    and lightweight heuristics. Returns a list of tag names.
    """
    tags = ["financial"]
    account_id = data.get("id")
    name = data.get("name", "")
    institution_id = data.get("institutionId", "")

    # Credit classification from Copilot's own credit_accounts.json
    if account_id in credit_ids:
        tags.append("credit")
    # Crypto
    elif institution_id == "coinbase":
        tags.append("crypto")
    # Name-based heuristics for the rest
    elif any(kw in name.lower() for kw in ("ira", "roth", "401k", "401(k)")):
        tags.append("retirement")
        tags.append("brokerage")
    elif "hsa" in name.lower():
        tags.append("hsa")
        tags.append("brokerage")
    elif "savings" in name.lower():
        tags.append("savings")
    elif "checking" in name.lower():
        tags.append("checking")
    else:
        tags.append("brokerage")

    # Tax treatment
    if any(t in tags for t in ("retirement", "hsa")):
        tags.append("tax-free")
    else:
        tags.append("taxable")

    return tags


@test
@returns("financial_account[]")
@claims("primary_user")
async def load_accounts(**params):
    """List all financial accounts with balances and institution info.

    Each account is a `financial_account` node with `(at, identifier)`
    identity: `at` is the institution (Coinbase, Chase, etc. — an
    organization node, or "Copilot" when Copilot doesn't expose the
    underlying bank), `identifier` is Copilot's account id hash.
    """
    credit_ids = _load_credit_ids()
    pattern = os.path.join(WIDGET_DIR, "widgets-account-account_*.json")
    accounts = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path) as f:
                data = json.load(f)
            # Skip placeholder accounts
            if not data.get("name") or data.get("name") == "Account name":
                continue

            tags = _classify_account(data, credit_ids)
            institution_id = data.get("institutionId") or "copilot"
            # Copilot exposes institution slugs (e.g. "coinbase", "chase"),
            # not full names — use the account name as a human label on the
            # institution node if that's the only signal we have.
            institution_name = institution_id.replace("_", " ").title()

            # Map classification tags to accountType. "credit" is a card
            # account, "checking"/"savings"/"brokerage"/"crypto"/"hsa"
            # are the balance-account flavours.
            account_type = next(
                (t for t in tags if t in ("credit", "checking", "savings", "brokerage", "crypto", "hsa")),
                "brokerage",
            )

            accounts.append({
                "id": data.get("id"),
                "identifier": data.get("id"),
                "at": {"shape": "organization", "name": institution_name, "url": f"https://{institution_id}.com"},
                "name": data.get("name"),
                "last4": data.get("mask"),
                "balance": data.get("balance"),
                "creditLimit": data.get("limit") or None,
                "accountType": account_type,
                "color": data.get("color"),
                "user_tag": tags,
            })
        except Exception as e:
            print(f"Warning: could not read {path}: {e}", file=sys.stderr)

    return accounts


if __name__ == "__main__":
    accounts = load_accounts()
    print(json.dumps(accounts, indent=2))
