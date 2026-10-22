"""ZHA backend: entity mapping, attribute cache, commands and invariants."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.preset_while_off.backend import PresetBackend
from custom_components.preset_while_off.matter_backend import MatterLight
from custom_components.preset_while_off.preset import expand_lights
from custom_components.preset_while_off.zha_backend import (
    COLOR_CLUSTER,
    LEVEL_CLUSTER,
    ON_OFF_CLUSTER,
    ZhaBackend,
    ZhaLight,
    ZhaUnavailable,
)

IEEE = "70:ac:08:ff:fe:c3:4e:c6"


class FakeCluster:
    """Mimics the zigpy cluster API the backend uses."""

    def __init__(self, cluster_id: int, attrs: dict[str, Any], status: int = 0) -> None:
        self.cluster_id = cluster_id
        self.attrs = attrs
        self.status = status
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, name: str) -> Any:
        return self.attrs.get(name)

    async def read_attributes(self, names: list[str], allow_cache: bool = False):
        self.calls.append(
            ("read_attributes", {"names": names, "allow_cache": allow_cache})
        )
        return {n: self.attrs.get(n) for n in names}, {}

    async def write_attributes(self, values: dict[str, Any]):
        self.calls.append(("write_attributes", values))
        self.attrs.update(values)

    def __getattr__(self, name: str):
        async def command(**kwargs: Any):
            self.calls.append((name, kwargs))
            return [0, self.status]

        return command


def make_zigbee(
    color_attrs: dict[str, Any] | None = None, status: int = 0
) -> tuple[dict[int, FakeCluster], SimpleNamespace]:
    clusters = {
        ON_OFF_CLUSTER: FakeCluster(ON_OFF_CLUSTER, {"on_off": 0}),
        LEVEL_CLUSTER: FakeCluster(
            LEVEL_CLUSTER,
            {"current_level": 1, "min_level": 1, "max_level": 254, "on_level": 0xFF},
            status,
        ),
        COLOR_CLUSTER: FakeCluster(
            COLOR_CLUSTER,
            {
                "color_temperature": 370,
                "color_temp_physical_min": 250,
                "color_temp_physical_max": 454,
                **(color_attrs or {}),
            },
            status,
        ),
    }
    endpoint = SimpleNamespace(in_clusters=clusters)
    zigpy_device = SimpleNamespace(endpoints={1: endpoint})
    zha_device = SimpleNamespace(device=zigpy_device, available=True)
    gateway = SimpleNamespace(device_proxies={IEEE: SimpleNamespace(device=zha_device)})
    return clusters, gateway


@pytest.fixture
def zigbee(hass: HomeAssistant):
    clusters, gateway = make_zigbee()
    backend = ZhaBackend(hass)
    with patch.object(backend, "_gateway", return_value=gateway):
        yield backend, clusters


LIGHT = ZhaLight("light.stairs_1", IEEE, 1)


async def test_resolve(hass: HomeAssistant, zigbee) -> None:
    backend, _ = zigbee
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "light", "zha", f"{IEEE}-1", suggested_object_id="stairs_1"
    )
    registry.async_get_or_create(
        "light", "zha", f"{IEEE}-2", suggested_object_id="no_endpoint"
    )
    registry.async_get_or_create(
        "light", "zha", "light_zha_group_0x0004", suggested_object_id="stairs_group"
    )
    registry.async_get_or_create("light", "hue", "abc-1", suggested_object_id="hue")
    assert backend.resolve("light.stairs_1") == LIGHT
    assert backend.resolve("light.no_endpoint") is None
    assert backend.resolve("light.stairs_group") is None
    assert backend.resolve("light.hue") is None


async def test_get_attributes_white_bulb(zigbee) -> None:
    backend, _ = zigbee
    attrs = backend.get_attributes(LIGHT)
    assert attrs.on_off is False
    assert attrs.current_level == 1
    # on_level 0xFF is null, but the attribute exists.
    assert attrs.on_level is None and attrs.has_on_level is True
    assert (attrs.ct_min_mireds, attrs.ct_max_mireds) == (250, 454)
    # No ColorCapabilities: assume color temp, never full color.
    assert attrs.supports_color_temp is True
    assert attrs.supports_xy is attrs.supports_hs is False


async def test_get_attributes_color_capabilities(hass: HomeAssistant) -> None:
    _, gateway = make_zigbee(
        {"color_capabilities": 0x19, "color_temp_physical_max": 0xFFFF}
    )
    backend = ZhaBackend(hass)
    with patch.object(backend, "_gateway", return_value=gateway):
        attrs = backend.get_attributes(LIGHT)
    assert attrs.supports_color_temp and attrs.supports_xy and attrs.supports_hs
    assert attrs.ct_max_mireds is None


@pytest.mark.parametrize(
    ("method", "args", "cluster", "command", "kwargs"),
    [
        ("move_to_level", (64,), LEVEL_CLUSTER, "move_to_level", {"level": 64}),
        (
            "move_to_color_temp",
            (204,),
            COLOR_CLUSTER,
            "move_to_color_temp",
            {"color_temp_mireds": 204},
        ),
        (
            "move_to_color_xy",
            (32768, 26214),
            COLOR_CLUSTER,
            "move_to_color",
            {"color_x": 32768, "color_y": 26214},
        ),
        (
            "move_to_hue_saturation",
            (127, 254),
            COLOR_CLUSTER,
            "move_to_hue_and_saturation",
            {"hue": 127, "saturation": 254},
        ),
    ],
)
async def test_commands_use_execute_if_off(
    zigbee, method, args, cluster, command, kwargs
) -> None:
    backend, clusters = zigbee
    await getattr(backend, method)(LIGHT, *args)
    assert clusters[cluster].calls == [
        (
            command,
            {**kwargs, "transition_time": 0, "options_mask": 1, "options_override": 1},
        )
    ]
    assert clusters[ON_OFF_CLUSTER].calls == []


@pytest.mark.parametrize(
    ("cluster", "command"),
    [
        (ON_OFF_CLUSTER, "on"),
        (ON_OFF_CLUSTER, "off"),
        (ON_OFF_CLUSTER, "toggle"),
        (LEVEL_CLUSTER, "move_to_level_with_on_off"),
        (COLOR_CLUSTER, "move_to_color_temp_with_on_off"),
    ],
)
async def test_send_refuses_other_commands(zigbee, cluster, command) -> None:
    backend, clusters = zigbee
    with pytest.raises(RuntimeError):
        await backend._send(LIGHT, cluster, command)
    assert all(c.calls == [] for c in clusters.values())


async def test_failed_status_raises(hass: HomeAssistant) -> None:
    _, gateway = make_zigbee(status=0x81)  # UNSUP_CLUSTER_COMMAND
    backend = ZhaBackend(hass)
    with (
        patch.object(backend, "_gateway", return_value=gateway),
        pytest.raises(RuntimeError, match="status"),
    ):
        await backend.move_to_level(LIGHT, 64)


async def test_clear_on_level_writes_null(zigbee) -> None:
    backend, clusters = zigbee
    clusters[LEVEL_CLUSTER].attrs["on_level"] = 128
    assert backend.get_attributes(LIGHT).on_level == 128
    await backend.clear_on_level(LIGHT)
    assert clusters[LEVEL_CLUSTER].calls == [("write_attributes", {"on_level": 0xFF})]
    assert backend.get_attributes(LIGHT).on_level is None


async def test_read_attributes_is_live(zigbee) -> None:
    backend, clusters = zigbee
    await backend.read_attributes(LIGHT)
    for cluster in clusters.values():
        ((name, kwargs),) = cluster.calls
        assert name == "read_attributes" and kwargs["allow_cache"] is False


async def test_unavailable_without_zha(hass: HomeAssistant) -> None:
    backend = ZhaBackend(hass)
    assert backend.available is False
    with pytest.raises(ZhaUnavailable):
        backend._gateway()


async def test_preset_backend_dispatch(hass: HomeAssistant) -> None:
    backend = PresetBackend(hass)
    zha_light = ZhaLight("light.z", IEEE, 1)
    matter_light = MatterLight("light.m", 72, 1)
    with (
        patch.object(backend.zha, "move_to_level") as zha_move,
        patch.object(backend.matter, "move_to_level") as matter_move,
    ):
        await backend.move_to_level(zha_light, 10)
        await backend.move_to_level(matter_light, 20)
    zha_move.assert_awaited_once_with(zha_light, 10)
    matter_move.assert_awaited_once_with(matter_light, 20)

    with (
        patch.object(backend.matter, "resolve", return_value=None),
        patch.object(backend.zha, "resolve", return_value=zha_light),
    ):
        assert backend.resolve("light.z") is zha_light


async def test_expand_zha_group(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "light.stairs",
        "off",
        {"group_entities": ["light.stairs_1", "light.stairs_2"]},
    )
    assert expand_lights(hass, ["light.stairs"]) == ["light.stairs_1", "light.stairs_2"]


async def test_read_failure_returns_none(zigbee) -> None:
    """A failed read must not be mistaken for an ignored command."""
    backend, clusters = zigbee

    async def fail(*args, **kwargs):
        raise TimeoutError

    clusters[COLOR_CLUSTER].read_attributes = fail
    assert await backend.read_attributes(LIGHT) is None
