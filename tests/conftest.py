"""Fixtures: a fake Matter backend so tests never touch a real Matter server."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from unittest.mock import patch

import pytest

from custom_components.preset_while_off.matter_backend import (
    LightAttributes,
    MatterLight,
)


@dataclass
class FakeLight:
    """A simulated Matter bulb."""

    attrs: LightAttributes
    honors_level_while_off: bool = True
    honors_ct_while_off: bool = True


def make_attrs(**kwargs) -> LightAttributes:
    """Return attributes of an off E12-like bulb after a switch dim-down."""
    base = LightAttributes(
        available=True,
        on_off=False,
        current_level=1,
        min_level=1,
        max_level=254,
        on_level=None,
        has_on_level=True,
        color_temp_mireds=370,
        ct_min_mireds=153,
        ct_max_mireds=454,
        supports_color_temp=True,
    )
    return replace(base, **kwargs)


@dataclass
class FakeBackend:
    """Records every command; simulates ExecuteIfOff support per light."""

    lights: dict[str, FakeLight] = field(default_factory=dict)
    commands: list[tuple[str, str, object]] = field(default_factory=list)
    available: bool = True
    on_level_callbacks: dict[str, Callable] = field(default_factory=dict)
    node_callbacks: list[Callable] = field(default_factory=list)

    def add(self, entity_id: str, node_id: int, **kwargs) -> FakeLight:
        light = FakeLight(
            make_attrs(**kwargs.pop("attrs", {})),
            **kwargs,
        )
        light.node_id = node_id  # type: ignore[attr-defined]
        self.lights[entity_id] = light
        return light

    def connection_id(self) -> int | None:
        return 1 if self.available else None

    def resolve(self, entity_id: str) -> MatterLight | None:
        if entity_id not in self.lights:
            return None
        return MatterLight(entity_id, self.lights[entity_id].node_id, 1)

    def get_attributes(self, light: MatterLight) -> LightAttributes | None:
        return replace(self.lights[light.entity_id].attrs)

    async def read_attributes(self, light: MatterLight) -> LightAttributes | None:
        return self.get_attributes(light)

    async def move_to_level(self, light: MatterLight, level: int) -> None:
        self.commands.append((light.entity_id, "MoveToLevel", level))
        fake = self.lights[light.entity_id]
        if fake.honors_level_while_off or fake.attrs.on_off:
            fake.attrs.current_level = level

    async def move_to_color_temp(self, light: MatterLight, mireds: int) -> None:
        self.commands.append((light.entity_id, "MoveToColorTemperature", mireds))
        fake = self.lights[light.entity_id]
        if fake.honors_ct_while_off or fake.attrs.on_off:
            fake.attrs.color_temp_mireds = mireds

    async def clear_on_level(self, light: MatterLight) -> None:
        self.commands.append((light.entity_id, "ClearOnLevel", None))
        self.lights[light.entity_id].attrs.on_level = None

    def subscribe_on_level(self, light: MatterLight, on_change: Callable) -> Callable:
        self.on_level_callbacks[light.entity_id] = on_change
        return lambda: self.on_level_callbacks.pop(light.entity_id, None)

    def subscribe_node_updated(self, on_update: Callable) -> Callable:
        self.node_callbacks.append(on_update)
        return lambda: self.node_callbacks.remove(on_update)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations."""
    return


@pytest.fixture(autouse=True)
def no_delays():
    """Remove command spacing and verify delays."""
    with (
        patch("custom_components.preset_while_off.preset.COMMAND_SPACING", 0),
        patch("custom_components.preset_while_off.preset.VERIFY_DELAY", 0),
    ):
        yield


@pytest.fixture(autouse=True)
def no_matter_setup():
    """Don't set up the real Matter component as a dependency."""
    with patch(
        "homeassistant.components.matter.async_setup", return_value=True, create=True
    ):
        yield


@pytest.fixture
def backend() -> FakeBackend:
    """Patch the integration to use a fake backend."""
    fake = FakeBackend()
    with patch("custom_components.preset_while_off.MatterBackend", return_value=fake):
        yield fake
