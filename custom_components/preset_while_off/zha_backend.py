"""The only module that touches HA's ZHA integration and zigpy.

Everything here uses internals of `homeassistant.components.zha` and zigpy
cluster objects. They can change between HA releases, so keep all of that
access in this one file. ZHA is imported lazily so installs without ZHA work.

Safety invariant (same as the Matter backend): this module can only send
`move_to_level`, `move_to_color_temp`, `move_to_color` (xy) and
`move_to_hue_and_saturation`, always with ExecuteIfOff in the per-command
`options_mask` / `options_override`. It never sends anything that changes
OnOff, and never writes the persistent `options` attributes. The only attribute
it writes is `on_level`, and only to null (0xFF).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import LOGGER
from .models import BackendUnavailable, LightAttributes

ZHA_DOMAIN = "zha"

ON_OFF_CLUSTER = 0x0006
LEVEL_CLUSTER = 0x0008
COLOR_CLUSTER = 0x0300

# ExecuteIfOff is bit 0 of the Level Control and Color Control options bitmaps.
EXECUTE_IF_OFF = 1
# ColorCapabilities bits.
COLOR_CAP_HUE_SATURATION = 0x01
COLOR_CAP_XY = 0x08
COLOR_CAP_COLOR_TEMPERATURE = 0x10
# on_level is a nullable uint8: 0xFF means null.
ON_LEVEL_NULL = 0xFF

# (cluster id, zigpy command name) pairs this backend may send.
ALLOWED_COMMANDS: frozenset[tuple[int, str]] = frozenset(
    {
        (LEVEL_CLUSTER, "move_to_level"),
        (COLOR_CLUSTER, "move_to_color_temp"),
        (COLOR_CLUSTER, "move_to_color"),
        (COLOR_CLUSTER, "move_to_hue_and_saturation"),
    }
)

# Attributes read back live after sending, for capability detection.
_VERIFY_ATTRIBUTES: dict[int, list[str]] = {
    ON_OFF_CLUSTER: ["on_off"],
    LEVEL_CLUSTER: ["current_level"],
    COLOR_CLUSTER: [
        "color_temperature",
        "current_x",
        "current_y",
        "current_hue",
        "current_saturation",
    ],
}


class ZhaUnavailable(BackendUnavailable):
    """Raised when the ZHA integration is not ready."""


@dataclass(frozen=True, slots=True)
class ZhaLight:
    """A light entity mapped to a Zigbee device endpoint."""

    entity_id: str
    ieee: str
    endpoint_id: int


def _mireds_or_none(value: Any) -> int | None:
    # 0 and 0xFFFF mean "not set" for the physical color temp limits.
    if value in (None, 0, 0xFFFF):
        return None
    return int(value)


class ZhaBackend:
    """Access to Zigbee lights through HA's own ZHA gateway."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the backend."""
        self.hass = hass

    def _gateway(self) -> Any:
        try:
            from homeassistant.components.zha.helpers import (  # noqa: PLC0415
                get_zha_gateway_proxy,
            )

            return get_zha_gateway_proxy(self.hass)
        except (ImportError, ValueError, KeyError, AttributeError) as err:
            raise ZhaUnavailable("ZHA integration is not loaded") from err

    @property
    def available(self) -> bool:
        """Return True if the ZHA gateway is running."""
        return self.connection_id() is not None

    @callback
    def connection_id(self) -> int | None:
        """Return an id that changes when the ZHA gateway is replaced."""
        try:
            return id(self._gateway())
        except ZhaUnavailable:
            return None

    @callback
    def resolve(self, entity_id: str) -> ZhaLight | None:
        """Map a light entity to its Zigbee device endpoint.

        Returns None for non-ZHA lights and ZHA groups (target the members).
        """
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None or entry.platform != ZHA_DOMAIN:
            return None
        # ZHA light unique_id is "<ieee>-<endpoint>[-<cluster or suffix>]";
        # groups are "light_zha_group_0x...".
        parts = entry.unique_id.split("-")
        if len(parts) < 2 or not parts[1].isdigit():
            return None
        light = ZhaLight(entity_id, parts[0], int(parts[1]))
        _device, endpoint = self._endpoint(light)
        if endpoint is None or LEVEL_CLUSTER not in endpoint.in_clusters:
            return None
        return light

    def _endpoint(self, light: ZhaLight) -> tuple[Any, Any]:
        """Return (zha Device, zigpy Endpoint) for a light, or (None, None)."""
        gateway = self._gateway()
        for ieee, proxy in gateway.device_proxies.items():
            if str(ieee) == light.ieee:
                device = proxy.device
                return device, device.device.endpoints.get(light.endpoint_id)
        return None, None

    def _clusters(self, light: ZhaLight) -> tuple[Any, Any, Any, Any]:
        """Return (zha Device, on/off, level, color) clusters."""
        device, endpoint = self._endpoint(light)
        if endpoint is None:
            return device, None, None, None
        clusters = endpoint.in_clusters
        return (
            device,
            clusters.get(ON_OFF_CLUSTER),
            clusters.get(LEVEL_CLUSTER),
            clusters.get(COLOR_CLUSTER),
        )

    @callback
    def get_attributes(self, light: ZhaLight) -> LightAttributes | None:
        """Return attributes from zigpy's attribute cache."""
        device, on_off_cluster, level, color = self._clusters(light)
        if device is None or level is None:
            return None

        def get(cluster: Any, name: str) -> Any:
            return None if cluster is None else cluster.get(name)

        caps = get(color, "color_capabilities")

        def supports(cap_bit: int, assume: bool) -> bool:
            if color is None:
                return False
            if caps is None:
                return assume
            return bool(caps & cap_bit)

        on_off = get(on_off_cluster, "on_off")
        on_level = get(level, "on_level")
        return LightAttributes(
            available=bool(device.available),
            on_off=None if on_off is None else bool(on_off),
            current_level=get(level, "current_level"),
            min_level=get(level, "min_level"),
            max_level=get(level, "max_level"),
            on_level=None if on_level in (None, ON_LEVEL_NULL) else on_level,
            # Only known once read; never write an attribute we haven't seen.
            has_on_level=on_level is not None,
            color_temp_mireds=get(color, "color_temperature"),
            ct_min_mireds=_mireds_or_none(get(color, "color_temp_physical_min")),
            ct_max_mireds=_mireds_or_none(get(color, "color_temp_physical_max")),
            # Without ColorCapabilities, assume color temp (most white bulbs)
            # and let capability detection confirm it; never assume full color.
            supports_color_temp=supports(COLOR_CAP_COLOR_TEMPERATURE, True),
            current_x=get(color, "current_x"),
            current_y=get(color, "current_y"),
            current_hue=get(color, "current_hue"),
            current_saturation=get(color, "current_saturation"),
            supports_xy=supports(COLOR_CAP_XY, False),
            supports_hs=supports(COLOR_CAP_HUE_SATURATION, False),
        )

    async def read_attributes(self, light: ZhaLight) -> LightAttributes | None:
        """Read on/off, level and color live from the bulb.

        Falls back to the cache if the read fails (sleepy or slow device).
        """
        _device, *clusters = self._clusters(light)
        for cluster in clusters:
            if cluster is None:
                continue
            names = _VERIFY_ATTRIBUTES.get(cluster.cluster_id, [])
            try:
                await cluster.read_attributes(names, allow_cache=False)
            except Exception as err:  # noqa: BLE001 - zigpy raises many types
                LOGGER.debug("Live read failed for %s: %s", light.entity_id, err)
        return self.get_attributes(light)

    async def _send(
        self, light: ZhaLight, cluster_id: int, command: str, **kwargs: Any
    ) -> None:
        if (cluster_id, command) not in ALLOWED_COMMANDS:
            raise RuntimeError(f"Refusing to send {command}")
        _device, endpoint = self._endpoint(light)
        cluster = None if endpoint is None else endpoint.in_clusters.get(cluster_id)
        if cluster is None:
            raise RuntimeError(f"{light.entity_id} has no cluster {cluster_id:#06x}")
        kwargs |= {
            "transition_time": 0,
            "options_mask": EXECUTE_IF_OFF,
            "options_override": EXECUTE_IF_OFF,
        }
        LOGGER.debug("Sending %s%s to %s", command, kwargs, light.entity_id)
        result = await getattr(cluster, command)(**kwargs)
        status = getattr(result, "status", None)
        if status is None and isinstance(result, (list, tuple)) and len(result) > 1:
            status = result[1]
        if status is not None and int(status) != 0:
            raise RuntimeError(f"{command} failed with status {status!r}")

    async def move_to_level(self, light: ZhaLight, level: int) -> None:
        """Set current_level without changing OnOff."""
        await self._send(light, LEVEL_CLUSTER, "move_to_level", level=level)

    async def move_to_color_temp(self, light: ZhaLight, mireds: int) -> None:
        """Set color_temperature without changing OnOff."""
        await self._send(
            light, COLOR_CLUSTER, "move_to_color_temp", color_temp_mireds=mireds
        )

    async def move_to_color_xy(self, light: ZhaLight, x: int, y: int) -> None:
        """Set current_x/current_y without changing OnOff."""
        await self._send(light, COLOR_CLUSTER, "move_to_color", color_x=x, color_y=y)

    async def move_to_hue_saturation(
        self, light: ZhaLight, hue: int, saturation: int
    ) -> None:
        """Set current_hue/current_saturation without changing OnOff."""
        await self._send(
            light,
            COLOR_CLUSTER,
            "move_to_hue_and_saturation",
            hue=hue,
            saturation=saturation,
        )

    async def clear_on_level(self, light: ZhaLight) -> None:
        """Write on_level = null (0xFF)."""
        _device, _on_off, level, _color = self._clusters(light)
        if level is None:
            return
        LOGGER.info("Clearing OnLevel on %s", light.entity_id)
        await level.write_attributes({"on_level": ON_LEVEL_NULL})

    @callback
    def subscribe_on_level(
        self, light: ZhaLight, on_change: Callable[[int | None], None]
    ) -> Callable[[], None]:
        """ZHA has no reliable push for on_level; the safety interval re-checks it."""
        return lambda: None

    @callback
    def subscribe_node_updated(
        self, on_update: Callable[[int], None]
    ) -> Callable[[], None]:
        """Not used for ZHA: OnLevel is re-checked on the safety interval."""
        return lambda: None
