"""
Scoring engine — implemented strictly per docx/02_SCORING_RULES.md.
Pure functions; safe to call repeatedly (idempotent for /recalc).
"""
from typing import TypedDict


class Prediction(TypedDict):
    pred_home: int
    pred_away: int
    is_doubled: bool
    playoff_team: str | None
    playoff_method: str | None  # 'OT' | 'PEN' | None


class Match(TypedDict):
    score_home: int
    score_away: int
    went_to_extra_time: bool
    ot_pen_winner: str | None
    ot_pen_method: str | None  # 'OT' | 'PEN' | None


class ScoreResult(TypedDict):
    base_points: int    # 3 = exact, 1 = correct outcome — used for tiebreak
    base_final: int     # after doubling / penalty
    bonus_points: int
    total_points: int


def _outcome(home: int, away: int) -> str:
    if home > away:
        return "home_win"
    if home < away:
        return "away_win"
    return "draw"


def score_prediction(pred: Prediction, match: Match) -> ScoreResult:
    # 1. Base points for 90-minute score
    exact = pred["pred_home"] == match["score_home"] and pred["pred_away"] == match["score_away"]
    same_outcome = _outcome(pred["pred_home"], pred["pred_away"]) == _outcome(
        match["score_home"], match["score_away"]
    )

    if exact:
        base = 3
    elif same_outcome:
        base = 1
    else:
        base = 0

    # 2. Doubling — applies only to base, never to OT/PEN bonus
    if pred["is_doubled"]:
        base_final = base * 2 if base > 0 else -1
    else:
        base_final = base

    # 3. OT/PEN bonus — independent of doubling
    bonus = 0
    predicted_draw = _outcome(pred["pred_home"], pred["pred_away"]) == "draw"

    if predicted_draw and match["went_to_extra_time"]:
        if pred["playoff_team"] == match["ot_pen_winner"]:
            if pred["playoff_method"] is None:
                bonus = 1
            elif pred["playoff_method"] == match["ot_pen_method"]:
                bonus = 2
            else:
                bonus = 0  # correct winner but wrong method
        # else: wrong winner → bonus stays 0

    return ScoreResult(
        base_points=base,
        base_final=base_final,
        bonus_points=bonus,
        total_points=base_final + bonus,
    )


def tiebreak_key(user_predictions: list[ScoreResult]) -> tuple[int, int, int]:
    """Returns (total_points, exact_count, correct_outcome_count) — sort descending on all three."""
    total = sum(p["total_points"] for p in user_predictions)
    exact_count = sum(1 for p in user_predictions if p["base_points"] == 3)
    correct_outcome_count = sum(1 for p in user_predictions if p["base_points"] >= 1)
    return (total, exact_count, correct_outcome_count)
