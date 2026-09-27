"""End-to-end detection tests with a fake TRON API returning realistic payloads."""

from typing import Any

from app.tron_client import TronApiError
from app.tron_monitor import AccountMonitor, BlockMonitor
from tests.helpers import (
    BLOCK_TS, FAKE_USDT, OTHER, THIRD, USDT, WALLET, make_processor, make_settings, run, trongrid_record,
    transfer_log, trx_transfer_info, tx_hash, tx_info,
)


async def _nosleep(_):
    return None


class FakeTron:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.infos: dict[str, dict[str, Any]] = {}
        self.blocks: dict[int, list[dict[str, Any]]] = {}
        self.head = (70_000_000, BLOCK_TS)
        self.calls: list[tuple[str, Any]] = []
        self.fail_next = 0

    def _maybe_fail(self):
        if self.fail_next:
            self.fail_next -= 1
            raise TronApiError("simulated outage")

    async def get_head_block(self, *, solidity=False):
        self._maybe_fail()
        return self.head

    async def get_trc20_transfers(self, address, contract, *, min_timestamp=None, max_timestamp=None,
                                  order="asc", limit=200, only_confirmed=False, fingerprint=None,
                                  only_from=False):
        self._maybe_fail()
        self.calls.append(("trc20", dict(min_timestamp=min_timestamp, max_timestamp=max_timestamp, order=order,
                                         only_from=only_from)))
        recs = [r for r in self.records
                if (not only_from or r["from"] == address)
                and (min_timestamp is None or r["block_timestamp"] >= min_timestamp)
                and (max_timestamp is None or r["block_timestamp"] <= max_timestamp)]
        recs.sort(key=lambda r: r["block_timestamp"], reverse=(order == "desc"))
        return recs[:limit], None

    async def get_transaction_info(self, h, *, solidity=False):
        self.calls.append(("info", h))
        return self.infos.get(h, {})

    async def get_transaction_info_by_block(self, n, *, solidity=False):
        self._maybe_fail()
        return self.blocks.get(n, [])


async def account_monitor(settings=None, clock=None):
    settings = settings or make_settings()
    processor, repo, alerts, stats = await make_processor(settings, clock=clock)
    tron = FakeTron()
    mon = AccountMonitor(settings, tron, repo, processor, stats, sleep=_nosleep)
    return mon, tron, repo, alerts


def test_account_first_start_ignores_history_and_detects_new():
    async def go():
        mon, tron, repo, alerts = await account_monitor()
        # historical match before the monitoring point -> must not alert
        tron.records.append(trongrid_record(1, ts=BLOCK_TS - 60_000))
        await mon.initialize()
        await mon.poll_once()
        assert alerts.queued == []

        # new incoming + outgoing matches, and noise
        tron.records += [
            trongrid_record(2, sender=OTHER, recipient=WALLET, value="1000050", ts=BLOCK_TS + 3000),
            trongrid_record(3, sender=WALLET, recipient=OTHER, value="1000100", ts=BLOCK_TS + 6000),
            trongrid_record(4, value="1000101", ts=BLOCK_TS + 6000),                   # above range
            trongrid_record(5, value="999999", ts=BLOCK_TS + 6000),                    # below range
            trongrid_record(6, contract=FAKE_USDT, ts=BLOCK_TS + 6000),                # fake USDT
            trongrid_record(7, type_="Approval", ts=BLOCK_TS + 6000),                  # not a transfer
        ]
        for n, s, r, v in ((2, OTHER, WALLET, 1_000_050), (3, WALLET, OTHER, 1_000_100)):
            tron.infos[tx_hash(n)] = tx_info(n, [transfer_log(s, r, v)], block=70_000_001 + n,
                                             ts=BLOCK_TS + 3000 * (n - 1))
        await mon.poll_once()
        assert alerts.queued == [tx_hash(2), tx_hash(3)]
        incoming = await repo.get_transaction(tx_hash(2))
        outgoing = await repo.get_transaction(tx_hash(3))
        assert incoming.direction == "INCOMING" and incoming.amount_usdt == "1.000050"
        assert outgoing.direction == "OUTGOING" and outgoing.amount_usdt == "1.000100"
        assert incoming.block_number == 70_000_003 and incoming.source == "event_log"
        assert incoming.block_timestamp_ms == BLOCK_TS + 3000  # exact chain timestamp
        # polling again (overlapping lookback window) never re-alerts
        await mon.poll_once()
        await mon.poll_once()
        assert alerts.queued == [tx_hash(2), tx_hash(3)]
    run(go())


