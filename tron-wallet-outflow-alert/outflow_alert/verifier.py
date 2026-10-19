"""On-chain verification of candidate transactions and the startup token check."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from outflow_alert.abi import AbiError, decode_string, decode_transfer_log, decode_uint256
from outflow_alert.amounts import match_target
from outflow_alert.storage import MatchedTransfer
from outflow_alert.tron_client import TronClient


class TokenCheckError(RuntimeError):
    pass


async def verify_token(client: TronClient, contract: str, owner: str, *, symbol: str, decimals: int) -> None:
    """Fail fast unless the contract reports the expected ``symbol()`` and ``decimals()``."""
    try:
        got_symbol = decode_string(await client.call_constant(contract, "symbol()", owner))
        got_decimals = decode_uint256(await client.call_constant(contract, "decimals()", owner))
    except AbiError as exc:
        raise TokenCheckError(f"cannot decode token metadata of {contract}: {exc}") from exc
    if got_symbol != symbol:
        raise TokenCheckError(f"{contract}: symbol()={got_symbol!r}, expected {symbol!r}")
    if got_decimals != decimals:
        raise TokenCheckError(f"{contract}: decimals()={got_decimals}, expected {decimals}")


class Verdict(enum.Enum):
    MATCH = "alerted"
    REJECTED = "rejected"
    NOT_FOUND = "not_found"  # not yet in a (solidified) block - try again later


@dataclass
class VerificationResult:
    verdict: Verdict
    reason: str = ""
    matches: list[MatchedTransfer] = field(default_factory=list)


def evaluate_transaction_info(
    txid: str,
    info: dict,
    *,
    wallet: str,
    contract: str,
    targets: tuple[int, ...],
    tolerance_raw: int,
) -> VerificationResult:
    """Pure decision function over a ``gettransactioninfobyid`` response."""
    if not info:
        return VerificationResult(Verdict.NOT_FOUND, "transaction info not available yet")
    if str(info.get("id", txid)).lower() != txid.lower():
        return VerificationResult(Verdict.REJECTED, "transaction id mismatch")
    receipt = info.get("receipt") or {}
    receipt_result = receipt.get("result")
    if receipt_result != "SUCCESS":
        return VerificationResult(Verdict.REJECTED, f"receipt result is {receipt_result!r}, not SUCCESS")
    if info.get("result") == "FAILED":
        return VerificationResult(Verdict.REJECTED, "transaction result FAILED")

    matches: list[MatchedTransfer] = []
    saw_outgoing = False
    for index, raw_log in enumerate(info.get("log") or []):
        decoded = decode_transfer_log(raw_log, index)
        if decoded is None or decoded.contract != contract:
            continue  # not a Transfer event, or emitted by some other contract
        if decoded.from_address != wallet:
            continue  # incoming or unrelated transfer
        saw_outgoing = True
        target = match_target(decoded.amount_raw, targets, tolerance_raw)
        if target is None:
            continue
        matches.append(
            MatchedTransfer(
                txid=txid,
                log_index=index,
                from_address=decoded.from_address,
                to_address=decoded.to_address,
                amount_raw=decoded.amount_raw,
                target_raw=target,
                block_number=info.get("blockNumber"),
                block_ts=info.get("blockTimeStamp"),
            )
        )
    if matches:
        return VerificationResult(Verdict.MATCH, "", matches)
    if saw_outgoing:
        return VerificationResult(Verdict.REJECTED, "outgoing USDT Transfer log amount outside filter")
    return VerificationResult(Verdict.REJECTED, "no USDT Transfer log from the wallet")
