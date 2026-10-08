"""The ranking rule: batch/ranking_rule.yaml -> the app's `Logic` plus the sales window.

Logic.from_dict forgives mistakes (unknown keys and criteria are dropped, an order without sum_total is replaced
by the default). Here every mistake is an error, so an edit to the rule can never be silently ignored.
"""
from __future__ import annotations
from dataclasses import dataclass, fields
from pathlib import Path

import yaml

from app.analysis.cohorts import CRITERIA, Logic


@dataclass
class Rule:
    logic: Logic
    analysis_months: int


def load_rule(path: Path) -> Rule:
    d = yaml.safe_load(Path(path).read_text())
    months = d.pop("analysis_months")
    unknown = set(d) - {f.name for f in fields(Logic)}
    if unknown:
        raise ValueError(f"{path}: unknown setting(s) {sorted(unknown)}")
    order = d.get("order") or []
    bad = [c for c in order if c not in CRITERIA]
    if bad:
        raise ValueError(f"{path}: unknown criteria {bad}; known: {list(CRITERIA)}")
    if "sum_total" not in order:
        raise ValueError(f"{path}: `order` must contain sum_total, otherwise the app ignores it and uses its default order")
    return Rule(logic=Logic.from_dict(d), analysis_months=int(months))
