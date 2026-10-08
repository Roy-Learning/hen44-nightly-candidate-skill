"""Deterministic producer for one declared HEN qualification prescreen.

The host independently verifies all artifact bindings and runs the declared
semantic review.  This script only canonicalizes the three quoted page-body
claims and never writes a workspace file or emits a decision/mutation shape.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

from agents.evidence_text_projection import canonical_source_quote


SCHEMA_VERSION = 1
REQUEST_PATH = "hen44_candidate_screening.json"
CRITERIA = ("entity_type", "independent_operation", "regional_scope")
ARTIFACT = re.compile(r"^artifacts/spill_[0-9a-f]{32}\.txt$")


class ScreeningRejected(ValueError):
    pass


def exact_object(value: object, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ScreeningRejected(f"{label} must contain exactly {sorted(fields)}")
    return value


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ScreeningRejected(f"{label} is invalid")
    return value


def normalized_claim(raw: object, criterion: str) -> dict[str, str]:
    claim = exact_object(raw, {"evidence_file", "quote"}, f"claims.{criterion}")
    evidence_file = text(claim["evidence_file"], f"claims.{criterion}.evidence_file")
    quote = text(claim["quote"], f"claims.{criterion}.quote")
    if ARTIFACT.fullmatch(evidence_file) is None:
        raise ScreeningRejected(f"claims.{criterion}.evidence_file is invalid")
    try:
        source = Path(evidence_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise ScreeningRejected(f"claims.{criterion}.evidence_file is unavailable") from exc
    canonical = canonical_source_quote(source, quote)
    if canonical is None:
        raise ScreeningRejected(
            f"claims.{criterion}.quote is not a continuous page-body excerpt"
        )
    return {"value": canonical, "evidence_file": evidence_file, "quote": canonical}


def assess(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    value = exact_object(payload, {"subject", "claims"}, "screening request")
    subject = exact_object(value["subject"], {"name", "domain"}, "subject")
    canonical_subject = {key: text(subject[key], f"subject.{key}") for key in ("name", "domain")}
    claims = exact_object(value["claims"], set(CRITERIA), "claims")
    return {
        "schema_version": SCHEMA_VERSION,
        "subject": canonical_subject,
        "claims": {criterion: normalized_claim(claims[criterion], criterion) for criterion in CRITERIA},
    }


def main() -> int:
    try:
        if len(sys.argv) != 2 or sys.argv[1] != REQUEST_PATH:
            raise ScreeningRejected("expected the fixed screening request path")
        print(json.dumps(assess(Path(REQUEST_PATH)), ensure_ascii=False, sort_keys=True))
        return 0
    except (ScreeningRejected, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"readonly screening rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
