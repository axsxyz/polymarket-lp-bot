#!/usr/bin/env python3
"""
Token Allowance Setup for Polymarket CLOB (EOA / MetaMask wallets).

Before the LP bot can trade, your wallet must approve the Polymarket exchange
contracts to spend your USDC (ERC20) and Conditional Tokens (ERC1155) on Polygon.

This script:
  1. Checks current on-chain allowances
  2. Sends approve / setApprovalForAll transactions for any that are insufficient
  3. Calls the CLOB API's update_balance_allowance to sync server state

Run once per wallet. After approval, you can trade freely.

Usage:
    python scripts/setup_allowances.py              # check + approve if needed
    python scripts/setup_allowances.py --check-only  # just check, don't send txs
    python scripts/setup_allowances.py --revoke       # revoke all approvals
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

# ──────────────────────────────────────────────────────────────────────
# Constants — Polygon Mainnet
# ──────────────────────────────────────────────────────────────────────

POLYGON_RPC = os.environ.get("POLYGON_RPC_URL", "https://polygon.drpc.org")

# USDC.e on Polygon (ERC20)
USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

# Polymarket Conditional Tokens (ERC1155 — uses setApprovalForAll, NOT approve)
CONDITIONAL_TOKENS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

SPENDERS = {
    "CTF Exchange": Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    "Neg Risk Exchange": Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a"),
    "Neg Risk Adapter": Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
}

MAX_UINT256 = 2**256 - 1
MIN_ALLOWANCE = 10**18

ERC20_ABI = [
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC1155_ABI = [
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _format_allowance(val, is_erc1155: bool = False) -> str:
    if is_erc1155:
        return "APPROVED" if val else "NOT SET"
    if val >= MAX_UINT256 // 2:
        return "UNLIMITED"
    if val == 0:
        return "0"
    return f"{val:,}"


def check_all(w3: Web3, owner: str) -> list[dict]:
    """Return a list of dicts describing each token/spender pair and its status."""
    owner = Web3.to_checksum_address(owner)
    results = []

    usdc = w3.eth.contract(address=USDC, abi=ERC20_ABI)
    ct = w3.eth.contract(address=CONDITIONAL_TOKENS, abi=ERC1155_ABI)

    for spender_label, spender_addr in SPENDERS.items():
        # USDC (ERC20)
        try:
            allowance = usdc.functions.allowance(owner, spender_addr).call()
        except Exception:
            allowance = 0
        results.append({
            "token": "USDC",
            "token_addr": USDC,
            "spender": spender_label,
            "spender_addr": spender_addr,
            "is_erc1155": False,
            "value": allowance,
            "ok": allowance >= MIN_ALLOWANCE,
        })

        # Conditional Tokens (ERC1155)
        try:
            approved = ct.functions.isApprovedForAll(owner, spender_addr).call()
        except Exception:
            approved = False
        results.append({
            "token": "Conditional Tokens",
            "token_addr": CONDITIONAL_TOKENS,
            "spender": spender_label,
            "spender_addr": spender_addr,
            "is_erc1155": True,
            "value": approved,
            "ok": bool(approved),
        })

    return results


def print_status(w3: Web3, owner: str) -> list[dict]:
    """Print formatted status table. Returns the check results."""
    owner = Web3.to_checksum_address(owner)

    usdc_contract = w3.eth.contract(address=USDC, abi=ERC20_ABI)
    try:
        usdc_balance = usdc_contract.functions.balanceOf(owner).call()
        usdc_decimals = usdc_contract.functions.decimals().call()
    except Exception:
        usdc_balance = 0
        usdc_decimals = 6
    matic_balance = w3.eth.get_balance(owner)

    print(f"\n  Wallet:        {owner}")
    print(f"  MATIC balance: {Web3.from_wei(matic_balance, 'ether'):.4f} MATIC")
    print(f"  USDC balance:  {usdc_balance / 10**usdc_decimals:,.2f} USDC")
    print()

    checks = check_all(w3, owner)

    print(f"  {'Token':<22} {'Spender':<22} {'Allowance':<20} {'Status'}")
    print(f"  {'─' * 22} {'─' * 22} {'─' * 20} {'─' * 10}")

    for c in checks:
        formatted = _format_allowance(c["value"], c["is_erc1155"])
        status = "OK" if c["ok"] else "NEEDS APPROVAL"
        icon = "  " if c["ok"] else "!! "
        print(f"{icon}{c['token']:<22} {c['spender']:<22} {formatted:<20} {status}")

    print()

    if matic_balance < Web3.to_wei(0.01, "ether"):
        print("  WARNING: Low MATIC balance — need MATIC on Polygon for gas.")
        print("  Send at least 0.1 MATIC to your wallet before approving.\n")

    return checks


def send_erc20_approve(w3: Web3, account, token_addr: str, spender: str, amount: int, nonce: int) -> str:
    contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    tx = contract.functions.approve(spender, amount).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 80_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": 137,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def send_erc1155_set_approval(w3: Web3, account, token_addr: str, operator: str, approved: bool, nonce: int) -> str:
    contract = w3.eth.contract(address=token_addr, abi=ERC1155_ABI)
    tx = contract.functions.setApprovalForAll(operator, approved).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 80_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": 137,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def sync_clob_allowances(private_key: str, funder: str, sig_type: int):
    """Call the CLOB API update_balance_allowance to sync server state."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams

        kwargs = {
            "host": "https://clob.polymarket.com",
            "key": private_key,
            "chain_id": 137,
            "signature_type": sig_type,
        }
        if funder:
            kwargs["funder"] = funder

        client = ClobClient(**kwargs)
        client.set_api_creds(client.create_or_derive_api_creds())

        # SDK's AssetType is broken (subclasses `enumerate` instead of `Enum`),
        # so we pass raw strings which is what the SDK uses internally anyway.
        for label in ["COLLATERAL", "CONDITIONAL"]:
            resp = client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=label, signature_type=sig_type)
            )
            print(f"  CLOB sync ({label}): {resp}")

    except Exception as exc:
        print(f"  CLOB sync failed (non-critical): {exc}")
        print("  The CLOB will pick up on-chain allowances automatically within minutes.")


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Set up Polymarket token allowances")
    parser.add_argument("--check-only", action="store_true", help="Only check, don't send transactions")
    parser.add_argument("--revoke", action="store_true", help="Revoke all approvals (set to 0)")
    parser.add_argument("--rpc", default=POLYGON_RPC, help="Polygon RPC URL")
    args = parser.parse_args()

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    private_key = os.environ.get("PRIVATE_KEY", "")
    funder = os.environ.get("FUNDER_ADDRESS", "")
    sig_type = int(os.environ.get("SIGNATURE_TYPE", "0"))

    if not private_key or private_key == "0xYOUR_PRIVATE_KEY_HERE":
        print("ERROR: Set PRIVATE_KEY in .env first.")
        sys.exit(1)

    account = Account.from_key(private_key)
    owner = funder if funder else account.address

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        print(f"ERROR: Cannot connect to {args.rpc}")
        sys.exit(1)

    print("\n" + "=" * 72)
    print("  Polymarket Token Allowance Setup")
    print("=" * 72)

    checks = print_status(w3, Web3.to_checksum_address(owner))

    if args.check_only:
        needs = sum(1 for c in checks if not c["ok"])
        if needs == 0:
            print("  All allowances are set. You're ready to trade!")
        else:
            print(f"  {needs} allowance(s) need approval. Run without --check-only to fix.")
        return

    # Determine what needs action
    to_fix = []
    for c in checks:
        if args.revoke and c["ok"]:
            to_fix.append(c)
        elif not args.revoke and not c["ok"]:
            to_fix.append(c)

    if not to_fix:
        action_word = "revoke" if args.revoke else "approve"
        print(f"  Nothing to {action_word}.")
        return

    action = "Revoking" if args.revoke else "Approving"
    nonce = w3.eth.get_transaction_count(account.address)
    txs_sent = 0

    for c in to_fix:
        label = f"{c['token']} for {c['spender']}"
        print(f"  {action} {label}...", end=" ", flush=True)

        try:
            if c["is_erc1155"]:
                approved = not args.revoke
                tx_hash = send_erc1155_set_approval(
                    w3, account, c["token_addr"], c["spender_addr"], approved, nonce,
                )
            else:
                amount = 0 if args.revoke else MAX_UINT256
                tx_hash = send_erc20_approve(
                    w3, account, c["token_addr"], c["spender_addr"], amount, nonce,
                )

            print(f"tx: {tx_hash}")
            print(f"    https://polygonscan.com/tx/{tx_hash}")

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if receipt["status"] == 1:
                print(f"    Confirmed in block {receipt['blockNumber']}")
                nonce += 1
                txs_sent += 1
            else:
                print(f"    REVERTED — check on Polygonscan")
                nonce += 1  # nonce still consumed on revert

            time.sleep(0.5)

        except Exception as exc:
            print(f"FAILED: {exc}")
            # Re-fetch nonce in case of issues
            nonce = w3.eth.get_transaction_count(account.address)

    if txs_sent > 0:
        print(f"\n  {txs_sent} transaction(s) confirmed. Syncing with CLOB API...")
        sync_clob_allowances(private_key, funder, sig_type)

    print()
    final_checks = print_status(w3, Web3.to_checksum_address(owner))

    all_ok = all(c["ok"] for c in final_checks)
    if all_ok and not args.revoke:
        print("  All allowances set. You're ready to trade!")
    elif args.revoke:
        print("  All approvals revoked.")

    print()


if __name__ == "__main__":
    main()
