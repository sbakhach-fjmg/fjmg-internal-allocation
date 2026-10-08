from pathlib import Path

import pytest

from batch.rule import load_rule

RULE = Path(__file__).resolve().parents[1] / "ranking_rule.yaml"


def test_committed_rule_is_the_app_default_ranking_rule():
    rule = load_rule(RULE)
    assert rule.logic.order == ["sum_total", "volume", "total", "front", "sum_front", "back", "slot", "days", "similar"]
    assert (rule.logic.k, rule.logic.min_cohort, rule.logic.min_store, rule.logic.min_best) == (8, 5, 3, 5)
    assert (rule.logic.over_abs, rule.logic.over_pct) == (2000, 0)
    assert (rule.logic.tie_units_pct, rule.logic.tie_gross, rule.logic.tie_days, rule.logic.tie_rel) == (0.15, 500, 5, 0.15)
    assert rule.analysis_months == 6


def _rule_with(tmp_path, text):
    p = tmp_path / "rule.yaml"
    p.write_text(RULE.read_text() + "\n" + text)
    return p


def test_order_without_sum_total_is_rejected_not_silently_replaced(tmp_path):
    with pytest.raises(ValueError, match="sum_total"):
        load_rule(_rule_with(tmp_path, "order: [volume, total]"))


def test_misspelled_setting_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="tie_grss"):
        load_rule(_rule_with(tmp_path, "tie_grss: 800"))


def test_unknown_criterion_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="sum_totl"):
        load_rule(_rule_with(tmp_path, "order: [sum_total, sum_totl, volume]"))
