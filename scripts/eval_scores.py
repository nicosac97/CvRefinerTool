"""How well do scores separate jobs you marked Good match from Bad match?

Run from the project root: uv run python -m scripts.eval_scores
"""

from sqlmodel import Session, select

from cvrefine.db import engine
from cvrefine.models import Job

MIN_LABELS = 10


def auc(good: list[float], bad: list[float]) -> float:
    """Probability that a random good job outscores a random bad one (ties count half)."""
    wins = sum(1.0 if g > b else 0.5 if g == b else 0.0 for g in good for b in bad)
    return wins / (len(good) * len(bad))


def best_threshold(good: list[float], bad: list[float]) -> tuple[float, float, float, float]:
    """Threshold maximizing F1 for "good"; returns (threshold, precision, recall, f1)."""
    best = (0.0, 0.0, 0.0, 0.0)
    for threshold in sorted(set(good + bad)):
        true_positive = sum(score >= threshold for score in good)
        false_positive = sum(score >= threshold for score in bad)
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
        recall = true_positive / len(good)
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        if f1 > best[3]:
            best = (threshold, precision, recall, f1)
    return best


def main() -> None:
    with Session(engine) as session:
        jobs = session.exec(select(Job).where(Job.feedback.is_not(None), Job.score.is_not(None))).all()
    good = [job.score for job in jobs if job.feedback == 1]
    bad = [job.score for job in jobs if job.feedback == -1]
    print(f"Labeled and scored jobs: {len(good)} good, {len(bad)} bad")
    if not good or not bad:
        print("Need at least one Good match and one Bad match label. Mark jobs on their detail page.")
        return
    if len(good) + len(bad) < MIN_LABELS:
        print(f"Fewer than {MIN_LABELS} labels: numbers below are noisy.")
    print(f"Mean score: good {sum(good) / len(good):.1f}, bad {sum(bad) / len(bad):.1f}")
    print(f"AUC: {auc(good, bad):.2f}  (0.5 = no better than chance, 1.0 = perfect separation)")
    threshold, precision, recall, f1 = best_threshold(good, bad)
    print(f"Best cut-off: score >= {threshold:.0f}  precision {precision:.0%}, recall {recall:.0%}, F1 {f1:.2f}")
    uncertain = [job for job in jobs if job.uncertain]
    if uncertain:
        wrong = sum((job.feedback == 1) != (job.score >= threshold) for job in uncertain)
        print(f"Uncertain-flagged jobs: {len(uncertain)}, misclassified at that cut-off: {wrong}")


if __name__ == "__main__":
    main()
