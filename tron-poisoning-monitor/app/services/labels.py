"""Address labels for "possible exchange/service attribution".

Sources, in order: local file (``LABELS_FILE``, operator-curated), DB cache,
TronScan public tag (optional).  Labels are only ever presented as *possible*
attribution - a public tag is not proof of who controls an address.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from app.models import AddressLabelCache
from app.repository import _insert
from app.services.tron_service import label_category
from app.utils.address import try_normalize
from app.utils.clock import Clock, as_utc
from app.utils.logging import get_logger

log = get_logger(__name__)


class LabelService:
    def __init__(self, settings, session_factory, source, clock: Clock) -> None:
        self.s = settings
        self.sf = session_factory
        self.source = source
        self.clock = clock
        self.static: dict[str, tuple[str, str]] = {}
        self._load_file()

    def _load_file(self) -> None:
        path = Path(self.s.labels_file)
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("LABELS_FILE_INVALID", path=str(path), error=str(exc)[:100])
            return
        for addr, v in (data.get("labels") or {}).items():
            a = try_normalize(addr)
            if not a:
                continue
            if isinstance(v, str):
                self.static[a] = (v, label_category(v))
            elif isinstance(v, dict) and v.get("label"):
                self.static[a] = (str(v["label"]), str(v.get("category") or label_category(v["label"])))
        log.info("LABELS_LOADED", count=len(self.static), path=str(path))

    async def get(self, address: str) -> tuple[str, str, str] | None:
        """(label, category, source) or None."""
        if address in self.static:
            label, cat = self.static[address]
            return label, cat, "operator label file"
        now = self.clock.now()
        async with self.sf() as s:
            row = await s.get(AddressLabelCache, address)
        if row and as_utc(row.fetched_at) > now - timedelta(hours=self.s.label_cache_hours):
            return (row.label, row.category or "service", row.source) if row.label else None
        lab = None
        try:
            lab = await self.source.get_label(address)
        except Exception as exc:  # noqa: BLE001 - labels are best effort
            log.warning("LABEL_LOOKUP_FAILED", address=address, error=type(exc).__name__)
            return None
        async with self.sf() as s, s.begin():
            values = {
                "label": lab.label if lab else None,
                "category": lab.category if lab else None,
                "source": lab.source if lab else "lookup (no label)",
                "fetched_at": now,
            }
            stmt = _insert(s, AddressLabelCache).values(address=address, **values)
            await s.execute(stmt.on_conflict_do_update(index_elements=["address"], set_=values))
        return (lab.label, lab.category, lab.source) if lab else None
