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


@pytest.fixture
async def color_setup(hass: HomeAssistant, backend: FakeBackend) -> FakeBackend:
    backend.add("light.xy", 1, attrs={"supports_xy": True, "supports_hs": True})
    backend.add("light.hs", 2, attrs={"supports_hs": True})
    backend.add("light.ws", 3)
    for entity_id in ("light.xy", "light.hs", "light.ws"):
        hass.states.async_set(entity_id, "off")
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    return backend


async def test_apply_xy_color(hass: HomeAssistant, color_setup: FakeBackend) -> None:
    response = await _apply(
        hass,
        entity_id=["light.xy", "light.hs", "light.ws"],
        brightness_pct=25,
        xy_color=[0.5, 0.4],
    )
    assert response["applied"] == ["light.hs", "light.ws", "light.xy"]
    hs_command = color_setup.commands[1]
    assert color_setup.commands == [
        ("light.hs", "MoveToLevel", 64),
        # Hue/saturation only: converted from the same color.
        ("light.hs", "MoveToHueAndSaturation", hs_command[2]),
        # No color support: brightness only.
        ("light.ws", "MoveToLevel", 64),
        ("light.xy", "MoveToLevel", 64),
        # xy preferred when supported.
        ("light.xy", "MoveToColor", (32768, 26214)),
    ]
    caps = hass.data[DOMAIN].capabilities
    assert caps["light.xy"]["color"] == "supported"
    assert caps["light.hs"]["color"] == "supported"
    assert "color" not in caps["light.ws"]


async def test_apply_rgb_color(hass: HomeAssistant, color_setup: FakeBackend) -> None:
    await _apply(hass, entity_id="light.xy", rgb_color=[255, 0, 0])
    ((entity_id, command, (x, y)),) = color_setup.commands
    assert (entity_id, command) == ("light.xy", "MoveToColor")
    assert x > 0.6 * 65536 and y < 0.35 * 65536


async def test_apply_hs_color(hass: HomeAssistant, color_setup: FakeBackend) -> None:
    await _apply(hass, entity_id="light.hs", hs_color=[180, 50])
    assert color_setup.commands == [("light.hs", "MoveToHueAndSaturation", (127, 127))]


async def test_color_only_unsupported_light(
    hass: HomeAssistant, color_setup: FakeBackend
) -> None:
    response = await _apply(hass, entity_id="light.ws", rgb_color=[255, 0, 0])
    assert response["unsupported"] == {"light.ws": "no_supported_parts"}
    assert color_setup.commands == []


async def test_color_and_kelvin_exclusive(
    hass: HomeAssistant, color_setup: FakeBackend
) -> None:
    with pytest.raises(vol.Invalid):
        await _apply(
            hass, entity_id="light.xy", rgb_color=[255, 0, 0], color_temp_kelvin=3000
        )


async def test_color_gamut_clamp_counts_as_supported(
    hass: HomeAssistant, color_setup: FakeBackend
) -> None:
    color_setup.lights["light.xy"].gamut_clamp = lambda x, y: (x - 500, y + 300)
    await _apply(hass, entity_id="light.xy", xy_color=[0.5, 0.4])
    assert hass.data[DOMAIN].capabilities["light.xy"]["color"] == "supported"


async def test_color_ignored_while_off(
    hass: HomeAssistant, color_setup: FakeBackend
) -> None:
    color_setup.lights["light.xy"].honors_color_while_off = False
    await _apply(hass, entity_id="light.xy", brightness_pct=50, rgb_color=[0, 0, 255])
    assert hass.data[DOMAIN].capabilities["light.xy"] == {
        "level": "supported",
        "color": "unsupported",
    }
    color_setup.commands.clear()
    await _apply(hass, entity_id="light.xy", brightness_pct=60, rgb_color=[0, 0, 255])
    assert [c[1] for c in color_setup.commands] == ["MoveToLevel"]


async def test_apply_if_on_passes_color(
    hass: HomeAssistant, color_setup: FakeBackend
) -> None:
    calls = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("light.xy", "on")
    response = await _apply(
        hass, entity_id="light.xy", rgb_color=[255, 100, 0], apply_if_on=True
    )
    assert response["applied_on"] == ["light.xy"]
    assert calls[0].data == {"entity_id": ["light.xy"], "rgb_color": (255, 100, 0)}
    assert color_setup.commands == []
