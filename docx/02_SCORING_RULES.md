# Движок подсчёта очков — точная спецификация

Это критический модуль — реализовывать строго по этому файлу, не по интуиции.

## Входные данные на одну пару (прогноз, матч)

**Прогноз (prediction):**
- `pred_home`, `pred_away` — прогноз счёта на 90 минут
- `is_doubled` — bool, помечен ли удвоением
- `playoff_team` — какая команда пройдёт дальше (только если прогноз = ничья), nullable
- `playoff_method` — `OT` | `PEN` | `None` (опционально)

**Матч (match):**
- `score_home`, `score_away` — фактический счёт 90 минут
- `went_to_extra_time` — bool, дошло ли до ОТ/пенальти (т.е. была ничья в основное время)
- `ot_pen_winner` — какая команда прошла дальше (если `went_to_extra_time`)
- `ot_pen_method` — `OT` | `PEN`

## Псевдокод

```python
def outcome(home_score, away_score) -> str:
    if home_score > away_score:
        return "home_win"
    if home_score < away_score:
        return "away_win"
    return "draw"

def score_prediction(pred, match) -> dict:
    # ---- 1. Базовые очки за счёт 90 минут ----
    exact = (pred.pred_home == match.score_home and pred.pred_away == match.score_away)
    same_outcome = outcome(pred.pred_home, pred.pred_away) == outcome(match.score_home, match.score_away)

    if exact:
        base = 3
    elif same_outcome:
        base = 1
    else:
        base = 0

    # ---- 2. Удвоение — только база, бонус ОТ/ПЕН не трогаем ----
    if pred.is_doubled:
        base_final = base * 2 if base > 0 else -1
    else:
        base_final = base

    # ---- 3. Бонус ОТ/ПЕН — независимо от удвоения ----
    bonus = 0
    predicted_draw = outcome(pred.pred_home, pred.pred_away) == "draw"

    if predicted_draw and match.went_to_extra_time:
        if pred.playoff_team == match.ot_pen_winner:
            if pred.playoff_method is None:
                bonus = 1
            elif pred.playoff_method == match.ot_pen_method:
                bonus = 2
            else:
                bonus = 0  # неверно указан способ
        else:
            bonus = 0  # не угадан победитель

    total = base_final + bonus

    return {
        "base_points": base,            # для тай-брейка: 3 = точный счёт, 1 = верный исход
        "base_final": base_final,       # после удвоения/штрафа
        "bonus_points": bonus,
        "total_points": base_final + bonus,
    }
```

Функция идемпотентна — безопасно гонять повторно при `/recalc`.

## Worked-примеры (использовать как тест-кейсы)

| № | Прогноз | Удвоение | Факт 90' | ОТ/ПЕН | Пик плей-офф | base | bonus | total |
|---|---|---|---|---|---|---|---|---|
| 1 | 2:1 | нет | 2:1 | — | — | 3 | 0 | **3** |
| 2 | 2:1 | нет | 3:0 (домашняя выиграла) | — | — | 1 | 0 | **1** |
| 3 | 2:1 | нет | 1:1 | — | — | 0 | 0 | **0** |
| 4 | 2:1 | да | 2:1 | — | — | 3→6 | 0 | **6** |
| 5 | 2:1 | да | 1:1 (не угадал) | — | — | 0→−1 | 0 | **−1** |
| 6 | 1:1 | нет | 1:1 (точно) | ОТ, победила команда А | "Команда А" (без метода) | 3 | +1 | **4** |
| 7 | 0:0 | нет | 2:2 (исход верный, не точно) | ПЕН, победила команда А | "Команда А, ПЕН" | 1 | +2 | **3** |
| 8 | 1:1 | да | 1:1 (точно) | ОТ, победила команда А | "Команда А, ПЕН" (метод неверный) | 3→6 | 0 | **6** |
| 9 | 1:1 | нет | 1:1 | ОТ, победила команда Б | "Команда А" (не угадал) | 3 | 0 | **3** |
| 10 | 2:1 (не ничья) | нет | 1:1 → ОТ | — | playoff_team указан | 0 (исход неверный) | 0 (бонус не активируется — прогноз не был ничьёй) | **0** |

Пример 8 показывает ключевое правило: удвоение умножает только базу (3→6), даже если bonus = 0 из-за неверного метода — это не штраф по удвоению, а просто bonus=0 сам по себе.

## Тай-брейк (используется в `/standings`)

```python
def tiebreak_key(user_predictions):
    exact_count = count(p for p in user_predictions if p.base_points == 3)
    # "угадан исход" = угадан победитель/ничья, независимо от точности счёта —
    # поэтому считаем base_points >= 1 (включает и точные, и просто верный исход)
    correct_outcome_count = count(p for p in user_predictions if p.base_points >= 1)
    return (total_points, exact_count, correct_outcome_count)  # сортировка по убыванию всех трёх
```

Зафиксировано: пункт 2 тай-брейка = "просто угадал победителя", без требования точного счёта.
