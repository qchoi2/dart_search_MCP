from __future__ import annotations

import json
import re
import secrets
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from app.storage.atomic import atomic_write_json


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,96}$")


class BatchPlanStore:
    """Short-lived approval plans. Plans deliberately do not survive a restart."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._plans: dict[str, dict[str, Any]] = {}
        self._lineages: dict[str, str] = {}
        self._declined_until: dict[str, tuple[float, float]] = {}

    def lookup(self, *, lineage: str, scope_signature: str, scope_weight: float) -> dict[str, Any] | None:
        self._purge()
        now = self.clock()
        declined = self._declined_until.get(lineage)
        if declined is not None:
            declined_until, old_weight = declined
            if declined_until > now and scope_weight < old_weight * 1.5:
                return {
                    "status": "recommendation_suppressed",
                    "lineage": lineage,
                    "suppressed_seconds": max(1, int(declined_until - now)),
                }
            self._declined_until.pop(lineage, None)
        existing_id = self._lineages.get(lineage)
        existing = self._plans.get(existing_id or "")
        if existing is None:
            return None
        if existing.get("scope_signature") == scope_signature:
            result = deepcopy(existing)
            result["plan_reused"] = True
            return result
        if scope_weight < float(existing.get("scope_weight", scope_weight)) * 1.5:
            return {
                "status": "recommendation_suppressed",
                "lineage": lineage,
                "suppressed_seconds": max(1, int(existing["expires_at_epoch"] - now)),
            }
        self._plans.pop(existing_id, None)
        self._lineages.pop(lineage, None)
        return None

    def issue(self, *, lineage: str, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        self._purge()
        now = self.clock()
        plan_id = secrets.token_urlsafe(18)
        plan = deepcopy(payload)
        plan.update(
            {
                "plan_id": plan_id,
                "lineage": lineage,
                "created_at_epoch": now,
                "expires_at_epoch": now + self.ttl_seconds,
                "plan_reused": False,
            }
        )
        self._plans[plan_id] = plan
        self._lineages[lineage] = plan_id
        return deepcopy(plan), False

    def get(self, plan_id: str) -> dict[str, Any] | None:
        self._purge()
        plan = self._plans.get(plan_id)
        return deepcopy(plan) if plan is not None else None

    def decline(self, plan_id: str) -> bool:
        plan = self.get(plan_id)
        if plan is None:
            return False
        lineage = str(plan["lineage"])
        self._declined_until[lineage] = (
            self.clock() + self.ttl_seconds,
            float(plan.get("scope_weight", 1.0)),
        )
        self._plans.pop(plan_id, None)
        self._lineages.pop(lineage, None)
        return True

    def consume(self, plan_id: str) -> dict[str, Any] | None:
        plan = self.get(plan_id)
        if plan is None:
            return None
        self._plans.pop(plan_id, None)
        self._lineages.pop(str(plan["lineage"]), None)
        return plan

    def _purge(self) -> None:
        now = self.clock()
        expired = [key for key, value in self._plans.items() if value["expires_at_epoch"] <= now]
        for key in expired:
            lineage = str(self._plans[key]["lineage"])
            self._plans.pop(key, None)
            if self._lineages.get(lineage) == key:
                self._lineages.pop(lineage, None)
        self._declined_until = {
            key: value for key, value in self._declined_until.items() if value[0] > now
        }


class JsonRecordStore:
    def __init__(self, root: Path, *, retention_days: int = 7) -> None:
        self.root = root
        self.retention_seconds = retention_days * 24 * 60 * 60

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{secrets.token_urlsafe(15)}"

    def save(self, record_id: str, payload: dict[str, Any]) -> None:
        atomic_write_json(self._path(record_id), payload)

    def load(self, record_id: str) -> dict[str, Any] | None:
        path = self._path(record_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def delete(self, record_id: str) -> None:
        path = self._path(record_id)
        if path.exists():
            path.unlink()

    def cleanup(self, *, now: float | None = None) -> int:
        if not self.root.exists():
            return 0
        cutoff = (time.time() if now is None else now) - self.retention_seconds
        removed = 0
        for path in self.root.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def _path(self, record_id: str) -> Path:
        if not _ID_RE.fullmatch(record_id):
            raise ValueError("invalid record id")
        return self.root / f"{record_id}.json"


class BatchCheckpointStore(JsonRecordStore):
    pass


class BatchResultStore(JsonRecordStore):
    pass
