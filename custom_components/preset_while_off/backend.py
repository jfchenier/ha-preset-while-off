"""Dispatch light operations to the Matter or ZHA backend."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .matter_backend import MatterBackend, MatterLight
from .models import BackendUnavailable, LightAttributes
from .zha_backend import ZhaBackend, ZhaLight

type Light = MatterLight | ZhaLight


class PresetBackend:
    """One backend per supported platform, chosen per light."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        self.matter = MatterBackend(hass)
        self.zha = ZhaBackend(hass)

    def _for(self, light: Light) -> Any:
        return self.zha if isinstance(light, ZhaLight) else self.matter

    @property
    def available(self) -> bool:
        """Return True if any backend is ready."""
        return self.matter.available or self.zha.available

    @callback
    def connection_id(self) -> tuple[int | None, int | None] | None:
        """Return an id that changes when either backend's connection changes."""
        ids = (self.matter.connection_id(), self.zha.connection_id())
        return None if ids == (None, None) else ids

    @callback
    def resolve(self, entity_id: str) -> Light | None:
        """Map a light entity to a Matter or Zigbee light."""
        for backend in (self.matter, self.zha):
            try:
                if light := backend.resolve(entity_id):
                    return light
            except BackendUnavailable:
                continue
        return None

    @callback
    def get_attributes(self, light: Light) -> LightAttributes | None:
        """Return cached attributes."""
        return self._for(light).get_attributes(light)

    async def read_attributes(self, light: Light) -> LightAttributes | None:
        """Read attributes live."""
        return await self._for(light).read_attributes(light)

    async def move_to_level(self, light: Light, level: int) -> None:
        """Set the level while off."""
        await self._for(light).move_to_level(light, level)

    async def move_to_color_temp(self, light: Light, mireds: int) -> None:
        """Set the color temperature while off."""
        await self._for(light).move_to_color_temp(light, mireds)

    async def move_to_color_xy(self, light: Light, x: int, y: int) -> None:
        """Set an xy color while off."""
        await self._for(light).move_to_color_xy(light, x, y)

    async def move_to_hue_saturation(
        self, light: Light, hue: int, saturation: int
    ) -> None:
        """Set a hue/saturation color while off."""
        await self._for(light).move_to_hue_saturation(light, hue, saturation)

    async def clear_on_level(self, light: Light) -> None:
        """Write OnLevel = null."""
        await self._for(light).clear_on_level(light)

    @callback
    def subscribe_on_level(
        self, light: Light, on_change: Callable[[int | None], None]
    ) -> Callable[[], None]:
        """Call `on_change` when the light reports a new OnLevel."""
        return self._for(light).subscribe_on_level(light, on_change)

    @callback
    def subscribe_node_updated(
        self, on_update: Callable[[int], None]
    ) -> Callable[[], None]:
        """Subscribe to Matter node updates (re-interviews)."""
        try:
            return self.matter.subscribe_node_updated(on_update)
        except BackendUnavailable:
            return lambda: None