def test_account_verification_rejects_index_event_mismatch():
    async def go():
        mon, tron, repo, alerts = await account_monitor()
        await mon.initialize()
        tron.records.append(trongrid_record(8, ts=BLOCK_TS + 3000))
        # On-chain log says 5 USDT, index said 1.1 -> trust the chain, no alert
        tron.infos[tx_hash(8)] = tx_info(8, [transfer_log(OTHER, WALLET, 5_000_000)])
        await mon.poll_once()
        assert alerts.queued == []
    run(go())


def test_account_verification_pending_then_success():
    async def go():
        mon, tron, repo, alerts = await account_monitor()
        await mon.initialize()
        tron.records.append(trongrid_record(9, ts=BLOCK_TS + 3000))
        await mon.poll_once()  # receipt not yet available
        assert alerts.queued == []
        tron.infos[tx_hash(9)] = tx_info(9, [transfer_log(OTHER, WALLET, 1_000_087)])
        await mon.poll_once()
        assert alerts.queued == [tx_hash(9)]
    run(go())


def test_account_failed_tx_not_alerted():
    async def go():
        mon, tron, repo, alerts = await account_monitor()
        await mon.initialize()
        tron.records.append(trongrid_record(10, ts=BLOCK_TS + 3000))
        tron.infos[tx_hash(10)] = tx_info(10, [transfer_log(OTHER, WALLET, 1_000_087)], result="REVERT")
        await mon.poll_once()
        assert alerts.queued == []
    run(go())


def test_account_malformed_record_does_not_break_poll():
    async def go():
        mon, tron, repo, alerts = await account_monitor(make_settings(VERIFY_EVENT_LOG="false"))
        await mon.initialize()
        bad = trongrid_record(11, ts=BLOCK_TS + 1000)
        bad["value"] = "garbage"
        tron.records += [bad, trongrid_record(12, ts=BLOCK_TS + 2000)]
        await mon.poll_once()
        assert alerts.queued == [tx_hash(12)]
    run(go())


def test_account_resume_after_restart_catches_up_missed(tmp_path):
    from app.database import SQLiteRepository
    from app.tron_monitor import STATE_ACCOUNT_CURSOR, STATE_ACCOUNT_FLOOR

    async def go():
        settings = make_settings(VERIFY_EVENT_LOG="false")
        repo = SQLiteRepository(str(tmp_path / "db.sqlite"))
        await repo.init()
        await repo.set_state(STATE_ACCOUNT_FLOOR, str(BLOCK_TS))
        await repo.set_state(STATE_ACCOUNT_CURSOR, str(BLOCK_TS))
        processor, _, alerts, stats = await make_processor(settings, repo=repo)
        tron = FakeTron()
        tron.head = (80_000_000, BLOCK_TS + 3_600_000)  # an hour later
        tron.records.append(trongrid_record(13, ts=BLOCK_TS + 1_800_000))  # while bot was down
        mon = AccountMonitor(settings, tron, repo, processor, stats, sleep=_nosleep)
        await mon.initialize()
        await mon.poll_once()
        assert alerts.queued == [tx_hash(13)]
    run(go())


def test_backfill_disabled_by_default_and_enabled_with_limit():
    async def go():
        settings = make_settings(BACKFILL_ENABLED="true", BACKFILL_LIMIT="2")
        mon, tron, repo, alerts = await account_monitor(settings)
        tron.records += [
            trongrid_record(20, ts=BLOCK_TS - 30_000),
            trongrid_record(21, ts=BLOCK_TS - 20_000),
            trongrid_record(22, value="5000000", ts=BLOCK_TS - 15_000),
            trongrid_record(23, ts=BLOCK_TS - 10_000),
        ]
        await mon.initialize()
        # 2 most recent matches, alerted oldest first, flagged as backfill
        assert alerts.queued == [tx_hash(21), tx_hash(23)]
        assert (await repo.get_transaction(tx_hash(23))).is_backfill
        # backfill does not repeat on next initialize
        mon.stats.initialized = False
        await mon.initialize()
        assert alerts.queued == [tx_hash(21), tx_hash(23)]
    run(go())


