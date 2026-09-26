from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import VARIANT_ID

CASE_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,63}$")


@dataclass(frozen=True)
class CaseSet:
    version: str
    variant_id: str
    case_ids: tuple[str, ...]
    cases: dict[str, dict[str, Any]]


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def load_case_set(root: Path, expected_count: int = 100) -> CaseSet:
    root = root.resolve()
    manifest = _object(root / "case-set.json")
    if set(manifest) != {"case_set_version", "variant_id", "case_ids"}:
        raise ValueError("case-set.json has unexpected or missing fields")
    if manifest["variant_id"] != VARIANT_ID:
        raise ValueError(f"expected variant {VARIANT_ID}, got {manifest['variant_id']!r}")
    raw_ids = manifest["case_ids"]
    if not isinstance(raw_ids, list) or not all(isinstance(item, str) for item in raw_ids):
        raise ValueError("case_ids must be an array of strings")
    if len(raw_ids) != expected_count or len(set(raw_ids)) != expected_count:
        raise ValueError(f"case-set must contain exactly {expected_count} unique case IDs")
    if any(not CASE_ID_PATTERN.fullmatch(case_id) for case_id in raw_ids):
        raise ValueError("case-set contains an invalid case ID")
    version = manifest["case_set_version"]
    if not isinstance(version, str) or not version:
        raise ValueError("case_set_version must be a non-empty string")

    input_root = root / "inputs"
    actual_files = {path.stem: path for path in input_root.glob("*.json") if path.is_file()}
    if set(actual_files) != set(raw_ids):
        missing = sorted(set(raw_ids) - set(actual_files))
        extra = sorted(set(actual_files) - set(raw_ids))
        raise ValueError(f"inputs do not match case-set; missing={missing}, extra={extra}")
    cases = {case_id: _object(actual_files[case_id]) for case_id in raw_ids}
    for case_id, case in cases.items():
        if case.get("case_id") != case_id:
            raise ValueError(f"inputs/{case_id}.json has a mismatched case_id")
    return CaseSet(version, VARIANT_ID, tuple(raw_ids), cases)


def extract_case_details(case: dict[str, Any]) -> dict[str, Any]:
    """Uniformly extract normalized attributes from input case JSON."""
    case_id = str(case.get("case_id") or "")
    customer_request = case.get("customer_request", {})
    if not isinstance(customer_request, dict):
        customer_request = {}

    claims = customer_request.get("claims") or case.get("claims") or []
    claimed_order_id = customer_request.get("claimed_order_id")
    message = customer_request.get("message") or ""
    language = customer_request.get("language") or "vi"

    candidate_order_ids = case.get("candidate_order_ids") or []
    hints = case.get("hints", {})
    if not isinstance(hints, dict):
        hints = {}

    customer_unique_id = (
        case.get("customer_unique_id_hint")
        or case.get("customer_unique_id")
        or hints.get("customer_unique_id")
    )
    policy_version = case.get("policy_version") or "EC_POLICY_V2"
    investigation_scope = case.get("investigation_scope") or {}
    opened_at = case.get("opened_at")

    return {
        "case_id": case_id,
        "claims": claims,
        "claimed_order_id": claimed_order_id,
        "message": message,
        "language": language,
        "candidate_order_ids": candidate_order_ids,
        "customer_unique_id": customer_unique_id,
        "policy_version": policy_version,
        "investigation_scope": investigation_scope,
        "opened_at": opened_at,
    }

