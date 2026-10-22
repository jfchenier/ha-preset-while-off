"""Config and options flow."""

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.preset_while_off.const import DOMAIN

from .conftest import FakeBackend


async def test_flow_adaptive_lighting(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Living",
            "lights": ["light.lamp"],
            "source_type": "adaptive_lighting",
        },
    )
    assert result["step_id"] == "source_adaptive_lighting"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"adaptive_lighting_switch": "switch.al"}
    )
    assert result["step_id"] == "settings"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "sync_brightness": False,
            "sync_color_temp": False,
            "clear_on_level": True,
            "interval_minutes": 10,
        },
    )
    assert result["errors"] == {"base": "nothing_to_sync"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "sync_brightness": True,
            "sync_color_temp": True,
            "clear_on_level": True,
            "interval_minutes": 5,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living"
    assert result["data"] == {"source_type": "adaptive_lighting"}
    assert result["options"] == {
        "lights": ["light.lamp"],
        "adaptive_lighting_switch": "switch.al",
        "sync_brightness": True,
        "sync_color_temp": True,
        "clear_on_level": True,
        "interval_minutes": 5,
    }


async def test_flow_entities_needs_one(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Kitchen", "lights": ["light.k"], "source_type": "entities"},
    )
    assert result["step_id"] == "source_entities"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "no_source_entity"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"brightness_entity": "input_number.b"}
    )
    assert result["step_id"] == "settings"


async def test_options_flow(hass: HomeAssistant, backend: FakeBackend) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living",
        data={"source_type": "adaptive_lighting"},
        options={"lights": ["light.lamp"], "adaptive_lighting_switch": "switch.al"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "lights": ["light.lamp", "light.other"],
            "adaptive_lighting_switch": "switch.al",
            "sync_brightness": True,
            "sync_color_temp": False,
            "clear_on_level": True,
            "interval_minutes": 15,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["sync_color_temp"] is False
    assert entry.runtime_data.lights == ["light.lamp", "light.other"]
