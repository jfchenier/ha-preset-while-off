"""Resolve targets, clamp values, apply presets and detect capabilities."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS_PCT as LIGHT_ATTR_BRIGHTNESS_PCT,
)
from homeassistant.components.light import (
    ATTR_COLOR_TEMP_KELVIN as LIGHT_ATTR_COLOR_TEMP_KELVIN,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.components.light import (
    SERVICE_TURN_ON,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import (
    CAP_SUPPORTED,
    CAP_UNKNOWN,
    CAP_UNSUPPORTED,
    COMMAND_SPACING,
    DOMAIN,
    ISSUE_UNSUPPORTED,
    LEVEL_MAX,
    LEVEL_MIN,
    LOGGER,
    STORAGE_KEY,
    STORAGE_VERSION,
    VERIFY_DELAY,
)
from .matter_backend import MatterBackend, MatterLight, MatterUnavailable

PART_LEVEL = "level"
PART_COLOR_TEMP = "color_temp"


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def pct_to_level(brightness_pct: float) -> int:
    """Convert 0-100 % to a Matter level (0-254). Never returns 255."""
    return _round_half_up(brightness_pct * LEVEL_MAX / 100)


def brightness_to_level(brightness: float) -> int:
    """Convert HA brightness (0-255) to a Matter level (0-254)."""
    return _round_half_up(brightness * LEVEL_MAX / 255)


def clamp_level(level: int, min_level: int | None, max_level: int | None) -> int:
    """Clamp to max(MinLevel, 1) .. min(MaxLevel, 254)."""
    low = max(min_level or LEVEL_MIN, LEVEL_MIN)
    high = min(max_level or LEVEL_MAX, LEVEL_MAX)
    low = min(low, high)
    return max(low, min(high, int(level)))


def kelvin_to_mireds(
    kelvin: float, min_mireds: int | None = None, max_mireds: int | None = None
) -> int:
    """Convert Kelvin to mireds, clamped to the bulb's physical range."""
    mireds = _round_half_up(1_000_000 / kelvin)
    if min_mireds:
        mireds = max(mireds, min_mireds)
    if max_mireds:
        mireds = min(mireds, max_mireds)
    return mireds


@callback
def expand_lights(hass: HomeAssistant, entity_ids: Iterable[str]) -> list[str]:
    """Expand light groups (and old-style groups) to member light entities."""
    result: list[str] = []
    seen: set[str] = set()

    def visit(entity_id: str) -> None:
        if entity_id in seen:
            return
        seen.add(entity_id)
        state = hass.states.get(entity_id)
        members = state.attributes.get(ATTR_ENTITY_ID) if state else None
        if isinstance(members, (list, tuple)) and members:
            for member in members:
                visit(member)
            return
        if entity_id.startswith(f"{LIGHT_DOMAIN}."):
            result.append(entity_id)

    for entity_id in entity_ids:
        visit(entity_id)
    return result


