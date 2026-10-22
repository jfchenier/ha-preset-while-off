"""The preset_while_off.apply action."""

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.preset_while_off.const import DOMAIN, ISSUE_UNSUPPORTED

from .conftest import FakeBackend

BULBS = [f"light.bulb_{i}" for i in range(1, 4)]


@pytest.fixture
async def setup(hass: HomeAssistant, backend: FakeBackend) -> FakeBackend:
    for i, entity_id in enumerate(BULBS):
        backend.add(entity_id, 72 + i)
        hass.states.async_set(entity_id, "off")
    hass.states.async_set("light.lamp", "off", {"entity_id": BULBS[:2]})
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    return backend


async def _apply(hass: HomeAssistant, **data):
    return await hass.services.async_call(
        DOMAIN, "apply", data, blocking=True, return_response=True
    )


async def test_apply_expands_group(hass: HomeAssistant, setup: FakeBackend) -> None:
    response = await _apply(
        hass, entity_id="light.lamp", brightness_pct=25, color_temp_kelvin=4890
    )
    assert response["applied"] == BULBS[:2]
    assert setup.commands == [
        ("light.bulb_1", "MoveToLevel", 64),
        ("light.bulb_1", "MoveToColorTemperature", 204),
        ("light.bulb_2", "MoveToLevel", 64),
        ("light.bulb_2", "MoveToColorTemperature", 204),
    ]
    assert setup.lights["light.bulb_1"].attrs.on_off is False


async def test_apply_brightness_alias(hass: HomeAssistant, setup: FakeBackend) -> None:
    await _apply(hass, entity_id="light.bulb_1", brightness=255)
    assert setup.commands == [("light.bulb_1", "MoveToLevel", 254)]


async def test_requires_a_value(hass: HomeAssistant, setup: FakeBackend) -> None:
    with pytest.raises(vol.Invalid):
        await _apply(hass, entity_id="light.bulb_1")


async def test_on_unavailable_and_non_matter(
    hass: HomeAssistant, setup: FakeBackend
) -> None:
    hass.states.async_set("light.bulb_2", "on")
    hass.states.async_set("light.bulb_3", "unavailable")
    hass.states.async_set("light.hue", "off")
    response = await _apply(hass, entity_id=[*BULBS, "light.hue"], brightness_pct=50)
    assert response["applied"] == ["light.bulb_1"]
    assert response["skipped"] == {"light.bulb_2": "on", "light.bulb_3": "unavailable"}
    assert response["unsupported"] == {"light.hue": "not_matter"}
    assert [c[0] for c in setup.commands] == ["light.bulb_1"]


async def test_apply_if_on(hass: HomeAssistant, setup: FakeBackend) -> None:
    calls = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("light.bulb_2", "on")
    response = await _apply(
        hass,
        entity_id=BULBS[:2],
        brightness_pct=25,
        color_temp_kelvin=4890,
        apply_if_on=True,
    )
    assert response["applied_on"] == ["light.bulb_2"]
    assert calls[0].data == {
        "entity_id": ["light.bulb_2"],
        "brightness_pct": 25.2,
        "color_temp_kelvin": 4890,
    }
    assert [c[0] for c in setup.commands] == ["light.bulb_1", "light.bulb_1"]


async def test_no_color_temp_without_capability(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    backend.add("light.dimmer", 1, attrs={"supports_color_temp": False})
    hass.states.async_set("light.dimmer", "off")
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    response = await _apply(hass, entity_id="light.dimmer", color_temp_kelvin=3000)
    assert response["unsupported"] == {"light.dimmer": "no_supported_parts"}
    assert backend.commands == []


async def test_capability_detection(hass: HomeAssistant, setup: FakeBackend) -> None:
    setup.lights["light.bulb_2"].honors_level_while_off = False
    await _apply(hass, entity_id=BULBS[:2], brightness_pct=25, color_temp_kelvin=4890)

    manager = hass.data[DOMAIN]
    assert manager.capabilities["light.bulb_1"] == {
        "level": "supported",
        "color_temp": "supported",
    }
    assert manager.capabilities["light.bulb_2"] == {
        "level": "unsupported",
        "color_temp": "supported",
    }
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNSUPPORTED)
    assert issue is not None
    assert "light.bulb_2: level" in issue.translation_placeholders["lights"]

    setup.commands.clear()
    await _apply(
        hass, entity_id="light.bulb_2", brightness_pct=50, color_temp_kelvin=4000
    )
    assert setup.commands == [("light.bulb_2", "MoveToColorTemperature", 250)]

    await hass.services.async_call(
        DOMAIN, "reset_capabilities", {"entity_id": "light.bulb_2"}, blocking=True
    )
    assert "light.bulb_2" not in manager.capabilities
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNSUPPORTED) is None


async def test_unknown_when_value_already_matches(
    hass: HomeAssistant, setup: FakeBackend
) -> None:
    setup.lights["light.bulb_1"].attrs.current_level = 64
    await _apply(hass, entity_id="light.bulb_1", brightness_pct=25)
    assert "level" not in hass.data[DOMAIN].capabilities.get("light.bulb_1", {})
