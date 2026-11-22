"""Load and validate the synthetic materials bundle.

The bundle is read-only.  Every derived judgement is computed by the other
modules and persisted as an event log outside the bundle.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class BundleError(ValueError):
    """Raised when the source materials do not satisfy their contract."""


def load_bundle(directory: str | Path) -> "Bundle":
    directory = Path(directory)
    raw = (directory / "data.json").read_bytes()
    data = json.loads(raw)
    contract = json.loads((directory / "contract.json").read_text(encoding="utf-8"))
    if set(data) != set(contract["tables"]):
        raise BundleError("资料表与契约不一致")
    for name, meta in contract["tables"].items():
        rows = data[name]
        if not isinstance(rows, list) or not rows:
            raise BundleError("资料表为空：" + name)
        ids = [row.get("id") for row in rows]
        if any(not isinstance(i, str) or not i for i in ids):
            raise BundleError("资料编号为空：" + name)
        if len(ids) != len(set(ids)):
            raise BundleError("资料编号重复：" + name)
        for row in rows:
            if set(row) != set(meta["fields"]):
                raise BundleError("字段与契约不一致：" + name)
        for column, target in meta["references"].items():
            valid = {r["id"] for r in data[target]}
            if any(row[column] not in valid for row in rows):
                raise BundleError("关联资料不存在：" + name + "." + column)
    return Bundle(data, contract, hashlib.sha256(raw).hexdigest(), directory)


class Bundle:
    """Immutable view over the six-team synthetic input."""

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]],
        contract: dict[str, Any],
        sha256: str,
        source: Path,
    ):
        self.tables = tables
        self.contract = contract
        self.sha256 = sha256
        self.source = source
        for name, rows in tables.items():
            setattr(self, name, rows)
        self.by_id = {name: {row["id"]: row for row in rows} for name, rows in tables.items()}

    def get(self, table: str, record_id: str) -> dict[str, Any]:
        try:
            return self.by_id[table][record_id]
        except KeyError as exc:  # pragma: no cover - defensive
            raise BundleError(f"未知记录：{table}.{record_id}") from exc

    def receipt(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "rows": {name: len(rows) for name, rows in self.tables.items()},
        }
