"""Tinh chinh output tu debug/raw, KHONG goi MCP. Chi dung ref da co trong trace cua lan chay nay.

  python scripts/refine.py all                  # evidence_refs = moi ref da thu thap cua case
  python scripts/refine.py add shipment item    # them ref cua cac domain nay vao moi case
  python scripts/refine.py restore              # tra lai output goc cua lan chay

Lan dau chay se sao luu outputs/ -> debug/outputs_backup/.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
OUT = ROOT / "outputs"
RAW = ROOT / "debug" / "raw"
BACKUP = ROOT / "debug" / "outputs_backup"


def refs_by_domain(case_id: str) -> dict[str, list[str]]:
    raw = json.loads((RAW / f"{case_id}.json").read_text(encoding="utf-8"))
    found: dict[str, list[str]] = {}
    for call in raw.get("calls", {}).values():
        ev = call.get("evidence")
        if ev:
            found.setdefault(ev["domain"], []).append(ev["evidence_ref"])
    return found


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if not BACKUP.exists():
        shutil.copytree(OUT, BACKUP)
        print(f"Backup -> {BACKUP}")
    if mode == "restore":
        for path in BACKUP.glob("*.json"):
            shutil.copy2(path, OUT / path.name)
        print("Restored original outputs")
        return
    if mode not in {"all", "add"}:
        raise SystemExit(__doc__)
    extra = set(sys.argv[2:])
    changed = 0
    for path in sorted(BACKUP.glob("*.json")):
        out = json.loads(path.read_text(encoding="utf-8"))
        domains = refs_by_domain(path.stem)
        refs = list(out["evidence_refs"])
        for domain, domain_refs in domains.items():
            if mode == "all" or domain in extra:
                refs.extend(r for r in domain_refs if r not in refs)
        refs = refs[:30]
        if refs != out["evidence_refs"]:
            changed += 1
        out["evidence_refs"] = refs
        for claim in out.get("claim_assessments", []):
            claim["evidence_refs"] = refs
        (OUT / path.name).write_text(
            json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"Updated evidence_refs for {changed} cases (mode={mode} {sorted(extra)})")


if __name__ == "__main__":
    main()
