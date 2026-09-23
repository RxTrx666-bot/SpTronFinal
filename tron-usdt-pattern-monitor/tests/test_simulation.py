"""36. Simulation mode must pass end-to-end (TEST→LARGE→TEST→LARGE→TEST, 250→40K, 1,000→100K)."""

from app.simulation.runner import run_simulation


async def test_simulation_passes(capsys):
    assert await run_simulation("sqlite+aiosqlite:///:memory:") == 0
    out = capsys.readouterr().out
    assert "All simulation checks passed" in out
    assert "WATCHLIST TEST TRANSFER DETECTED" in out
    assert "LARGE FOLLOW-UP DETECTED" in out
