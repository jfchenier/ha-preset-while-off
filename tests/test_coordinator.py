"""Follow-a-source rules: re-apply timing and OnLevel clearing."""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.preset_while_off.const import DOMAIN

from .conftest import FakeBackend

AL = "switch.adaptive_lighting_test"
BULBS = ["light.bulb_1", "light.bulb_2"]


@pytest.fixture
async def entry(hass: HomeAssistant, backend: FakeBackend) -> MockConfigEntry:
    for i, entity_id in enumerate(BULBS):
        backend.add(entity_id, 72 + i)
        hass.states.async_set(entity_id, "off")
    backend.lights["light.bulb_2"].attrs.on_level = 254
    hass.states.async_set("light.lamp", "off", {"entity_id": BULBS})
    hass.states.async_set(AL, "on", {"brightness_pct": 25.0, "color_temp_kelvin": 4890})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data={"source_type": "adaptive_lighting"},
        options={
            "lights": ["light.lamp"],
            "adaptive_lighting_switch": AL,
            "sync_brightness": True,
            "sync_color_temp": True,
            "clear_on_level": True,
            "interval_minutes": 10,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _fire(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))


async def test_startup_applies_and_clears_on_level(
    hass: HomeAssistant, entry: MockConfigEntry, backend: FakeBackend
) -> None:
    assert ("light.bulb_2", "ClearOnLevel", None) in backend.commands
    assert ("light.bulb_1", "ClearOnLevel", None) not in backend.commands
    assert ("light.bulb_1", "MoveToLevel", 64) in backend.commands
    assert ("light.bulb_2", "MoveToColorTemperature", 204) in backend.commands


async def test_turn_off_reapplies_after_delay(
    hass: HomeAssistant, entry: MockConfigEntry, backend: FakeBackend
) -> None:
    hass.states.async_set("light.bulb_1", "on")
    await hass.async_block_till_done()
    backend.commands.clear()
    hass.states.async_set("light.bulb_1", "off")
    # The switch dims the bulb down to 1 on turn-off.
    backend.lights["light.bulb_1"].attrs.current_level = 1
    await hass.async_block_till_done()
    assert backend.commands == []

    _fire(hass, 1.6)
    await hass.async_block_till_done()
    assert backend.commands == [
        ("light.bulb_1", "MoveToLevel", 64),
        ("light.bulb_1", "MoveToColorTemperature", 204),
    ]


async def test_source_change_is_debounced(
    hass: HomeAssistant, entry: MockConfigEntry, backend: FakeBackend
) -> None:
    backend.commands.clear()
    hass.states.async_set(AL, "on", {"brightness_pct": 40.0, "color_temp_kelvin": 4890})
    hass.states.async_set(AL, "on", {"brightness_pct": 50.0, "color_temp_kelvin": 4890})
    await hass.async_block_till_done()
    assert backend.commands == []

    _fire(hass, 2.5)
    await hass.async_block_till_done()
    # Color temp already matches, only the level changes.
    assert backend.commands == [
        ("light.bulb_1", "MoveToLevel", 127),
        ("light.bulb_2", "MoveToLevel", 127),
    ]


async def test_on_lights_untouched(
    hass: HomeAssistant, entry: MockConfigEntry, backend: FakeBackend
) -> None:
    hass.states.async_set("light.bulb_2", "on")
    backend.commands.clear()
    _fire(hass, 601)
    await hass.async_block_till_done()
    assert {c[0] for c in backend.commands} == {"light.bulb_1"}


async def test_on_level_rewrite_is_cleared(
    hass: HomeAssistant, entry: MockConfigEntry, backend: FakeBackend
) -> None:
    backend.commands.clear()
    backend.lights["light.bulb_1"].attrs.on_level = 64
    backend.on_level_callbacks["light.bulb_1"](64)
    await hass.async_block_till_done()
    assert backend.commands == [("light.bulb_1", "ClearOnLevel", None)]
    assert entry.runtime_data.on_level_rewrites == {"light.bulb_1": 1}


async def test_unload(
    hass: HomeAssistant, entry: MockConfigEntry, backend: FakeBackend
) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    backend.commands.clear()
    hass.states.async_set("light.bulb_1", "on")
    hass.states.async_set("light.bulb_1", "off")
    _fire(hass, 700)
    await hass.async_block_till_done()
    assert backend.commands == []
    assert backend.on_level_callbacks == {}


async def test_diagnostics(
    hass: HomeAssistant, entry: MockConfigEntry, backend: FakeBackend
) -> None:
    from custom_components.preset_while_off.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)
    bulb = diag["lights"]["light.bulb_1"]
    assert bulb["device"]["platform"] == "matter"
    assert bulb["device"]["node_id"] == 72
    assert bulb["device"]["cached_attributes"]["current_level"] == 64
    assert bulb["capabilities"] == {"level": "supported", "color_temp": "supported"}
    assert diag["last_source"] == {"brightness_pct": 25.0, "color_temp_kelvin": 4890.0}
