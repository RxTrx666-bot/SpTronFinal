"""Address similarity engine.

Address poisoning works because wallets and explorers abbreviate addresses
(``TLegit…Wr2c``): attackers generate a vanity address whose *beginning and
end* match an address the victim already uses, then hope the victim copies the
wrong one from their history.  The engine therefore measures several things
and never relies on a single metric:

* ``prefix_match_length`` / ``suffix_match_length`` - exact matching characters
  at the start / end.  The leading ``T`` is identical for every TRON mainnet
  address and is therefore **excluded**: ``TLegit…`` vs ``TLegiX…`` has a
  prefix match of 4 (``Legi``).
* fuzzy edge lengths - same, but treating case differences and a few visually
  confusable Base58 characters (``1/i``, ``5/s``, ``2/z``, ``8/B``, ``9/g``) as
  half-matches (``CONFUSABLE_MATCHING``).
* ``prefix_similarity`` / ``suffix_similarity`` - effective edge match divided
  by the visible window (``SIMILARITY_PREFIX_WINDOW`` / ``..._SUFFIX_WINDOW``).
* ``overall_similarity`` - normalised Levenshtein similarity of the 33-char body.
* ``positional_similarity`` - share of identical characters at the same position.
* ``coincidence_log10`` - log10 of the probability that a *random* address
  shares this many edge characters (58^-n).  Matching 4+4 characters by chance
  is ~1 in 10^14, which is why edge matches are strong evidence - but the
  engine still requires both edges (or one very long edge) *and* a weighted
  ``similarity_score`` above ``MIN_SIMILARITY_SCORE``.

Inputs are normalised with :func:`app.utils.address.normalize_address`, so hex
and Base58 forms of the same account compare as identical and malformed
strings never match.  Stored addresses are never modified.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from app.utils.address import InvalidAddress, base58_to_hex, normalize_address

_LOG10_58 = math.log10(58)

# Visually confusable Base58 characters (after case folding).
_CONFUSABLE = {"1": "i", "5": "s", "2": "z", "8": "b", "9": "g"}


def fold(ch: str) -> str:
    c = ch.lower()
    return _CONFUSABLE.get(c, c)


def body(address: str) -> str:
    """The 33 characters after the mandatory leading ``T``."""
    return address[1:]


def candidate_keys(address: str, length: int = 3) -> tuple[str, str]:
    """Folded prefix/suffix keys used to pre-filter look-alike candidates via DB indexes."""
    b = body(address)
    return "".join(fold(c) for c in b[:length]), "".join(fold(c) for c in b[-length:])


@dataclass(frozen=True)
class SimilarityConfig:
    min_prefix_match: int = 4
    min_suffix_match: int = 4
    single_edge_min_match: int = 7
    min_similarity_score: float = 0.60
    very_high_similarity: float = 0.85
    prefix_window: int = 5
    suffix_window: int = 5
    weight_prefix: float = 0.40
    weight_suffix: float = 0.40
    weight_overall: float = 0.10
    weight_positional: float = 0.10
    confusable_matching: bool = True

    @classmethod
    def from_settings(cls, s) -> SimilarityConfig:
        return cls(
            min_prefix_match=s.min_prefix_match,
            min_suffix_match=s.min_suffix_match,
            single_edge_min_match=s.single_edge_min_match,
            min_similarity_score=s.min_similarity_score,
            very_high_similarity=s.very_high_similarity,
            prefix_window=s.similarity_prefix_window,
            suffix_window=s.similarity_suffix_window,
            weight_prefix=s.similarity_weight_prefix,
            weight_suffix=s.similarity_weight_suffix,
            weight_overall=s.similarity_weight_overall,
            weight_positional=s.similarity_weight_positional,
            confusable_matching=s.confusable_matching,
        )


@dataclass(frozen=True)
class SimilarityResult:
    legitimate: str
    candidate: str
    identical: bool
    prefix_match_length: int
    suffix_match_length: int
    fuzzy_prefix_match_length: int
    fuzzy_suffix_match_length: int
    effective_prefix: float
    effective_suffix: float
    prefix_similarity: float
    suffix_similarity: float
    overall_similarity: float
    positional_similarity: float
    hex_prefix_match: int
    coincidence_log10: float
    similarity_score: float
    edge_rule: str  # both_edges / single_edge / none
    is_match: bool
    very_high: bool

    @property
    def similarity_pct(self) -> int:
        return int(round(self.similarity_score * 100))

    def as_dict(self) -> dict:
        d = asdict(self)
        d["similarity_pct"] = self.similarity_pct
        return d

    def describe(self) -> str:
        return f"prefix {self.prefix_match_length} chars, suffix {self.suffix_match_length} chars (after leading T), score {self.similarity_pct}%"


def _common_prefix(a: str, b: str, eq) -> int:
    n = 0
    for x, y in zip(a, b):
        if not eq(x, y):
            break
        n += 1
    return n


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class SimilarityEngine:
    def __init__(self, config: SimilarityConfig | None = None) -> None:
        self.cfg = config or SimilarityConfig()

    def compare(self, legitimate: str, candidate: str) -> SimilarityResult:
        """Compare two addresses (Base58 or hex).  Raises InvalidAddress for invalid input."""
        legit = normalize_address(legitimate)
        cand = normalize_address(candidate)
        a, b = body(legit), body(cand)
        identical = legit == cand
        n = len(a)

        exact_eq = lambda x, y: x == y  # noqa: E731
        fuzzy_eq = lambda x, y: fold(x) == fold(y)  # noqa: E731

        pm = _common_prefix(a, b, exact_eq)
        sm = _common_prefix(a[::-1], b[::-1], exact_eq)
        if pm + sm > n:  # identical addresses
            sm = n - pm
        if self.cfg.confusable_matching:
            fpm = _common_prefix(a, b, fuzzy_eq)
            fsm = _common_prefix(a[::-1], b[::-1], fuzzy_eq)
            if fpm + fsm > n:
                fsm = n - fpm
        else:
            fpm, fsm = pm, sm
        eff_p = pm + 0.5 * (fpm - pm)
        eff_s = sm + 0.5 * (fsm - sm)

        prefix_sim = min(1.0, eff_p / self.cfg.prefix_window) if self.cfg.prefix_window else 0.0
        suffix_sim = min(1.0, eff_s / self.cfg.suffix_window) if self.cfg.suffix_window else 0.0
        overall = 1.0 - levenshtein(a, b) / n
        positional = sum(1 for x, y in zip(a, b) if x == y) / n

        try:
            ha, hb = base58_to_hex(legit)[2:], base58_to_hex(cand)[2:]
            hex_pm = _common_prefix(ha, hb, exact_eq)
        except InvalidAddress:  # pragma: no cover - already validated
            hex_pm = 0

        wsum = self.cfg.weight_prefix + self.cfg.weight_suffix + self.cfg.weight_overall + self.cfg.weight_positional
        score = (
            self.cfg.weight_prefix * prefix_sim
            + self.cfg.weight_suffix * suffix_sim
            + self.cfg.weight_overall * overall
            + self.cfg.weight_positional * positional
        ) / (wsum or 1.0)
        score = round(min(max(score, 0.0), 1.0), 4)

        p_floor, s_floor = int(eff_p), int(eff_s)
        if p_floor >= self.cfg.min_prefix_match and s_floor >= self.cfg.min_suffix_match:
            rule = "both_edges"
        elif max(pm, sm) >= self.cfg.single_edge_min_match:
            rule = "single_edge"
        else:
            rule = "none"
        is_match = (not identical) and rule != "none" and score >= self.cfg.min_similarity_score

        return SimilarityResult(
            legitimate=legit,
            candidate=cand,
            identical=identical,
            prefix_match_length=pm,
            suffix_match_length=sm,
            fuzzy_prefix_match_length=fpm,
            fuzzy_suffix_match_length=fsm,
            effective_prefix=eff_p,
            effective_suffix=eff_s,
            prefix_similarity=round(prefix_sim, 4),
            suffix_similarity=round(suffix_sim, 4),
            overall_similarity=round(overall, 4),
            positional_similarity=round(positional, 4),
            hex_prefix_match=hex_pm,
            coincidence_log10=round(-(pm + sm) * _LOG10_58, 2),
            similarity_score=score,
            edge_rule=rule,
            is_match=is_match,
            very_high=is_match and score >= self.cfg.very_high_similarity,
        )

    def best_matches(self, candidate: str, legitimate: list[str], limit: int = 5) -> list[SimilarityResult]:
        """Return matching legitimate addresses ordered by similarity (best first)."""
        out = []
        for legit in legitimate:
            try:
                r = self.compare(legit, candidate)
            except InvalidAddress:
                continue
            if r.is_match:
                out.append(r)
        out.sort(key=lambda r: (r.similarity_score, r.prefix_match_length + r.suffix_match_length), reverse=True)
        return out[:limit]
