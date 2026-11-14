from __future__ import annotations

import hashlib
import json
import math
import platform
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrievalCase(StrictModel):
    id: str
    query: str
    category: str
    filters: dict = Field(default_factory=dict)
    relevant: list[str]
    no_answer: bool = False
    label_status: Literal["seed", "reviewed"] = "seed"
    rationale: str

    @model_validator(mode="after")
    def labels(self):
        if self.no_answer != (not self.relevant):
            raise ValueError("no_answer must be true exactly when relevant is empty")
        if len(self.relevant) != len(set(self.relevant)):
            raise ValueError("duplicate relevant ids")
        return self


class ToolExpectation(StrictModel):
    name: str
    arguments: dict = Field(default_factory=dict, description="Subset match; lists mean contains all expected elements")
    min_calls: int = Field(1, ge=1)


class AgentCase(StrictModel):
    id: str
    category: str
    prompt: str
    profile: dict = Field(default_factory=dict)
    fixture: Literal["none", "plan", "logs", "memory", "candidate"] = "none"
    expected_tools: list[ToolExpectation] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=list)
    required_patterns: list[str] = Field(default_factory=list)
    ordered_tools: list[str] = Field(default_factory=list)
    expect_plan_saved: bool = False
    expect_log_saved: bool = False
    check_plan_grounding: bool = False
    tool_result_injection: str | None = None
    rationale: str


def load_cases(path: Path, schema):
    cases = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(schema.model_validate_json(line))
        except Exception as exc:
            raise ValueError(f"{path}:{line_no}: {exc}") from exc
    if not cases or len({c.id for c in cases}) != len(cases):
        raise ValueError("dataset must be nonempty with unique ids")
    return cases


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows), encoding="utf-8")


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def metadata(cases: Path, catalog: list[dict], config: dict):
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    source_files = sorted([*ROOT.glob("app/**/*.py"), *ROOT.glob("eval/*.py"), *ROOT.glob("scripts/eval_*.py")])
    source_hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    return {"source_sha256": fingerprint(source_hashes), "source_files": source_hashes, "created_at": datetime.now(timezone.utc).isoformat(), "git_commit": git.stdout.strip() or None, "git_worktree_dirty": bool(dirty.stdout.strip()),
            "python": platform.python_version(), "dataset": str(cases), "dataset_sha256": hashlib.sha256(cases.read_bytes()).hexdigest(),
            "catalog_sha256": fingerprint(sorted(catalog, key=lambda d: d["id"])), "catalog_count": len(catalog), "config": config}


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * fraction
    low = int(pos)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (pos - low)


def retrieval_metrics(ranked: list[str], relevant: list[str], k: int, no_answer=False):
    if len(ranked) != len(set(ranked)):
        raise ValueError("duplicate ranked ids")
    ranked, gold = ranked[:k], set(relevant)
    if no_answer:
        return {"recall": None, "mrr": None, "ndcg": None, "no_answer_correct": not ranked}
    if not gold:
        raise ValueError("positive case has no relevant labels")
    hits = [int(eid in gold) for eid in ranked]
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(gold), k)))
    return {"recall": sum(hits) / len(gold), "mrr": next((1 / (i + 1) for i, hit in enumerate(hits) if hit), 0),
            "ndcg": sum(hit / math.log2(i + 2) for i, hit in enumerate(hits)) / ideal, "no_answer_correct": None}


def subset_matches(expected: Any, actual: Any):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset_matches(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and all(any(subset_matches(e, a) for a in actual) for e in expected)
    return expected == actual


def counts(values):
    return dict(Counter(values))