def test_block_monitor_decodes_events():
    async def go():
        settings = make_settings(MONITOR_MODE="blocks")
        processor, repo, alerts, stats = await make_processor(settings)
        tron = FakeTron()
        mon = BlockMonitor(settings, tron, repo, processor, stats, sleep=_nosleep)
        await mon.initialize()  # establishes block 70_000_000
        tron.head = (70_000_002, BLOCK_TS + 6000)
        tron.blocks[70_000_001] = [
            trx_transfer_info(30),                                                    # TRX transfer
            tx_info(31, [transfer_log(OTHER, WALLET, 1_000_087, contract=FAKE_USDT)]),  # fake token
            tx_info(32, [transfer_log(OTHER, THIRD, 1_000_087)]),                     # unrelated wallets
            tx_info(33, [transfer_log(OTHER, WALLET, 1_000_000)], block=70_000_001, ts=BLOCK_TS + 3000),
            {"id": "broken"},                                                         # malformed
        ]
        tron.blocks[70_000_002] = [
            tx_info(34, [transfer_log(WALLET, OTHER, 1_000_100)], block=70_000_002, ts=BLOCK_TS + 6000),
            tx_info(35, [transfer_log(WALLET, OTHER, 2_000_000)], block=70_000_002),
        ]
        await mon.poll_once()
        assert alerts.queued == [tx_hash(33), tx_hash(34)]
        assert (await repo.get_transaction(tx_hash(33))).direction == "INCOMING"
        assert (await repo.get_transaction(tx_hash(34))).direction == "OUTGOING"
        assert await repo.get_state("blocks.last_block") == "70000002"
        await mon.poll_once()  # nothing new
        assert len(alerts.queued) == 2
    run(go())


def test_detection_latency_uses_chain_timestamp():
    async def go():
        clock = lambda: BLOCK_TS + 3000 + 1120  # detected 1.120 s after the block
        mon, tron, repo, alerts = await account_monitor(make_settings(VERIFY_EVENT_LOG="false"), clock=clock)
        await mon.initialize()
        tron.records.append(trongrid_record(40, ts=BLOCK_TS + 3000))
        await mon.poll_once()
        tx = await repo.get_transaction(tx_hash(40))
        assert tx.block_timestamp_ms == BLOCK_TS + 3000
        assert tx.detected_at_ms - tx.block_timestamp_ms == 1120
        assert mon.stats.last_detection_latency_ms == 1120
    run(go())


def test_outgoing_only_account_mode_default():
    """Production default: only transfers SENT by the wallet alert; TronGrid asked with only_from."""
    async def go():
        settings = make_settings(ALERT_DIRECTIONS="OUTGOING", VERIFY_EVENT_LOG="false")
        mon, tron, repo, alerts = await account_monitor(settings)
        await mon.initialize()
        tron.records += [
            trongrid_record(50, sender=OTHER, recipient=WALLET, ts=BLOCK_TS + 3000),   # incoming -> no
            trongrid_record(51, sender=WALLET, recipient=OTHER, ts=BLOCK_TS + 3000),   # outgoing -> yes
            trongrid_record(52, sender=WALLET, recipient=WALLET, ts=BLOCK_TS + 3000),  # self -> yes
        ]
        await mon.poll_once()
        assert alerts.queued == [tx_hash(51), tx_hash(52)]
        assert all(c[1]["only_from"] for c in tron.calls if c[0] == "trc20")
    run(go())


def test_outgoing_only_even_if_api_ignores_only_from():
    """The filter itself rejects incoming, so a provider ignoring only_from cannot cause incoming alerts."""
    async def go():
        settings = make_settings(ALERT_DIRECTIONS="OUTGOING", VERIFY_EVENT_LOG="false")
        mon, tron, repo, alerts = await account_monitor(settings)

        original = tron.get_trc20_transfers

        async def ignore_only_from(*args, **kwargs):
            kwargs["only_from"] = False
            return await original(*args, **kwargs)

        tron.get_trc20_transfers = ignore_only_from
        await mon.initialize()
        tron.records += [trongrid_record(53, sender=OTHER, recipient=WALLET, ts=BLOCK_TS + 3000),
                         trongrid_record(54, sender=WALLET, recipient=OTHER, ts=BLOCK_TS + 3000)]
        await mon.poll_once()
        assert alerts.queued == [tx_hash(54)]
    run(go())


def test_outgoing_only_block_mode():
    async def go():
        settings = make_settings(MONITOR_MODE="blocks", ALERT_DIRECTIONS="OUTGOING")
        processor, repo, alerts, stats = await make_processor(settings)
        tron = FakeTron()
        mon = BlockMonitor(settings, tron, repo, processor, stats, sleep=_nosleep)
        await mon.initialize()
        tron.head = (70_000_001, BLOCK_TS + 3000)
        tron.blocks[70_000_001] = [
            tx_info(55, [transfer_log(OTHER, WALLET, 1_000_087)]),
            tx_info(56, [transfer_log(WALLET, OTHER, 1_000_087)]),
        ]
        await mon.poll_once()
        assert alerts.queued == [tx_hash(56)]
    run(go())
