#!/usr/bin/env python3
"""
Token Allowance Setup for Polymarket CLOB V2 (EOA / MetaMask wallets).

Before the LP bot can trade on CLOB V2, your wallet must:
  - Hold pUSD collateral (wrap USDC / USDC.e → pUSD via CollateralOnramp), and
  - Approve the Polymarket exchange contracts to spend pUSD (ERC20) and
    Conditional Tokens (ERC1155) on Polygon.

This script:
  1. Optionally wraps USDC (native or bridged) → pUSD (CollateralOnramp.wrap)
  2. Checks current on-chain allowances for pUSD + Conditional Tokens
  3. Sends approve / setApprovalForAll for any that are insufficient
  4. Calls the CLOB API update_balance_allowance (V2 SDK) to sync server state

Usage:
    python scripts/setup_allowances.py              # interactive wrap only (no allowances)
    python scripts/setup_allowances.py --wrap       # same (alias)
    python scripts/setup_allowances.py --wrap-interactive
    python scripts/setup_allowances.py --all        # auto-wrap both USDC sources + allowances
    python scripts/setup_allowances.py --wrap-only  # auto-wrap both USDC sources, no allowances
    python scripts/setup_allowances.py --approve-only
    python scripts/setup_allowances.py --check-only
    python scripts/setup_allowances.py --revoke
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

# ──────────────────────────────────────────────────────────────────────
# Constants — Polygon Mainnet (V2)
# ──────────────────────────────────────────────────────────────────────

POLYGON_RPC = os.environ.get("POLYGON_RPC_URL", "https://polygon.drpc.org")
CHAIN_ID = 137

# USDC.e (bridged) — CollateralOnramp wrap input
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
# Native USDC on Polygon — CollateralOnramp wrap input
USDC_NATIVE = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")

WRAP_ASSETS_ORDER: tuple[str, ...] = (USDC_NATIVE, USDC_E)

ASSET_LABELS: dict[str, str] = {
    USDC_NATIVE: "USDC (native Polygon)",
    USDC_E: "USDC.e (bridged)",
}

COLLATERAL_ONRAMP = Web3.to_checksum_address(
    "0x93070a847efEf7F70739046A929D47a521F5B8ee"
)

# Fallback if py_clob_client_v2 is unavailable (matches docs + SDK 1.0.0)
_FALLBACK_SPENDERS: dict[str, str] = {
    "CTF Exchange": "0xE111180000d2663C0091e4f400237545B87B996B",
    "Neg Risk CTF Exchange": "0xe2222d279d744050d28e00520010520000310F59",
    "Neg Risk Adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}
_FALLBACK_PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
_FALLBACK_CONDITIONAL = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"


def _load_v2_addresses(chain_id: int) -> tuple[dict[str, str], str, str]:
    """Spenders keyed by label, pUSD address, Conditional Tokens address."""
    try:
        from py_clob_client_v2.config import get_contract_config

        cc = get_contract_config(chain_id)
        spenders = {
            "CTF Exchange": Web3.to_checksum_address(cc.exchange_v2),
            "Neg Risk CTF Exchange": Web3.to_checksum_address(cc.neg_risk_exchange_v2),
            "Neg Risk Adapter": Web3.to_checksum_address(cc.neg_risk_adapter),
        }
        pusd = Web3.to_checksum_address(cc.collateral)
        ct = Web3.to_checksum_address(cc.conditional_tokens)
        return spenders, pusd, ct
    except Exception:
        spenders = {
            k: Web3.to_checksum_address(v) for k, v in _FALLBACK_SPENDERS.items()
        }
        return (
            spenders,
            Web3.to_checksum_address(_FALLBACK_PUSD),
            Web3.to_checksum_address(_FALLBACK_CONDITIONAL),
        )


SPENDERS, PUSD, CONDITIONAL_TOKENS = _load_v2_addresses(CHAIN_ID)

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

ONRAMP_WRAP_ABI = [
    {
        "inputs": [
            {"name": "_asset", "type": "address"},
            {"name": "_to", "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "name": "wrap",
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


def read_erc20_balance(w3: Web3, token_addr: str, owner: str) -> tuple[int, int]:
    owner = Web3.to_checksum_address(owner)
    token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    try:
        raw = token.functions.balanceOf(owner).call()
        dec = token.functions.decimals().call()
        return raw, dec
    except Exception:
        return 0, 6


def pusd_balance(w3: Web3, owner: str) -> tuple[int, int]:
    return read_erc20_balance(w3, PUSD, owner)


def print_wrap_balances_header(w3: Web3, owner: str) -> None:
    """Step (1) for interactive mode — USDC native, USDC.e, pUSD."""
    owner = Web3.to_checksum_address(owner)
    nat_raw, nat_dec = read_erc20_balance(w3, USDC_NATIVE, owner)
    e_raw, e_dec = read_erc20_balance(w3, USDC_E, owner)
    p_raw, p_dec = pusd_balance(w3, owner)
    print("\n  Balances (wrap)")
    print(f"    USDC natif:     {nat_raw / 10**nat_dec:,.6f}")
    print(f"    USDC.e:         {e_raw / 10**e_dec:,.6f}")
    print(f"    pUSD actuel:    {p_raw / 10**p_dec:,.6f}")
    print()


def parse_human_amount(amount_str: str, decimals: int, max_raw: int) -> int | None:
    try:
        d = Decimal(amount_str.strip())
    except InvalidOperation:
        return None
    if d <= 0:
        return None
    scale = Decimal(10) ** decimals
    raw = int((d * scale).to_integral_value(rounding=ROUND_DOWN))
    if raw <= 0 or raw > max_raw:
        return None
    return raw


def check_all(w3: Web3, owner: str) -> list[dict]:
    """Return a list of dicts describing each token/spender pair and its status."""
    owner = Web3.to_checksum_address(owner)
    results = []

    collateral = w3.eth.contract(address=PUSD, abi=ERC20_ABI)
    ct = w3.eth.contract(address=CONDITIONAL_TOKENS, abi=ERC1155_ABI)

    for spender_label, spender_addr in SPENDERS.items():
        try:
            allowance = collateral.functions.allowance(owner, spender_addr).call()
        except Exception:
            allowance = 0
        results.append({
            "token": "pUSD",
            "token_addr": PUSD,
            "spender": spender_label,
            "spender_addr": spender_addr,
            "is_erc1155": False,
            "value": allowance,
            "ok": allowance >= MIN_ALLOWANCE,
        })

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

    nat_raw, nat_dec = read_erc20_balance(w3, USDC_NATIVE, owner)
    e_raw, e_dec = read_erc20_balance(w3, USDC_E, owner)
    pusd_raw, pusd_dec = pusd_balance(w3, owner)

    matic_balance = w3.eth.get_balance(owner)

    print(f"\n  Wallet:          {owner}")
    print(f"  MATIC balance:    {Web3.from_wei(matic_balance, 'ether'):.4f} MATIC")
    print(f"  USDC natif:       {nat_raw / 10**nat_dec:,.6f}  (wrap → pUSD)")
    print(
        f"  USDC.e:           {e_raw / 10**e_dec:,.6f}  (wrap → pUSD)"
    )
    print(f"  pUSD balance:     {pusd_raw / 10**pusd_dec:,.6f} (CLOB collateral)")
    print()

    checks = check_all(w3, owner)

    print(f"  {'Token':<22} {'Spender':<26} {'Allowance':<20} {'Status'}")
    print(f"  {'─' * 22} {'─' * 26} {'─' * 20} {'─' * 10}")

    for c in checks:
        formatted = _format_allowance(c["value"], c["is_erc1155"])
        status = "OK" if c["ok"] else "NEEDS APPROVAL"
        icon = "  " if c["ok"] else "!! "
        print(
            f"{icon}{c['token']:<22} {c['spender']:<26} {formatted:<20} {status}"
        )

    print()

    try:
        nat_ct = w3.eth.contract(address=USDC_NATIVE, abi=ERC20_ABI)
        e_ct = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
        onramp_allow_nat = nat_ct.functions.allowance(owner, COLLATERAL_ONRAMP).call()
        onramp_allow_e = e_ct.functions.allowance(owner, COLLATERAL_ONRAMP).call()
        print(
            f"  USDC natif → OnRamp: {_format_allowance(onramp_allow_nat)} "
            f"(spender {COLLATERAL_ONRAMP})"
        )
        print(
            f"  USDC.e → OnRamp:     {_format_allowance(onramp_allow_e)} "
            f"(spender {COLLATERAL_ONRAMP})"
        )
        print()

    except Exception as exc:
        print(f"  (Could not read OnRamp allowances: {exc})\n")

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
        "chainId": CHAIN_ID,
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
        "chainId": CHAIN_ID,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def send_onramp_wrap(
    w3: Web3,
    account,
    asset_addr: str,
    amount: int,
    recipient: str,
    nonce: int,
) -> tuple[str | None, str]:
    """
    Approve OnRamp for exact `amount` of `asset_addr` if needed, then wrap.
    Returns (approve_tx_hex_or_none, wrap_tx_hex).
    """
    asset_addr = Web3.to_checksum_address(asset_addr)
    recipient = Web3.to_checksum_address(recipient)
    token = w3.eth.contract(address=asset_addr, abi=ERC20_ABI)
    onramp = w3.eth.contract(address=COLLATERAL_ONRAMP, abi=ONRAMP_WRAP_ABI)
    label = ASSET_LABELS.get(asset_addr, asset_addr)

    approve_hex: str | None = None
    current = token.functions.allowance(account.address, COLLATERAL_ONRAMP).call()
    if current < amount:
        print(
            f"  Approving CollateralOnramp for {amount} {label} base units "
            f"(nonce {nonce})..."
        )
        approve_hex = send_erc20_approve(
            w3, account, asset_addr, COLLATERAL_ONRAMP, amount, nonce,
        )
        print(f"    approve tx: {approve_hex}")
        print(f"    https://polygonscan.com/tx/{approve_hex}")
        receipt = w3.eth.wait_for_transaction_receipt(approve_hex, timeout=180)
        if receipt["status"] != 1:
            raise RuntimeError(f"{label}: approve reverted")
        nonce += 1
        print(f"    Confirmed in block {receipt['blockNumber']}")

    print(
        f"  Calling CollateralOnramp.wrap({label} → pUSD, to={recipient}, amount={amount}) "
        f"(nonce {nonce})..."
    )
    tx = onramp.functions.wrap(asset_addr, recipient, amount).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 300_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": CHAIN_ID,
    })
    signed = account.sign_transaction(tx)
    raw = w3.eth.send_raw_transaction(signed.raw_transaction)
    wrap_hex = raw.hex()
    print(f"    wrap tx: {wrap_hex}")
    print(f"    https://polygonscan.com/tx/{wrap_hex}")
    receipt = w3.eth.wait_for_transaction_receipt(wrap_hex, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"{label}: wrap() reverted")
    print(f"    Confirmed in block {receipt['blockNumber']}")
    return approve_hex, wrap_hex


def log_pusd_after(w3: Web3, owner: str) -> None:
    owner = Web3.to_checksum_address(owner)
    raw, dec = pusd_balance(w3, owner)
    print(f"  pUSD balance now: {raw / 10**dec:,.6f}\n")


def wrap_assets_sequence(
    w3: Web3,
    account,
    owner: str,
    assets_and_amounts: list[tuple[str, int]],
) -> None:
    """Approve+wrap each (asset, amount) in order; refetch nonce between steps."""
    owner = Web3.to_checksum_address(owner)
    nonce = w3.eth.get_transaction_count(account.address)
    for asset_addr, amount in assets_and_amounts:
        if amount <= 0:
            continue
        label = ASSET_LABELS.get(asset_addr, asset_addr)
        print("\n" + "-" * 72)
        print(f"  Wrap {label}")
        print("-" * 72)
        try:
            send_onramp_wrap(w3, account, asset_addr, amount, owner, nonce)
        except Exception as exc:
            print(f"  Wrap FAILED ({label}): {exc}")
            return
        nonce = w3.eth.get_transaction_count(account.address)
        log_pusd_after(w3, owner)


def maybe_wrap_all_collateral(w3: Web3, account, owner: str) -> None:
    """Non-interactive: wrap full balance of each supported asset (native, then USDC.e)."""
    owner = Web3.to_checksum_address(owner)
    to_wrap: list[tuple[str, int]] = []
    for asset in WRAP_ASSETS_ORDER:
        raw, _dec = read_erc20_balance(w3, asset, owner)
        if raw > 0:
            to_wrap.append((asset, raw))
    if not to_wrap:
        print("  Wrap skipped: no USDC native or USDC.e balance on wallet.")
        return
    wrap_assets_sequence(w3, account, owner, to_wrap)


def run_wrap_interactive(w3: Web3, account, owner: str) -> None:
    owner = Web3.to_checksum_address(owner)
    while True:
        print_wrap_balances_header(w3, owner)
        print("  What do you want to convert?")
        print("    [1] Convert all (USDC natif + USDC.e → pUSD, each if balance > 0)")
        print("    [2] Convert only USDC natif → pUSD")
        print("    [3] Convert only USDC.e → pUSD")
        print("    [4] Custom amount (pick token + amount)")
        print("    [0] Cancel")
        choice = input("  Enter choice [0-4]: ").strip()

        if choice == "0":
            print("  Cancelled.\n")
            return
        if choice == "1":
            pairs: list[tuple[str, int]] = []
            for asset in WRAP_ASSETS_ORDER:
                raw, _d = read_erc20_balance(w3, asset, owner)
                if raw > 0:
                    pairs.append((asset, raw))
            if not pairs:
                print("  Nothing to wrap (both balances are 0).\n")
                continue
            wrap_assets_sequence(w3, account, owner, pairs)
            continue
        if choice == "2":
            raw, _d = read_erc20_balance(w3, USDC_NATIVE, owner)
            if raw == 0:
                print("  USDC natif balance is 0.\n")
                continue
            wrap_assets_sequence(w3, account, owner, [(USDC_NATIVE, raw)])
            continue
        if choice == "3":
            raw, _d = read_erc20_balance(w3, USDC_E, owner)
            if raw == 0:
                print("  USDC.e balance is 0.\n")
                continue
            wrap_assets_sequence(w3, account, owner, [(USDC_E, raw)])
            continue
        if choice == "4":
            tok = input("  Token: [1] USDC natif  [2] USDC.e : ").strip()
            if tok == "1":
                asset = USDC_NATIVE
            elif tok == "2":
                asset = USDC_E
            else:
                print("  Invalid choice.\n")
                continue
            raw_max, dec = read_erc20_balance(w3, asset, owner)
            if raw_max == 0:
                print("  Balance is 0 for that token.\n")
                continue
            amt_s = input(
                f"  Amount to wrap (max {raw_max / 10**dec:,.6f}): "
            ).strip()
            parsed = parse_human_amount(amt_s, dec, raw_max)
            if parsed is None:
                print("  Invalid amount or exceeds balance.\n")
                continue
            wrap_assets_sequence(w3, account, owner, [(asset, parsed)])
            continue

        print("  Invalid choice.\n")


def sync_clob_allowances(private_key: str, funder: str, sig_type: int) -> None:
    """Call the CLOB API update_balance_allowance to sync server state (V2 SDK)."""
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        kwargs = {
            "host": "https://clob.polymarket.com",
            "key": private_key,
            "chain_id": CHAIN_ID,
            "signature_type": sig_type,
        }
        if funder:
            kwargs["funder"] = funder

        client = ClobClient(**kwargs)
        client.set_api_creds(client.create_or_derive_api_key())

        for asset in [AssetType.COLLATERAL, AssetType.CONDITIONAL]:
            resp = client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=asset)
            )
            print(f"  CLOB sync ({asset}): {resp}")

    except Exception as exc:
        print(f"  CLOB sync failed (non-critical): {exc}")
        print("  The CLOB will pick up on-chain allowances automatically within minutes.")


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up Polymarket CLOB V2 token allowances")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--wrap-only",
        action="store_const",
        const="wrap",
        dest="mode",
        help="Auto-wrap all USDC native + USDC.e balance (no allowances)",
    )
    mode_group.add_argument(
        "--wrap-interactive",
        "--wrap",
        action="store_const",
        const="wrap_interactive",
        dest="mode",
        help="Interactive wrap (default when script is run with no arguments)",
    )
    mode_group.add_argument(
        "--approve-only",
        action="store_const",
        const="approve",
        dest="mode",
        help="Only check/set ERC20 + ERC1155 allowances (no wrap)",
    )
    mode_group.add_argument(
        "--all",
        action="store_const",
        const="all",
        dest="mode",
        help="Auto-wrap both USDC sources then allowances",
    )
    parser.set_defaults(mode="all")

    parser.add_argument("--check-only", action="store_true", help="Only check, don't send transactions")
    parser.add_argument("--revoke", action="store_true", help="Revoke all approvals (set ERC20 to 0 / ERC1155 false)")
    parser.add_argument("--rpc", default=POLYGON_RPC, help="Polygon RPC URL")
    args = parser.parse_args()

    # No CLI args except script name → interactive wrap only
    if len(sys.argv) == 1:
        args.mode = "wrap_interactive"

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
    print("  Polymarket CLOB V2 — collateral wrap & token allowances")
    print("=" * 72)
    print(f"  Mode: {args.mode}")
    print(f"  Revoke: {args.revoke} | Check-only: {args.check_only}")

    if args.check_only and args.mode == "wrap":
        print_status(w3, Web3.to_checksum_address(owner))
        print("  (--check-only) Not sending wrap transactions.")
        return

    if args.check_only and args.mode == "wrap_interactive":
        print_status(w3, Web3.to_checksum_address(owner))
        print("  (--check-only) Not sending wrap transactions.")
        return

    checks = print_status(w3, Web3.to_checksum_address(owner))

    if args.mode == "wrap_interactive":
        if args.check_only:
            return
        if args.revoke:
            print("  NOTE: --revoke applies to allowances only; no wrap with --revoke.")
            return
        run_wrap_interactive(w3, account, owner)
        print()
        print_status(w3, Web3.to_checksum_address(owner))
        return

    if args.mode == "wrap":
        if args.check_only:
            return
        if args.revoke:
            print("  NOTE: --revoke applies to allowances only; no wrap with --revoke.")
            return
        maybe_wrap_all_collateral(w3, account, owner)
        print()
        print_status(w3, Web3.to_checksum_address(owner))
        return

    if args.check_only:
        needs = sum(1 for c in checks if not c["ok"])
        if needs == 0:
            print("  All allowances are set.")
        else:
            print(f"  {needs} allowance(s) need approval. Run without --check-only to fix.")
        return

    if args.mode == "all" and not args.revoke:
        maybe_wrap_all_collateral(w3, account, owner)

    to_fix = []
    for c in checks:
        if args.revoke and c["ok"]:
            to_fix.append(c)
        elif not args.revoke and not c["ok"]:
            to_fix.append(c)

    if not to_fix:
        action_word = "revoke" if args.revoke else "approve"
        print(f"  Nothing to {action_word}.")
        if not args.revoke:
            sync_clob_allowances(private_key, funder, sig_type)
        print()
        print_status(w3, Web3.to_checksum_address(owner))
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
                print("    REVERTED — check on Polygonscan")
                nonce += 1

            time.sleep(0.5)

        except Exception as exc:
            print(f"FAILED: {exc}")
            nonce = w3.eth.get_transaction_count(account.address)

    if txs_sent > 0:
        print(f"\n  {txs_sent} transaction(s) confirmed. Syncing with CLOB API...")
        sync_clob_allowances(private_key, funder, sig_type)

    print()
    final_checks = print_status(w3, Web3.to_checksum_address(owner))

    all_ok = all(c["ok"] for c in final_checks)
    if all_ok and not args.revoke:
        print("  All allowances set. You're ready to trade on CLOB V2.")
    elif args.revoke:
        print("  Revoke transactions processed (verify table above).")

    print()


if __name__ == "__main__":
    main()
