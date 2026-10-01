"""In-memory view of the wallets table (O(1) membership checks for every event)."""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models import Wallet
from app.domain import WALLET_ROOT, dt_to_ms


@dataclass(slots=True)
class WalletInfo:
    address: str
    hop: int
    discovered_at_ms: int
    discovery_tx: str | None
    active: bool = True

    @property
    def is_root(self) -> bool:
        return self.hop == 0


class WalletRegistry:
    """Hop model: root = 0, Wallet A's direct recipients = 1, ...

    * an *expanding* wallet (hop < MAX_HOPS) discovers the recipients of its
      outgoing USDT transfers.  With MAX_HOPS=1 only the root expands.
    * a *monitored* wallet (hop >= 1) is watched for large incoming USDT.
    """

    def __init__(self, root: str, max_hops: int) -> None:
        self.root = root
        self.max_hops = max_hops
        self._w: dict[str, WalletInfo] = {}

    def load(self, rows: list[Wallet]) -> None:
        self._w = {
            r.address: WalletInfo(
                address=r.address,
                hop=0 if r.wallet_type == WALLET_ROOT else r.hop,
                discovered_at_ms=dt_to_ms(r.discovered_at),
                discovery_tx=r.first_seen_tx,
                active=r.active,
            )
            for r in rows
            # a wallet from a previous configuration with a different root is ignored
            if r.root_wallet == self.root
        }
        if self.root not in self._w:
            self._w[self.root] = WalletInfo(self.root, 0, 0, None)

    def get(self, address: str) -> WalletInfo | None:
        return self._w.get(address)

    def add(self, info: WalletInfo) -> None:
        self._w[info.address] = info

    def is_expander(self, w: WalletInfo | None) -> bool:
        return w is not None and w.active and w.hop < self.max_hops

    @staticmethod
    def is_monitored(w: WalletInfo | None) -> bool:
        return w is not None and w.active and w.hop >= 1

    def monitored(self) -> list[WalletInfo]:
        return [w for w in self._w.values() if self.is_monitored(w)]

    def expanders(self) -> list[WalletInfo]:
        return [w for w in self._w.values() if self.is_expander(w)]

    def __len__(self) -> int:
        return len(self._w)
