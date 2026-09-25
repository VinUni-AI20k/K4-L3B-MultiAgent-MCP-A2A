from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator


def test_public_contracts_match_locked_baseline() -> None:
    root = Path(__file__).resolve().parents[1] / "contracts"
    expected = json.loads((root / "schema-lock.json").read_text(encoding="utf-8"))
    actual = {}
    for path in sorted((root / "schemas").glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        actual[path.name] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert actual == expected, "Public contracts changed; restore the official release schemas"
