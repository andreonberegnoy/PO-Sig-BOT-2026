"""Статистические утилиты для аналитики.

Сейчас тут:
- wilson_lower_bound: нижняя граница доверительного интервала Уилсона.
  Используем как «честный» WR вместо точечного p_hat = wins/n.
- decay_weight: экспоненциальный распад веса сигнала по возрасту.

Никаких сайд-эффектов, чистая математика; легко тестируется.
"""

from __future__ import annotations

import math
from typing import Optional


# z-score для двустороннего CI. По умолчанию 95%.
Z_95 = 1.959963984540054


def wilson_lower_bound(wins: float, n: float, z: float = Z_95) -> Optional[float]:
    """Нижняя граница доверительного интервала Уилсона для пропорции.

    Возвращает значение в [0, 1] или None если n <= 0. Поддерживает
    дробные wins/n (для weighted-агрегации с decay).

    Формула:
        p_hat = wins / n
        center = p_hat + z² / (2n)
        margin = z * sqrt( p_hat·(1-p_hat)/n + z²/(4n²) )
        lower  = (center - margin) / (1 + z²/n)

    При маленьких n даёт сильно консервативную оценку — это и нужно:
    отсекает «WR 75% на 4 сигналах».
    """
    if n is None or n <= 0:
        return None
    p_hat = max(0.0, min(1.0, wins / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p_hat + z2 / (2.0 * n)
    margin = z * math.sqrt(p_hat * (1.0 - p_hat) / n + z2 / (4.0 * n * n))
    lower = (center - margin) / denom
    return max(0.0, min(1.0, lower))


def decay_weight(age_seconds: float, half_life_days: Optional[float]) -> float:
    """Экспоненциальный распад веса по возрасту.

    weight = 0.5 ^ (age_days / half_life_days)
           = exp(-ln(2) * age_days / half_life_days)

    Если half_life_days is None / <= 0 — возвращает 1.0 (распад выключен).
    """
    if half_life_days is None or half_life_days <= 0:
        return 1.0
    if age_seconds <= 0:
        return 1.0
    age_days = age_seconds / 86400.0
    return math.pow(0.5, age_days / float(half_life_days))
