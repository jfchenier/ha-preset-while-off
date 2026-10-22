"""Diagnostics: per-light Matter state, capabilities and last results."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from . import PresetConfigEntry
from .models import BackendUnavailable
from .preset import expand_lights
from .zha_backend import ZhaLight


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PresetConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a rule."""
    coordinator = entry.runtime_data
    manager = coordinator.manager
    backend = manager.backend
    lights: dict[str, Any] = {}
    for entity_id in expand_lights(hass, coordinator.lights):
        info: dict[str, Any] = {
            "state": (state.state if (state := hass.states.get(entity_id)) else None),
            "capabilities": manager.capabilities.get(entity_id, {}),
            "last_result": manager.last_results.get(entity_id),
            "on_level_rewrites": coordinator.on_level_rewrites.get(entity_id, 0),
        }
        try:
            light = backend.resolve(entity_id)
            if light is None:
                info["device"] = None
            else:
                attrs = backend.get_attributes(light)
                info["device"] = {
                    "platform": "zha" if isinstance(light, ZhaLight) else "matter",
                    **{k: v for k, v in asdict(light).items() if k != "entity_id"},
                    "cached_attributes": attrs.as_dict() if attrs else None,
                }
        except BackendUnavailable as err:
            info["device"] = f"unavailable: {err}"
        lights[entity_id] = info
    return {
        "options": {**entry.data, **entry.options},
        "backends_available": {
            "matter": backend.matter.available,
            "zha": backend.zha.available,
        },
        "last_source": coordinator.last_source,
        "lights": lights,
    }
