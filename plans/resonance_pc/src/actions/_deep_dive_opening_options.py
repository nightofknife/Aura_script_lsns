"""Inputs shared by the Deep Dive loop and its game entry action."""

from numbers import Real


DEFAULT_DIVE_DEPTH = 1
DEFAULT_OPENING_STRATEGY = 'source'
DIVE_DEPTHS = ('Ⅰ', 'Ⅱ', 'Ⅲ', 'Ⅳ', 'Ⅴ')
OPENING_STRATEGIES = {
    'source': '源流之策',
    'waxing_waning': '盈缺之策',
    'revival': '回生之策',
    'defense': '捍卫之策',
}


def validate_opening_options(dive_depth: int, opening_strategy: str) -> tuple[int, str]:
    if (isinstance(dive_depth, bool) or not isinstance(dive_depth, Real)
            or dive_depth not in range(1, len(DIVE_DEPTHS) + 1)):
        raise ValueError('下潜深度必须为 Ⅰ 至 Ⅴ。')
    if not isinstance(opening_strategy, str) or opening_strategy not in OPENING_STRATEGIES:
        raise ValueError('开局策略必须为源流之策、盈缺之策、回生之策或捍卫之策。')
    return int(dive_depth), opening_strategy
