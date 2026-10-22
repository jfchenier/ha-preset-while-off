"""The backend must never build or send anything that changes OnOff."""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chip.clusters import Objects as clusters

from custom_components.preset_while_off import matter_backend
from custom_components.preset_while_off.matter_backend import (
    ALLOWED_COMMANDS,
    MatterBackend,
    MatterLight,
    build_move_to_color,
    build_move_to_color_temp,
    build_move_to_hue_saturation,
    build_move_to_level,
)

SOURCE_DIR = Path(matter_backend.__file__).parent


def test_builders_use_execute_if_off() -> None:
    level = build_move_to_level(64)
    assert type(level) is clusters.LevelControl.Commands.MoveToLevel
    assert (
        level.level,
        level.transitionTime,
        level.optionsMask,
        level.optionsOverride,
    ) == (64, 0, 1, 1)
    ct = build_move_to_color_temp(204)
    assert type(ct) is clusters.ColorControl.Commands.MoveToColorTemperature
    assert (ct.colorTemperatureMireds, ct.optionsMask, ct.optionsOverride) == (
        204,
        1,
        1,
    )

    xy = build_move_to_color(32768, 26214)
    assert type(xy) is clusters.ColorControl.Commands.MoveToColor
    assert (xy.colorX, xy.colorY, xy.transitionTime) == (32768, 26214, 0)
    assert (xy.optionsMask, xy.optionsOverride) == (1, 1)
    hs = build_move_to_hue_saturation(127, 254)
    assert type(hs) is clusters.ColorControl.Commands.MoveToHueAndSaturation
    assert (hs.hue, hs.saturation, hs.optionsMask, hs.optionsOverride) == (
        127,
        254,
        1,
        1,
    )


def test_allowed_commands_exclude_on_off() -> None:
    for command in ALLOWED_COMMANDS:
        assert command.cluster_id != clusters.OnOff.id
        assert "OnOff" not in command.__name__
        assert command.__name__ not in ("On", "Off", "Toggle")


def test_source_never_references_on_off_commands_or_options() -> None:
    forbidden = re.compile(
        r"WithOnOff|OnOff\.Commands|Commands\.(On|Off|Toggle)\b|"
        r"Attributes\.Options\b|SERVICE_TURN_OFF|toggle|"
        # ZHA / zigpy equivalents
        r"with_on_off|on_with_|write_attributes\(\{[^}]*\"options\""
    )
    for path in SOURCE_DIR.glob("*.py"):
        assert not forbidden.search(path.read_text()), path.name


@pytest.mark.parametrize(
    "command",
    [
        clusters.OnOff.Commands.On(),
        clusters.OnOff.Commands.Off(),
        clusters.OnOff.Commands.Toggle(),
        clusters.LevelControl.Commands.MoveToLevelWithOnOff(level=10),
        clusters.LevelControl.Commands.MoveToLevel(
            level=10, optionsMask=0, optionsOverride=0
        ),
    ],
)
async def test_send_refuses_other_commands(command) -> None:
    backend = MatterBackend(MagicMock())
    client = MagicMock(send_device_command=AsyncMock())
    with (
        patch.object(backend, "_client", return_value=client),
        pytest.raises(RuntimeError),
    ):
        await backend._send(MatterLight("light.x", 72, 1), command)
    client.send_device_command.assert_not_called()


async def test_send_allowed_command() -> None:
    backend = MatterBackend(MagicMock())
    client = MagicMock(send_device_command=AsyncMock())
    with patch.object(backend, "_client", return_value=client):
        await backend.move_to_level(MatterLight("light.x", 72, 1), 64)
    kwargs = client.send_device_command.call_args.kwargs
    assert kwargs["node_id"] == 72 and kwargs["endpoint_id"] == 1
    assert kwargs["command"] == build_move_to_level(64)


async def test_clear_on_level_writes_null() -> None:
    backend = MatterBackend(MagicMock())
    client = MagicMock(write_attribute=AsyncMock())
    with patch.object(backend, "_client", return_value=client):
        await backend.clear_on_level(MatterLight("light.x", 72, 1))
    client.write_attribute.assert_awaited_once_with(
        node_id=72, attribute_path="1/8/17", value=None
    )


async def test_resolve_parses_endpoint_from_unique_id(hass) -> None:
    """unique_id format seen on the owner's HA for branch lamp bulb 1 (node 72)."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    config_entry = MockConfigEntry(domain="matter")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={
            ("matter", "deviceid_B6F01A71A78EA9B0-0000000000000048-MatterNodeDevice")
        },
    )
    er.async_get(hass).async_get_or_create(
        "light",
        "matter",
        "B6F01A71A78EA9B0-0000000000000048-MatterNodeDevice-1-MatterLight-6-0",
        suggested_object_id="bulb_1",
        device_id=device.id,
    )
    endpoints = {i: MagicMock(endpoint_id=i) for i in (0, 1)}
    node = MagicMock(node_id=72, endpoints=endpoints)
    backend = MatterBackend(hass)
    with (
        patch.object(backend, "_client", return_value=MagicMock()),
        patch.object(matter_backend, "get_node_from_device_entry", return_value=node),
        patch.object(
            matter_backend,
            "get_device_id",
            return_value="B6F01A71A78EA9B0-0000000000000048-MatterNodeDevice",
        ),
    ):
        assert backend.resolve("light.bulb_1") == MatterLight("light.bulb_1", 72, 1)
        assert backend.resolve("light.unknown") is None


async def test_matter_read_failure_returns_none() -> None:
    """A failed live read must not fall back to possibly stale cached values."""
    backend = MatterBackend(MagicMock())
    client = MagicMock(read_attribute=AsyncMock(side_effect=TimeoutError))
    with patch.object(backend, "_client", return_value=client):
        assert await backend.read_attributes(MatterLight("light.x", 72, 1)) is None
