"""Preset While Off: set brightness and color of Matter lights while they are off."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_extract_entity_ids
from homeassistant.helpers.typing import ConfigType

from .backend import PresetBackend
from .const import (
    ATTR_APPLY_IF_ON,
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ATTR_XY_COLOR,
    DOMAIN,
    SERVICE_APPLY,
)
from .coordinator import RuleCoordinator
from .preset import (
    ColorPreset,
    PresetManager,
    brightness_to_level,
    expand_lights,
    pct_to_level,
)

SERVICE_RESET_CAPABILITIES = "reset_capabilities"

# The action works without any rule; `preset_while_off:` in YAML loads it alone.
CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)

APPLY_SCHEMA = vol.All(
    cv.make_entity_service_schema(
        {
            vol.Exclusive(ATTR_BRIGHTNESS_PCT, "brightness"): vol.All(
                vol.Coerce(float), vol.Range(min=1, max=100)
            ),
            vol.Exclusive(ATTR_BRIGHTNESS, "brightness"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=255)
            ),
            # One color setting at most: a color temperature or a full color.
            vol.Exclusive(ATTR_COLOR_TEMP_KELVIN, "color"): vol.All(
                vol.Coerce(int), vol.Range(min=1000, max=20000)
            ),
            vol.Exclusive(ATTR_RGB_COLOR, "color"): vol.All(
                vol.Coerce(tuple), vol.ExactSequence((cv.byte,) * 3)
            ),
            vol.Exclusive(ATTR_HS_COLOR, "color"): vol.All(
                vol.Coerce(tuple),
                vol.ExactSequence(
                    (
                        vol.All(vol.Coerce(float), vol.Range(min=0, max=360)),
                        vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                    )
                ),
            ),
            vol.Exclusive(ATTR_XY_COLOR, "color"): vol.All(
                vol.Coerce(tuple), vol.ExactSequence((cv.small_float,) * 2)
            ),
            vol.Optional(ATTR_APPLY_IF_ON, default=False): cv.boolean,
        }
    ),
    cv.has_at_least_one_key(
        ATTR_BRIGHTNESS_PCT,
        ATTR_BRIGHTNESS,
        ATTR_COLOR_TEMP_KELVIN,
        ATTR_RGB_COLOR,
        ATTR_HS_COLOR,
        ATTR_XY_COLOR,
    ),
)

# No target resets every light.
RESET_SCHEMA = vol.Schema(cv.ENTITY_SERVICE_FIELDS)

type PresetConfigEntry = ConfigEntry[RuleCoordinator]


def get_manager(hass: HomeAssistant) -> PresetManager:
    """Return the shared preset manager."""
    return hass.data[DOMAIN]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the shared manager and the actions."""
    manager = PresetManager(hass, PresetBackend(hass))
    await manager.async_load()
    hass.data[DOMAIN] = manager

    async def async_apply(call: ServiceCall) -> ServiceResponse:
        entity_ids = await async_extract_entity_ids(call)
        if not entity_ids:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_lights"
            )
        level = None
        if (pct := call.data.get(ATTR_BRIGHTNESS_PCT)) is not None:
            level = pct_to_level(pct)
        elif (brightness := call.data.get(ATTR_BRIGHTNESS)) is not None:
            level = brightness_to_level(brightness)
        color = None
        if (rgb := call.data.get(ATTR_RGB_COLOR)) is not None:
            color = ColorPreset.from_rgb(rgb)
        elif (hs := call.data.get(ATTR_HS_COLOR)) is not None:
            color = ColorPreset.from_hs(hs)
        elif (xy := call.data.get(ATTR_XY_COLOR)) is not None:
            color = ColorPreset.from_xy(xy)
        result = await manager.async_apply(
            sorted(entity_ids),
            level=level,
            color_temp_kelvin=call.data.get(ATTR_COLOR_TEMP_KELVIN),
            color=color,
            apply_if_on=call.data[ATTR_APPLY_IF_ON],
        )
        return result.as_dict()

    async def async_reset(call: ServiceCall) -> None:
        entity_ids = await async_extract_entity_ids(call)
        await manager.async_reset_capabilities(
            expand_lights(hass, entity_ids) if entity_ids else None
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY,
        async_apply,
        schema=APPLY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_CAPABILITIES, async_reset, schema=RESET_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PresetConfigEntry) -> bool:
    """Set up a "follow a source" rule."""
    coordinator = RuleCoordinator(hass, entry, get_manager(hass))
    entry.runtime_data = coordinator
    await coordinator.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PresetConfigEntry) -> bool:
    """Unload a rule."""
    await entry.runtime_data.async_stop()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: PresetConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
