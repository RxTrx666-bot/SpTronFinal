"""In-memory TRON chain implementing :class:`app.services.tron_service.TronDataSource`.

It reproduces the behaviour the application depends on: block numbers and
timestamps, a solidified head lagging the latest block, unconfirmed vs
confirmed data, TronGrid-style history pagination (fingerprint), missing block
numbers in the history endpoint, transaction signers, and injectable API
outages.
"""

from __future__ import annotations

import hashlib
import itertools
import time
from dataclasses import replace

from app.config import OFFICIAL_USDT_TRC20
from app.domain import AccountInfo, AddressLabel, BlockData, TokenTransfer, TxDetails, assign_sequences
from app.services.tron_service import TronApiError

USDT = OFFICIAL_USDT_TRC20


class SimulatedChain:
    def __init__(self, *, start_block: int = 66_000_000, start_ts_ms: int | None = None, solid_lag: int = 19) -> None:
        self.head = start_block
        self.head_ts = start_ts_ms if start_ts_ms is not None else int(time.time() * 1000) - 400 * 86_400_000
        self._base = (start_block, self.head_ts)  # block numbers follow time: one block per 3 s
        self.solid_lag = solid_lag
        self.blocks: dict[int, BlockData] = {}
        self.transfers: list[TokenTransfer] = []
        self.pending: list[TokenTransfer] = []
        self.tx_index: dict[str, TokenTransfer] = {}
        self.first_seen: dict[str, int] = {}
        self.labels: dict[str, AddressLabel] = {}
        self._seq = itertools.count(1)
        self.outage = False
        self.fail_next = 0
        self.requests = 0
        self.failures = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------------ building
    def _hash(self) -> str:
        return hashlib.sha256(f"simtx:{next(self._seq)}".encode()).hexdigest()

    def transfer(self, frm: str, to: str, amount: int, *, token: str = USDT, initiator: str | None = None, tx_hash: str | None = None) -> str:
        h = tx_hash or self._hash()
        self.pending.append(
            TokenTransfer(tx_hash=h, token_contract=token, from_address=frm, to_address=to, amount=amount, block_timestamp_ms=0, initiator=initiator or frm)
        )
        return h

    def mine(self, ts_ms: int | None = None) -> BlockData:
        ts = ts_ms if ts_ms is not None else self.head_ts + 3000
        if ts <= self.head_ts:
            ts = self.head_ts + 3000
        self.head = max(self.head + 1, self._base[0] + (ts - self._base[1]) // 3000)
        self.head_ts = ts
        txs = [replace(t, block_number=self.head, block_timestamp_ms=ts) for t in self.pending]
        txs = assign_sequences(txs)
        self.pending = []
        for i, t in enumerate(txs):
            t = replace(t, log_index=0)
            txs[i] = t
            self.transfers.append(t)
            self.tx_index.setdefault(t.tx_hash, t)
            for a in (t.from_address, t.to_address):
                self.first_seen.setdefault(a, ts)
        blk = BlockData(number=self.head, timestamp_ms=ts, transfers=txs)
        self.blocks[self.head] = blk
        return blk

    def send(self, frm: str, to: str, amount: int, *, ts_ms: int | None = None, token: str = USDT, initiator: str | None = None) -> str:
        """transfer + mine in one step."""
        h = self.transfer(frm, to, amount, token=token, initiator=initiator)
        self.mine(ts_ms)
        return h

    def label(self, address: str, label: str, category: str = "exchange") -> None:
        self.labels[address] = AddressLabel(address=address, label=label, category=category, source="simulation public tag")

    @property
    def solid(self) -> int:
        """Solidified head: ``solid_lag`` blocks behind the newest block, where the chain is
        considered live at wall-clock time (blocks older than ~57 s are irreversible)."""
        by_time = self._base[0] + (int(time.time() * 1000) - self._base[1]) // 3000 - self.solid_lag
        return min(self.head, max(self.head - self.solid_lag, by_time))

    def _call(self) -> None:
        self.requests += 1
        if self.outage or self.fail_next > 0:
            if self.fail_next > 0:
                self.fail_next -= 1
            self.failures += 1
            self.last_error = "simulated API outage"
            raise TronApiError("simulated API outage", retryable=True, status=503)

    # ------------------------------------------------------------------ TronDataSource
    async def get_now_block_number(self) -> int:
        self._call()
        return self.head

    async def get_solid_block_number(self) -> int:
        self._call()
        return self.solid

    async def get_block(self, number: int, contracts: dict[str, int]) -> BlockData:
        self._call()
        blk = self.blocks.get(number)
        if blk is None:
            if number > self.head:
                raise TronApiError(f"block {number} not available yet", retryable=True)
            return BlockData(number=number, timestamp_ms=self.head_ts, transfers=[])
        return BlockData(number=number, timestamp_ms=blk.timestamp_ms, transfers=[t for t in blk.transfers if t.token_contract in contracts])

    async def get_trc20_transfers(
        self,
        address: str,
        contract: str,
        *,
        min_timestamp_ms: int | None = None,
        max_timestamp_ms: int | None = None,
        only_confirmed: bool = False,
        order: str = "asc",
        limit: int = 200,
        fingerprint: str | None = None,
        priority: int = 0,
    ) -> tuple[list[TokenTransfer], str | None]:
        self._call()
        rows = [
            t for t in self.transfers
            if t.token_contract == contract
            and address in (t.from_address, t.to_address)
            and (min_timestamp_ms is None or t.block_timestamp_ms >= min_timestamp_ms)
            and (max_timestamp_ms is None or t.block_timestamp_ms <= max_timestamp_ms)
            and (not only_confirmed or (t.block_number or 0) <= self.solid)
        ]  # fmt: skip
        rows.sort(key=lambda t: (t.block_timestamp_ms, t.tx_hash), reverse=order == "desc")
        offset = int(fingerprint or 0)
        page = rows[offset : offset + limit]
        nxt = str(offset + limit) if offset + limit < len(rows) else None
        # The history endpoint carries neither block number nor signer.
        out = [replace(t, block_number=None, initiator=None, log_index=None, confirmed=(t.block_number or 0) <= self.solid) for t in page]
        return out, nxt

    async def get_transaction(self, tx_hash: str, priority: int = 0) -> TxDetails:
        self._call()
        t = self.tx_index.get(tx_hash.lower())
        if t is None:
            return TxDetails(tx_hash=tx_hash, found=False)
        return TxDetails(
            tx_hash=tx_hash,
            found=True,
            block_number=t.block_number,
            block_timestamp_ms=t.block_timestamp_ms,
            success=True,
            initiator=t.initiator,
            confirmed=(t.block_number or 0) <= self.solid,
        )

    async def get_account(self, address: str, priority: int = 0) -> AccountInfo:
        self._call()
        ts = self.first_seen.get(address)
        return AccountInfo(address=address, exists=ts is not None, create_time_ms=ts)

    async def get_label(self, address: str) -> AddressLabel | None:
        return self.labels.get(address)

    async def close(self) -> None:
        pass
