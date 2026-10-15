import pytest

from scripts.eval_scores import auc, best_threshold


def test_auc():
    assert auc([80, 90], [10, 20]) == 1.0
    assert auc([10], [80]) == 0.0
    assert auc([50], [50]) == 0.5


def test_best_threshold():
    threshold, precision, recall, f1 = best_threshold([70, 80, 90], [20, 75])
    # >= 70 keeps all good jobs and one bad (F1 0.86); >= 80 drops a good one (F1 0.80).
    assert threshold == 70 and precision == 0.75 and recall == 1.0 and f1 == pytest.approx(6 / 7)