@dataclass
class ApplyResult:
    """Outcome of one apply run."""

    applied: list[str] = field(default_factory=list)
    applied_on: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    unsupported: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable service response."""
        return {
            "applied": self.applied,
            "applied_on": self.applied_on,
            "skipped": self.skipped,
            "unsupported": self.unsupported,
        }


@dataclass
class _Pending:
    light: MatterLight
    level: int | None
    level_before: int | None
    mireds: int | None
    mireds_before: int | None


class PresetManager:
    """Apply presets to lights. Shared by the action and all rules."""

    def __init__(self, hass: HomeAssistant, backend: MatterBackend) -> None:
        """Initialize."""
        self.hass = hass
        self.backend = backend
        self._lock = asyncio.Lock()
        self._store: Store[dict[str, dict[str, str]]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self.capabilities: dict[str, dict[str, str]] = {}
        self.last_results: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Load cached capabilities."""
        self.capabilities = await self._store.async_load() or {}
        self._update_issue()

    def capability(self, entity_id: str, part: str) -> str:
        """Return the capability state of one part of a light."""
        return self.capabilities.get(entity_id, {}).get(part, CAP_UNKNOWN)

    def _set_capability(self, entity_id: str, part: str, value: str) -> bool:
        caps = self.capabilities.setdefault(entity_id, {})
        if caps.get(part) == value:
            return False
        caps[part] = value
        LOGGER.info("%s: %s while off is %s", entity_id, part, value)
        return True

    async def async_reset_capabilities(self, entity_ids: Iterable[str] | None) -> None:
        """Forget capability results so lights get re-tested."""
        if entity_ids is None:
            self.capabilities = {}
        else:
            for entity_id in entity_ids:
                self.capabilities.pop(entity_id, None)
        await self._store.async_save(self.capabilities)
        self._update_issue()

    @callback
    def _update_issue(self) -> None:
        lines = [
            f"- {entity_id}: "
            + ", ".join(
                part for part, value in caps.items() if value == CAP_UNSUPPORTED
            )
            for entity_id, caps in sorted(self.capabilities.items())
            if CAP_UNSUPPORTED in caps.values()
        ]
        if not lines:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_UNSUPPORTED)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_UNSUPPORTED,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_UNSUPPORTED,
            translation_placeholders={"lights": "\n".join(lines)},
        )

    async def async_apply(
        self,
        entity_ids: Iterable[str],
        *,
        level: int | None = None,
        brightness_pct: float | None = None,
        color_temp_kelvin: float | None = None,
        apply_if_on: bool = False,
        skip_if_unchanged: bool = False,
    ) -> ApplyResult:
        """Apply a preset to every resolved light that is off.

        `level` (Matter 0-254) takes precedence over `brightness_pct`.
        """
        if level is None and brightness_pct is not None:
            level = pct_to_level(brightness_pct)
        result = ApplyResult()
        lights = expand_lights(self.hass, entity_ids)
        turn_on: list[str] = []
        pending: list[_Pending] = []

        async with self._lock:
            for entity_id in lights:
                state = self.hass.states.get(entity_id)
                if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                    result.skipped[entity_id] = "unavailable"
                    continue
                if state.state == STATE_ON:
                    if apply_if_on:
                        turn_on.append(entity_id)
                    else:
                        result.skipped[entity_id] = "on"
                    continue
                try:
                    item = await self._apply_one(
                        entity_id, level, color_temp_kelvin, skip_if_unchanged, result
                    )
                except MatterUnavailable as err:
                    result.skipped[entity_id] = f"matter_unavailable: {err}"
                    continue
                except Exception as err:  # noqa: BLE001 - keep going with other lights
                    LOGGER.warning("Failed to apply preset to %s: %s", entity_id, err)
                    result.skipped[entity_id] = f"error: {err}"
                    continue
                if item is not None:
                    pending.append(item)

            if pending:
                await asyncio.sleep(VERIFY_DELAY)
                await self._verify(pending)

        if turn_on:
            data: dict[str, Any] = {ATTR_ENTITY_ID: turn_on}
            if level is not None:
                data[LIGHT_ATTR_BRIGHTNESS_PCT] = round(level * 100 / LEVEL_MAX, 1)
            if color_temp_kelvin is not None:
                data[LIGHT_ATTR_COLOR_TEMP_KELVIN] = int(color_temp_kelvin)
            await self.hass.services.async_call(
                LIGHT_DOMAIN, SERVICE_TURN_ON, data, blocking=True
            )
            result.applied_on.extend(turn_on)

        for entity_id in lights:
            self.last_results[entity_id] = {
                "level": level,
                "color_temp_kelvin": color_temp_kelvin,
                "outcome": "applied"
                if entity_id in result.applied
                else result.skipped.get(entity_id)
                or result.unsupported.get(entity_id)
                or ("applied_on" if entity_id in result.applied_on else None),
            }
        return result

    async def _apply_one(
        self,
        entity_id: str,
        level: int | None,
        kelvin: float | None,
        skip_if_unchanged: bool,
        result: ApplyResult,
    ) -> _Pending | None:
        light = self.backend.resolve(entity_id)
        if light is None:
            LOGGER.debug("%s is not a Matter light, skipping", entity_id)
            result.unsupported[entity_id] = "not_matter"
            return None
        attrs = self.backend.get_attributes(light)
        if attrs is None or not attrs.available:
            result.skipped[entity_id] = "unavailable"
            return None
        if attrs.on_off:
            result.skipped[entity_id] = "on"
            return None

        send_level: int | None = None
        if (
            level is not None
            and self.capability(entity_id, PART_LEVEL) != CAP_UNSUPPORTED
        ):
            send_level = clamp_level(level, attrs.min_level, attrs.max_level)
        send_mireds: int | None = None
        if (
            kelvin is not None
            and attrs.supports_color_temp
            and self.capability(entity_id, PART_COLOR_TEMP) != CAP_UNSUPPORTED
        ):
            send_mireds = kelvin_to_mireds(
                kelvin, attrs.ct_min_mireds, attrs.ct_max_mireds
            )

        if send_level is None and send_mireds is None:
            result.unsupported[entity_id] = "no_supported_parts"
            return None
        if skip_if_unchanged:
            if send_level == attrs.current_level:
                send_level = None
            if send_mireds == attrs.color_temp_mireds:
                send_mireds = None
            if send_level is None and send_mireds is None:
                result.skipped[entity_id] = "unchanged"
                return None

        if send_level is not None:
            await self.backend.move_to_level(light, send_level)
            await asyncio.sleep(COMMAND_SPACING)
        if send_mireds is not None:
            await self.backend.move_to_color_temp(light, send_mireds)
            await asyncio.sleep(COMMAND_SPACING)
        result.applied.append(entity_id)

        verify_level = (
            send_level is not None
            and self.capability(entity_id, PART_LEVEL) == CAP_UNKNOWN
        )
        verify_ct = (
            send_mireds is not None
            and self.capability(entity_id, PART_COLOR_TEMP) == CAP_UNKNOWN
        )
        if not (verify_level or verify_ct):
            return None
        return _Pending(
            light,
            send_level if verify_level else None,
            attrs.current_level,
            send_mireds if verify_ct else None,
            attrs.color_temp_mireds,
        )

    async def _verify(self, pending: list[_Pending]) -> None:
        changed = False
        for item in pending:
            entity_id = item.light.entity_id
            try:
                attrs = await self.backend.read_attributes(item.light)
            except MatterUnavailable:
                return
            if attrs is None or attrs.on_off:
                # Turned on meanwhile (or unknown): can't tell, try next time.
                continue
            for part, sent, before, now in (
                (PART_LEVEL, item.level, item.level_before, attrs.current_level),
                (
                    PART_COLOR_TEMP,
                    item.mireds,
                    item.mireds_before,
                    attrs.color_temp_mireds,
                ),
            ):
                if sent is None or sent == before:
                    continue
                if now == sent:
                    changed |= self._set_capability(entity_id, part, CAP_SUPPORTED)
                elif now == before:
                    changed |= self._set_capability(entity_id, part, CAP_UNSUPPORTED)
        if changed:
            await self._store.async_save(self.capabilities)
            self._update_issue()
