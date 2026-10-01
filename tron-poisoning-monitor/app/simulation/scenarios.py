"""Realistic address-poisoning scenarios for the simulated chain."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.simulation import addresses as A
from app.simulation.chain import SimulatedChain

USDT = 10**6
DAY = 86_400_000


@dataclass
class Scenario:
    victim: str
    legit: str
    poison: str
    attack_amount: int
    history_payments: int
    dust_tx: str | None = None
    attack_tx: str | None = None
    forward_txs: list[str] = field(default_factory=list)
    hops: list[str] = field(default_factory=list)


def build_history(chain: SimulatedChain, now_ms: int, *, victim: str = A.VICTIM, legit: str = A.LEGIT, payments: int = 12,
                  dust: bool = True, poison: str = A.POISON, days: int = 150) -> Scenario:  # fmt: skip
    """Victim receives funds, pays LEGIT repeatedly, pays a few unrelated recipients;
    the attacker dusts the victim (and other wallets) from the look-alike."""
    start = now_ms - days * DAY
    chain.head_ts = start - 3000
    chain.send(A.FUNDING_SOURCE, victim, 900_000 * USDT, ts_ms=start)
    amounts = [18_500, 22_000, 25_000, 19_750, 30_000, 24_000, 21_300, 27_500, 25_000, 23_800, 26_200, 24_900]
    step = (days - 10) * DAY // max(payments, 1)
    for i in range(payments):
        chain.send(victim, legit, amounts[i % len(amounts)] * USDT, ts_ms=start + DAY + i * step)
        if i % 4 == 1:
            chain.send(victim, A.OTHER_RECIPIENTS[i % len(A.OTHER_RECIPIENTS)], (1_200 + 100 * i) * USDT, ts_ms=start + DAY + i * step + 3_600_000)
    sc = Scenario(victim=victim, legit=legit, poison=poison, attack_amount=25_000 * USDT, history_payments=payments)
    if dust:
        # Typical poisoning campaign: 0.000001 USDT from the look-alike to the victim and other wallets.
        t = now_ms - 5 * DAY
        sc.dust_tx = chain.send(poison, victim, 1, ts_ms=t)
        for i, w in enumerate(A.DUSTED_WALLETS):
            chain.send(poison, w, 1, ts_ms=t + (i + 1) * 60_000)
    return sc


def attack(chain: SimulatedChain, sc: Scenario, ts_ms: int) -> str:
    """The victim copies the poisoned address and pays it."""
    sc.attack_tx = chain.send(sc.victim, sc.poison, sc.attack_amount, ts_ms=ts_ms)
    return sc.attack_tx


def forward(chain: SimulatedChain, sc: Scenario, ts_ms: int) -> None:
    """Attacker moves the funds through intermediate wallets to an exchange deposit address."""
    chain.label(A.EXCHANGE, "Binance-Hot (simulated public tag)", "exchange")
    path = [sc.poison, *A.HOPS, A.EXCHANGE]
    amount = sc.attack_amount
    t = ts_ms
    for a, b in zip(path, path[1:]):
        t += 45_000
        amount -= 10 * USDT  # fees / dust kept back
        sc.forward_txs.append(chain.send(a, b, amount, ts_ms=t))
    sc.hops = path[1:]
