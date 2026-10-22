"""Clamping and unit conversion."""

import pytest

from custom_components.preset_while_off.preset import (
    brightness_to_level,
    clamp_level,
    kelvin_to_mireds,
    pct_to_level,
)


@pytest.mark.parametrize(
    ("pct", "level"), [(25, 64), (50, 127), (100, 254), (1, 3), (99.9472, 254)]
)
def test_pct_to_level(pct: float, level: int) -> None:
    assert pct_to_level(pct) == level


def test_level_never_255() -> None:
    assert max(pct_to_level(p / 10) for p in range(1001)) == 254
    assert brightness_to_level(255) == 254
    assert clamp_level(255, None, None) == 254
    assert clamp_level(255, 1, 255) == 254


def test_clamp_level_min_max() -> None:
    assert clamp_level(0, None, None) == 1
    assert clamp_level(0, 0, 254) == 1
    assert clamp_level(5, 10, 200) == 10
    assert clamp_level(220, 10, 200) == 200


def test_kelvin_to_mireds() -> None:
    assert kelvin_to_mireds(4890) == 204
    assert kelvin_to_mireds(4890, 153, 454) == 204
    assert kelvin_to_mireds(10000, 153, 454) == 153
    assert kelvin_to_mireds(1800, 153, 454) == 454
    assert kelvin_to_mireds(2000, None, None) == 500
