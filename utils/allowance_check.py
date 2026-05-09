"""
On-chain read of ERC1155 allowance (isApprovedForAll) and balance for debugging ASK rejections.
Uses same contract addresses as scripts/setup_allowances.py.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

# Polygon mainnet — V2 contracts (matches scripts/setup_allowances.py post CLOB V2 cutover Apr 28 2026).
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
SPENDERS = {
    "CTF Exchange V2": "0xE111180000d2663C0091e4f400237545B87B996B",
    "Neg Risk CTF Exchange V2": "0xe2222d279d744050d28e00520010520000310F59",
    "Neg Risk Adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}

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
            {"name": "accounts", "type": "address[]"},
            {"name": "ids", "type": "uint256[]"},
        ],
        "name": "balanceOfBatch",
        "outputs": [{"name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def fetch_conditional_allowance_and_balance(
    owner_address: str,
    token_id: str,
    rpc_url: str | None = None,
) -> Dict[str, Any]:
    """
    Read on-chain: isApprovedForAll(owner, operator) for each spender,
    and balanceOfBatch(owner, [token_id]) for the given token_id.
    Returns a dict with keys: owner, token_id, operators (name -> approved bool), balance.
    """
    try:
        from web3 import Web3
    except ImportError:
        return {"error": "web3 not installed"}

    rpc_url = rpc_url or os.environ.get("POLYGON_RPC_URL", "https://polygon.drpc.org")
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not w3.is_connected():
            return {"error": "RPC not connected", "rpc": rpc_url}
    except Exception as e:
        return {"error": str(e), "rpc": rpc_url}

    owner = Web3.to_checksum_address(owner_address)
    ct = w3.eth.contract(
        address=Web3.to_checksum_address(CONDITIONAL_TOKENS),
        abi=ERC1155_ABI,
    )

    operators = {}
    for name, addr in SPENDERS.items():
        try:
            approved = ct.functions.isApprovedForAll(owner, Web3.to_checksum_address(addr)).call()
            operators[name] = approved
        except Exception as e:
            operators[name] = f"err: {e}"

    balance = None
    try:
        token_id_int = int(token_id)
        balances = ct.functions.balanceOfBatch([owner], [token_id_int]).call()
        balance = int(balances[0]) if balances else 0
    except (ValueError, TypeError, Exception) as e:
        balance = f"err: {e}"

    return {
        "owner": owner,
        "token_id": token_id,
        "operators": operators,
        "balance": balance,
    }
