"""The only module that touches HA's Matter integration and the Matter Server.

Everything here uses internals of `homeassistant.components.matter` and the
`matter_server` python client. They can change between HA releases, so keep all
of that access in this one file.

Safety invariant: this module can only send `MoveToLevel`,
`MoveToColorTemperature`, `MoveToColor` (xy) and `MoveToHueAndSaturation`,
always with ExecuteIfOff. It never sends anything that
changes OnOff, and never writes the persistent `Options` attributes. The only
attribute it writes is `OnLevel`, and only to null.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from chip.clusters import Objects as clusters
from chip.clusters.Objects import NullValue
from chip.clusters.Types import Nullable
from homeassistant.components.matter.const import DOMAIN as MATTER_DOMAIN
from homeassistant.components.matter.helpers import (
    get_device_id,
    get_matter,
    get_node_from_device_entry,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from matter_server.common.helpers.util import create_attribute_path_from_attribute
from matter_server.common.models import EventType

from .const import LOGGER
from .models import BackendUnavailable, LightAttributes

# ExecuteIfOff is bit 0 of the LevelControl and ColorControl options bitmaps.
EXECUTE_IF_OFF = 1
# ColorCapabilities bits.
COLOR_CAP_HUE_SATURATION = 0x01
COLOR_CAP_XY = 0x08
COLOR_CAP_COLOR_TEMPERATURE = 0x10

ALLOWED_COMMANDS: tuple[type, ...] = (
    clusters.LevelControl.Commands.MoveToLevel,
    clusters.ColorControl.Commands.MoveToColorTemperature,
    clusters.ColorControl.Commands.MoveToColor,
    clusters.ColorControl.Commands.MoveToHueAndSaturation,
)

_ON_OFF = clusters.OnOff.Attributes.OnOff
_CURRENT_LEVEL = clusters.LevelControl.Attributes.CurrentLevel
_MIN_LEVEL = clusters.LevelControl.Attributes.MinLevel
_MAX_LEVEL = clusters.LevelControl.Attributes.MaxLevel
_ON_LEVEL = clusters.LevelControl.Attributes.OnLevel
_CT_MIREDS = clusters.ColorControl.Attributes.ColorTemperatureMireds
_CT_MIN = clusters.ColorControl.Attributes.ColorTempPhysicalMinMireds
_CT_MAX = clusters.ColorControl.Attributes.ColorTempPhysicalMaxMireds
_COLOR_CAPS = clusters.ColorControl.Attributes.ColorCapabilities
_CURRENT_X = clusters.ColorControl.Attributes.CurrentX
_CURRENT_Y = clusters.ColorControl.Attributes.CurrentY
_CURRENT_HUE = clusters.ColorControl.Attributes.CurrentHue
_CURRENT_SATURATION = clusters.ColorControl.Attributes.CurrentSaturation

# Attributes read back live after sending, for capability detection,
# mapped to the LightAttributes field they update.
_VERIFY_ATTRIBUTES = {
    _ON_OFF: "on_off",
    _CURRENT_LEVEL: "current_level",
    _CT_MIREDS: "color_temp_mireds",
    _CURRENT_X: "current_x",
    _CURRENT_Y: "current_y",
    _CURRENT_HUE: "current_hue",
    _CURRENT_SATURATION: "current_saturation",
}


class MatterUnavailable(BackendUnavailable):
    """Raised when the Matter integration or server is not ready."""


@dataclass(frozen=True, slots=True)
class MatterLight:
    """A light entity mapped to a Matter node endpoint."""

    entity_id: str
    node_id: int
    endpoint_id: int


def _null_to_none(value: Any) -> Any:
    if value is None or value is NullValue or isinstance(value, Nullable):
        return None
    return value


def build_move_to_level(level: int) -> clusters.ClusterCommand:
    """Build a MoveToLevel command that executes while the light is off."""
    return clusters.LevelControl.Commands.MoveToLevel(
        level=level,
        transitionTime=0,
        optionsMask=EXECUTE_IF_OFF,
        optionsOverride=EXECUTE_IF_OFF,
    )


def build_move_to_color_temp(mireds: int) -> clusters.ClusterCommand:
    """Build a MoveToColorTemperature command that executes while off."""
    return clusters.ColorControl.Commands.MoveToColorTemperature(
        colorTemperatureMireds=mireds,
        transitionTime=0,
        optionsMask=EXECUTE_IF_OFF,
        optionsOverride=EXECUTE_IF_OFF,
    )


def build_move_to_color(x: int, y: int) -> clusters.ClusterCommand:
    """Build a MoveToColor (CIE xy) command that executes while off."""
    return clusters.ColorControl.Commands.MoveToColor(
        colorX=x,
        colorY=y,
        transitionTime=0,
        optionsMask=EXECUTE_IF_OFF,
        optionsOverride=EXECUTE_IF_OFF,
    )


def build_move_to_hue_saturation(hue: int, saturation: int) -> clusters.ClusterCommand:
    """Build a MoveToHueAndSaturation command that executes while off."""
    return clusters.ColorControl.Commands.MoveToHueAndSaturation(
        hue=hue,
        saturation=saturation,
        transitionTime=0,
        optionsMask=EXECUTE_IF_OFF,
        optionsOverride=EXECUTE_IF_OFF,
    )


class MatterBackend:
    """Access to Matter lights through HA's own Matter client connection."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the backend."""
        self.hass = hass

    def _client(self) -> Any:
        try:
            client = get_matter(self.hass).matter_client
        except (IndexError, AttributeError, KeyError) as err:
            raise MatterUnavailable("Matter integration is not loaded") from err
        if client.server_info is None:
            raise MatterUnavailable("Matter server is not connected")
        return client

    @property
    def available(self) -> bool:
        """Return True if the Matter integration is loaded and connected."""
        try:
            self._client()
        except MatterUnavailable:
            return False
        return True

    @callback
    def connection_id(self) -> int | None:
        """Return an id that changes when the Matter client is replaced.

        Event subscriptions belong to a client, so callers re-subscribe when
        this changes (e.g. after the Matter integration reloads).
        """
        try:
            return id(self._client())
        except MatterUnavailable:
            return None

    @callback
    def resolve(self, entity_id: str) -> MatterLight | None:
        """Map a light entity to its Matter node and endpoint.

        Returns None for non-Matter lights or when the node can't be found.
        """
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None or entry.platform != MATTER_DOMAIN or not entry.device_id:
            return None
        device = dr.async_get(self.hass).async_get(entry.device_id)
        if device is None:
            return None
        client = self._client()
        node = get_node_from_device_entry(self.hass, device)
        if node is None:
            return None
        # Matter entity unique_id is "<device_id>-<endpoint_id>-<key>-<cluster>-<attr>"
        for endpoint in node.endpoints.values():
            prefix = (
                f"{get_device_id(client.server_info, endpoint)}-{endpoint.endpoint_id}-"
            )
            if entry.unique_id.startswith(prefix):
                return MatterLight(entity_id, node.node_id, endpoint.endpoint_id)
        return None

    @callback
    def get_attributes(self, light: MatterLight) -> LightAttributes | None:
        """Return attributes from the node's cached attribute map."""
        client = self._client()
        try:
            node = client.get_node(light.node_id)
        except Exception:  # noqa: BLE001 - client raises a generic error
            return None
        endpoint = node.endpoints.get(light.endpoint_id)
        if endpoint is None:
            return None

        def get(attribute: type) -> Any:
            return _null_to_none(endpoint.get_attribute_value(None, attribute))

        caps = get(_COLOR_CAPS)

        def supports(attribute: type, cap_bit: int) -> bool:
            return endpoint.has_attribute(None, attribute) and (
                caps is None or bool(caps & cap_bit)
            )

        on_off = get(_ON_OFF)
        return LightAttributes(
            available=node.available,
            on_off=None if on_off is None else bool(on_off),
            current_level=get(_CURRENT_LEVEL),
            min_level=get(_MIN_LEVEL),
            max_level=get(_MAX_LEVEL),
            on_level=get(_ON_LEVEL),
            has_on_level=endpoint.has_attribute(None, _ON_LEVEL),
            color_temp_mireds=get(_CT_MIREDS),
            ct_min_mireds=get(_CT_MIN) or None,
            ct_max_mireds=get(_CT_MAX) or None,
            supports_color_temp=supports(_CT_MIREDS, COLOR_CAP_COLOR_TEMPERATURE),
            current_x=get(_CURRENT_X),
            current_y=get(_CURRENT_Y),
            current_hue=get(_CURRENT_HUE),
            current_saturation=get(_CURRENT_SATURATION),
            supports_xy=supports(_CURRENT_X, COLOR_CAP_XY),
            supports_hs=supports(_CURRENT_HUE, COLOR_CAP_HUE_SATURATION),
        )

    async def read_attributes(self, light: MatterLight) -> LightAttributes | None:
        """Read OnOff, CurrentLevel and the color attributes live from the device.

        Falls back to the cache if the live read fails.
        """
        client = self._client()
        paths = {
            create_attribute_path_from_attribute(light.endpoint_id, attr): name
            for attr, name in _VERIFY_ATTRIBUTES.items()
        }
        try:
            values = await client.read_attribute(light.node_id, list(paths))
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Live read failed for %s: %s", light.entity_id, err)
            values = {}
        attrs = self.get_attributes(light)
        if attrs is None:
            return None
        for path, name in paths.items():
            if path in values:
                setattr(attrs, name, _null_to_none(values[path]))
        if attrs.on_off is not None:
            attrs.on_off = bool(attrs.on_off)
        return attrs

    async def _send(self, light: MatterLight, command: clusters.ClusterCommand) -> None:
        if not isinstance(command, ALLOWED_COMMANDS):
            raise RuntimeError(f"Refusing to send {type(command).__name__}")
        if (
            command.optionsMask != EXECUTE_IF_OFF
            or command.optionsOverride != EXECUTE_IF_OFF
        ):
            raise RuntimeError("Commands must use per-command ExecuteIfOff")
        LOGGER.debug("Sending %s to %s", command, light.entity_id)
        await self._client().send_device_command(
            node_id=light.node_id,
            endpoint_id=light.endpoint_id,
            command=command,
        )

    async def move_to_level(self, light: MatterLight, level: int) -> None:
        """Set CurrentLevel without changing OnOff."""
        await self._send(light, build_move_to_level(level))

    async def move_to_color_temp(self, light: MatterLight, mireds: int) -> None:
        """Set ColorTemperatureMireds without changing OnOff."""
        await self._send(light, build_move_to_color_temp(mireds))

    async def move_to_color_xy(self, light: MatterLight, x: int, y: int) -> None:
        """Set CurrentX/CurrentY without changing OnOff."""
        await self._send(light, build_move_to_color(x, y))

    async def move_to_hue_saturation(
        self, light: MatterLight, hue: int, saturation: int
    ) -> None:
        """Set CurrentHue/CurrentSaturation without changing OnOff."""
        await self._send(light, build_move_to_hue_saturation(hue, saturation))

    async def clear_on_level(self, light: MatterLight) -> None:
        """Write OnLevel = null."""
        LOGGER.info("Clearing OnLevel on %s", light.entity_id)
        await self._client().write_attribute(
            node_id=light.node_id,
            attribute_path=create_attribute_path_from_attribute(
                light.endpoint_id, _ON_LEVEL
            ),
            value=None,
        )

    @callback
    def subscribe_on_level(
        self, light: MatterLight, on_change: Callable[[int | None], None]
    ) -> Callable[[], None]:
        """Call `on_change` with the new OnLevel whenever it is reported."""

        def _handle(event: EventType, value: Any) -> None:
            on_change(_null_to_none(value))

        return self._client().subscribe_events(
            _handle,
            event_filter=EventType.ATTRIBUTE_UPDATED,
            node_filter=light.node_id,
            attr_path_filter=create_attribute_path_from_attribute(
                light.endpoint_id, _ON_LEVEL
            ),
        )

    @callback
    def subscribe_node_updated(
        self, on_update: Callable[[int], None]
    ) -> Callable[[], None]:
        """Call `on_update(node_id)` when a node is updated (e.g. re-interviewed)."""

        def _handle(event: EventType, node: Any) -> None:
            on_update(node.node_id)

        return self._client().subscribe_events(
            _handle, event_filter=EventType.NODE_UPDATED
        )
