"""Config and options flow for "follow a source" rules."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AL_SWITCH,
    CONF_BRIGHTNESS_ENTITY,
    CONF_CLEAR_ON_LEVEL,
    CONF_COLOR_TEMP_ENTITY,
    CONF_INTERVAL,
    CONF_LIGHTS,
    CONF_SOURCE_TYPE,
    CONF_SYNC_BRIGHTNESS,
    CONF_SYNC_COLOR_TEMP,
    DEFAULT_INTERVAL_MINUTES,
    DOMAIN,
    SOURCE_ADAPTIVE_LIGHTING,
    SOURCE_ENTITIES,
)

LIGHTS_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="light", multiple=True)
)
AL_SWITCH_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="switch", integration="adaptive_lighting")
)
VALUE_ENTITY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=["sensor", "input_number", "number"])
)
SOURCE_TYPE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[SOURCE_ADAPTIVE_LIGHTING, SOURCE_ENTITIES],
        translation_key=CONF_SOURCE_TYPE,
        mode=selector.SelectSelectorMode.LIST,
    )
)
INTERVAL_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=1,
        max=120,
        step=1,
        unit_of_measurement="min",
        mode=selector.NumberSelectorMode.BOX,
    )
)


def _source_schema(source_type: str, values: dict[str, Any]) -> dict[Any, Any]:
    if source_type == SOURCE_ADAPTIVE_LIGHTING:
        return {
            vol.Required(
                CONF_AL_SWITCH, default=values.get(CONF_AL_SWITCH, vol.UNDEFINED)
            ): AL_SWITCH_SELECTOR
        }
    return {
        vol.Optional(
            CONF_BRIGHTNESS_ENTITY,
            description={"suggested_value": values.get(CONF_BRIGHTNESS_ENTITY)},
        ): VALUE_ENTITY_SELECTOR,
        vol.Optional(
            CONF_COLOR_TEMP_ENTITY,
            description={"suggested_value": values.get(CONF_COLOR_TEMP_ENTITY)},
        ): VALUE_ENTITY_SELECTOR,
    }


def _settings_schema(values: dict[str, Any]) -> dict[Any, Any]:
    return {
        vol.Required(
            CONF_SYNC_BRIGHTNESS, default=values.get(CONF_SYNC_BRIGHTNESS, True)
        ): bool,
        vol.Required(
            CONF_SYNC_COLOR_TEMP, default=values.get(CONF_SYNC_COLOR_TEMP, True)
        ): bool,
        vol.Required(
            CONF_CLEAR_ON_LEVEL, default=values.get(CONF_CLEAR_ON_LEVEL, True)
        ): bool,
        vol.Required(
            CONF_INTERVAL, default=values.get(CONF_INTERVAL, DEFAULT_INTERVAL_MINUTES)
        ): INTERVAL_SELECTOR,
    }


def _validate(source_type: str, values: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if CONF_LIGHTS in values and not values[CONF_LIGHTS]:
        errors[CONF_LIGHTS] = "no_lights"
    if source_type == SOURCE_ENTITIES and not (
        values.get(CONF_BRIGHTNESS_ENTITY) or values.get(CONF_COLOR_TEMP_ENTITY)
    ):
        errors["base"] = "no_source_entity"
    if CONF_SYNC_BRIGHTNESS in values and not (
        values[CONF_SYNC_BRIGHTNESS] or values[CONF_SYNC_COLOR_TEMP]
    ):
        errors["base"] = "nothing_to_sync"
    return errors


def _normalize(values: dict[str, Any]) -> dict[str, Any]:
    if CONF_INTERVAL in values:
        values[CONF_INTERVAL] = int(values[CONF_INTERVAL])
    return values


class PresetWhileOffConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one entry per rule."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return PresetWhileOffOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name, lights and source type."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_LIGHTS]:
                errors[CONF_LIGHTS] = "no_lights"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_source()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): str,
                    vol.Required(CONF_LIGHTS): LIGHTS_SELECTOR,
                    vol.Required(
                        CONF_SOURCE_TYPE, default=SOURCE_ADAPTIVE_LIGHTING
                    ): SOURCE_TYPE_SELECTOR,
                }
            ),
            errors=errors,
        )

    async def async_step_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the source entity or entities."""
        source_type = self._data[CONF_SOURCE_TYPE]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(source_type, user_input)
            if not errors:
                self._data.update(user_input)
                return await self.async_step_settings()
        return self.async_show_form(
            step_id=f"source_{source_type}",
            data_schema=vol.Schema(_source_schema(source_type, user_input or {})),
            errors=errors,
        )

    async_step_source_adaptive_lighting = async_step_source
    async_step_source_entities = async_step_source

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Toggles and interval."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self._data[CONF_SOURCE_TYPE], user_input)
            if not errors:
                options = _normalize({**self._data, **user_input})
                name = options.pop(CONF_NAME)
                source_type = options.pop(CONF_SOURCE_TYPE)
                return self.async_create_entry(
                    title=name, data={CONF_SOURCE_TYPE: source_type}, options=options
                )
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(_settings_schema(user_input or {})),
            errors=errors,
        )


class PresetWhileOffOptionsFlow(OptionsFlow):
    """Edit a rule."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit lights, source and settings in one form."""
        source_type = self.config_entry.data[CONF_SOURCE_TYPE]
        errors: dict[str, str] = {}
        current = dict(self.config_entry.options)
        if user_input is not None:
            errors = _validate(source_type, user_input)
            if not errors:
                return self.async_create_entry(data=_normalize(dict(user_input)))
            current.update(user_input)
        schema = {
            vol.Required(
                CONF_LIGHTS, default=current.get(CONF_LIGHTS, [])
            ): LIGHTS_SELECTOR,
            **_source_schema(source_type, current),
            **_settings_schema(current),
        }
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema), errors=errors
        )
