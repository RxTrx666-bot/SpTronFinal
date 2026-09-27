import asyncio

from app.database import NewTransaction, SQLiteRepository
from app.transaction_parser import parse_trongrid_trc20_record
from tests.helpers import BLOCK_TS, make_processor, run, trongrid_record, tx_hash


def new_tx(n: int) -> NewTransaction:
    return NewTransaction(tx_hash=tx_hash(n), block_number=1, block_timestamp_ms=BLOCK_TS, direction="INCOMING",
                          sender="a", recipient="b", amount_raw=1_000_087, amount_usdt="1.000087",
                          contract_address="c", token_symbol="USDT", detected_at_ms=BLOCK_TS + 1000)


def test_unique_tx_hash_insert():
    async def go():
        repo = SQLiteRepository(":memory:")
        await repo.init()
        assert await repo.insert_transaction(new_tx(1)) is True
        assert await repo.insert_transaction(new_tx(1)) is False
        assert await repo.count_transactions() == 1
    run(go())


def test_same_transaction_alerts_once():
    async def go():
        processor, repo, alerts, _ = await make_processor()
        transfer = parse_trongrid_trc20_record(trongrid_record(1))
        assert await processor.process(transfer) is True
        assert await processor.process(transfer) is False
        assert await processor.process(transfer) is False
        assert alerts.queued == [tx_hash(1)]
    run(go())


def test_concurrent_processing_alerts_once():
    async def go():
        processor, repo, alerts, _ = await make_processor()
        transfer = parse_trongrid_trc20_record(trongrid_record(2))
        results = await asyncio.gather(*(processor.process(transfer) for _ in range(10)))
        assert sum(results) == 1 and alerts.queued == [tx_hash(2)]
    run(go())


def test_duplicate_safe_after_restart(tmp_path):
    db = str(tmp_path / "monitor.db")

    async def first_run():
        repo = SQLiteRepository(db)
        await repo.init()
        processor, _, alerts, _ = await make_processor(repo=repo)
        assert await processor.process(parse_trongrid_trc20_record(trongrid_record(3)))
        await repo.mark_alert_sent(tx_hash(3), BLOCK_TS + 2000)
        await repo.set_state("account.cursor_ms", "123")
        await repo.close()
        return alerts.queued

    async def second_run():
        repo = SQLiteRepository(db)
        await repo.init()
        processor, _, alerts, _ = await make_processor(repo=repo)
        assert await processor.process(parse_trongrid_trc20_record(trongrid_record(3))) is False
        assert await repo.get_state("account.cursor_ms") == "123"
        stored = await repo.get_transaction(tx_hash(3))
        assert stored.alert_status == "sent" and stored.amount_usdt == "1.000087"
        await repo.close()
        return alerts.queued

    assert run(first_run()) == [tx_hash(3)]
    assert run(second_run()) == []


def test_non_matching_is_not_stored():
    async def go():
        processor, repo, alerts, _ = await make_processor()
        assert not await processor.process(parse_trongrid_trc20_record(trongrid_record(4, value="2000000")))
        assert await repo.count_transactions() == 0 and alerts.queued == []
    run(go())
