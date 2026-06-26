"""
Tests for bot/services/scoring.py.
Each test case maps 1-to-1 to a row in the worked-examples table
from docx/02_SCORING_RULES.md (rows 1–10).
"""
import pytest
from bot.services.scoring import score_prediction, tiebreak_key, Prediction, Match

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pred(
    home: int,
    away: int,
    doubled: bool = False,
    playoff_team: str | None = None,
    playoff_method: str | None = None,
) -> Prediction:
    return Prediction(
        pred_home=home,
        pred_away=away,
        is_doubled=doubled,
        playoff_team=playoff_team,
        playoff_method=playoff_method,
    )


def match(
    home: int,
    away: int,
    extra_time: bool = False,
    ot_winner: str | None = None,
    ot_method: str | None = None,
) -> Match:
    return Match(
        score_home=home,
        score_away=away,
        went_to_extra_time=extra_time,
        ot_pen_winner=ot_winner,
        ot_pen_method=ot_method,
    )


# ---------------------------------------------------------------------------
# 10 worked examples from docx/02_SCORING_RULES.md
# ---------------------------------------------------------------------------

def test_case_1_exact_score_no_double():
    """Прогноз 2:1 | нет удвоения | Факт 2:1 → base=3, bonus=0, total=3"""
    result = score_prediction(pred(2, 1), match(2, 1))
    assert result["base_points"] == 3
    assert result["base_final"] == 3
    assert result["bonus_points"] == 0
    assert result["total_points"] == 3


def test_case_2_correct_outcome_wrong_score():
    """Прогноз 2:1 | нет удвоения | Факт 3:0 (хозяин победил) → base=1, bonus=0, total=1"""
    result = score_prediction(pred(2, 1), match(3, 0))
    assert result["base_points"] == 1
    assert result["base_final"] == 1
    assert result["bonus_points"] == 0
    assert result["total_points"] == 1


def test_case_3_wrong_outcome():
    """Прогноз 2:1 | нет удвоения | Факт 1:1 (ничья — неверный исход) → base=0, bonus=0, total=0"""
    result = score_prediction(pred(2, 1), match(1, 1))
    assert result["base_points"] == 0
    assert result["base_final"] == 0
    assert result["bonus_points"] == 0
    assert result["total_points"] == 0


def test_case_4_exact_score_with_double():
    """Прогноз 2:1 | удвоение | Факт 2:1 → base=3, после удвоения 3×2=6, bonus=0, total=6"""
    result = score_prediction(pred(2, 1, doubled=True), match(2, 1))
    assert result["base_points"] == 3
    assert result["base_final"] == 6
    assert result["bonus_points"] == 0
    assert result["total_points"] == 6


def test_case_5_wrong_outcome_with_double_penalty():
    """Прогноз 2:1 | удвоение | Факт 1:1 → base=0, штраф удвоения −1, total=−1"""
    result = score_prediction(pred(2, 1, doubled=True), match(1, 1))
    assert result["base_points"] == 0
    assert result["base_final"] == -1
    assert result["bonus_points"] == 0
    assert result["total_points"] == -1


def test_case_6_draw_exact_playoff_winner_no_method():
    """
    Прогноз 1:1 | нет удвоения | Факт 1:1, ОТ, победила А |
    Пик: 'Команда А' (без метода) → base=3, bonus=+1, total=4
    """
    result = score_prediction(
        pred(1, 1, playoff_team="Команда А"),
        match(1, 1, extra_time=True, ot_winner="Команда А", ot_method="OT"),
    )
    assert result["base_points"] == 3
    assert result["base_final"] == 3
    assert result["bonus_points"] == 1
    assert result["total_points"] == 4


def test_case_7_draw_correct_outcome_playoff_winner_and_method():
    """
    Прогноз 0:0 | нет удвоения | Факт 2:2, ПЕН, победила А |
    Пик: 'Команда А, ПЕН' → base=1 (исход верный, счёт нет), bonus=+2, total=3
    """
    result = score_prediction(
        pred(0, 0, playoff_team="Команда А", playoff_method="PEN"),
        match(2, 2, extra_time=True, ot_winner="Команда А", ot_method="PEN"),
    )
    assert result["base_points"] == 1
    assert result["base_final"] == 1
    assert result["bonus_points"] == 2
    assert result["total_points"] == 3


def test_case_8_draw_exact_double_wrong_method():
    """
    Прогноз 1:1 | удвоение | Факт 1:1, ОТ, победила А |
    Пик: 'Команда А, ПЕН' (метод неверный) →
    base=3→6 (удвоение только базу), bonus=0, total=6
    Ключевое правило: удвоение не штрафует за неверный метод.
    """
    result = score_prediction(
        pred(1, 1, doubled=True, playoff_team="Команда А", playoff_method="PEN"),
        match(1, 1, extra_time=True, ot_winner="Команда А", ot_method="OT"),
    )
    assert result["base_points"] == 3
    assert result["base_final"] == 6
    assert result["bonus_points"] == 0
    assert result["total_points"] == 6


def test_case_9_draw_exact_wrong_playoff_winner():
    """
    Прогноз 1:1 | нет удвоения | Факт 1:1, ОТ, победила Б |
    Пик: 'Команда А' (не угадал победителя) → base=3, bonus=0, total=3
    """
    result = score_prediction(
        pred(1, 1, playoff_team="Команда А"),
        match(1, 1, extra_time=True, ot_winner="Команда Б", ot_method="OT"),
    )
    assert result["base_points"] == 3
    assert result["base_final"] == 3
    assert result["bonus_points"] == 0
    assert result["total_points"] == 3


def test_case_10_non_draw_prediction_playoff_bonus_not_triggered():
    """
    Прогноз 2:1 (не ничья) | нет удвоения | Факт 1:1 → ОТ |
    playoff_team указан, но бонус не активируется — прогноз не был ничьёй.
    base=0 (неверный исход), bonus=0, total=0
    """
    result = score_prediction(
        pred(2, 1, playoff_team="Команда А"),
        match(1, 1, extra_time=True, ot_winner="Команда А", ot_method="OT"),
    )
    assert result["base_points"] == 0
    assert result["base_final"] == 0
    assert result["bonus_points"] == 0
    assert result["total_points"] == 0


# ---------------------------------------------------------------------------
# Tiebreak key
# ---------------------------------------------------------------------------

def test_tiebreak_key_ordering():
    """
    Игрок с большим числом точных счётов должен идти выше при равных total_points.
    """
    player_a = [
        {"base_points": 3, "base_final": 3, "bonus_points": 0, "total_points": 3},
        {"base_points": 1, "base_final": 1, "bonus_points": 0, "total_points": 1},
    ]
    player_b = [
        {"base_points": 1, "base_final": 1, "bonus_points": 0, "total_points": 1},
        {"base_points": 1, "base_final": 1, "bonus_points": 0, "total_points": 2},
        {"base_points": 0, "base_final": 0, "bonus_points": 0, "total_points": 1},
    ]
    key_a = tiebreak_key(player_a)
    key_b = tiebreak_key(player_b)
    # player_a: total=4, exact=1, outcomes=2
    assert key_a == (4, 1, 2)
    # player_b: total=4, exact=0, outcomes=2
    assert key_b == (4, 0, 2)
    assert key_a > key_b  # A ranks higher due to more exact scores
