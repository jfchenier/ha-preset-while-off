"""Per-rule listeners: source changes, light turn-off, startup and interval."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    AL_ATTR_BRIGHTNESS_PCT,
    AL_ATTR_COLOR_TEMP_KELVIN,
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
    ISSUE_ON_LEVEL_REWRITTEN,
    LOGGER,
    SOURCE_ADAPTIVE_LIGHTING,
    SOURCE_DEBOUNCE,
    STARTUP_RETRY,
    TURN_OFF_DELAY,
)
from .models import BackendUnavailable
from .preset import PresetManager, expand_lights


def _to_float(value: Any) -> float | None:
    try:
        result = float(value)
    except TypeError, ValueError:
        return None
    return result


class RuleCoordinator:
    """Keep the managed lights preset to a source while they are off."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, manager: PresetManager
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.manager = manager
        self.options: dict[str, Any] = {**entry.data, **entry.options}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._light_unsub: CALLBACK_TYPE | None = None
        self._matter_unsubs: list[Callable[[], None]] = []
        self._matter_connection: int | None = None
        self._tracked: set[str] = set()
        self._pending_off: set[str] = set()
        self._off_timer: CALLBACK_TYPE | None = None
        self._startup_timer: CALLBACK_TYPE | None = None
        self.on_level_rewrites: dict[str, int] = {}
        self.last_source: dict[str, float | None] = {}
        self._source_debouncer = Debouncer(
            hass,
            LOGGER,
            cooldown=SOURCE_DEBOUNCE,
            immediate=False,
            function=self._async_source_changed,
        )

    # ----------------------------------------------------------------- config

    @property
    def lights(self) -> list[str]:
        """Configured light entities (groups not expanded)."""
        return list(self.options.get(CONF_LIGHTS, []))

    @property
    def source_entities(self) -> list[str]:
        """Entities whose state drives the preset."""
        if self.options.get(CONF_SOURCE_TYPE) == SOURCE_ADAPTIVE_LIGHTING:
            return [self.options[CONF_AL_SWITCH]]
        return [
            entity_id
            for key in (CONF_BRIGHTNESS_ENTITY, CONF_COLOR_TEMP_ENTITY)
            if (entity_id := self.options.get(key))
        ]

    @callback
    def source_values(self) -> tuple[float | None, float | None]:
        """Return (brightness_pct, color_temp_kelvin) from the source."""
        brightness: float | None = None
        kelvin: float | None = None
        if self.options.get(CONF_SOURCE_TYPE) == SOURCE_ADAPTIVE_LIGHTING:
            state = self.hass.states.get(self.options[CONF_AL_SWITCH])
            if state is not None and state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                brightness = _to_float(state.attributes.get(AL_ATTR_BRIGHTNESS_PCT))
                kelvin = _to_float(state.attributes.get(AL_ATTR_COLOR_TEMP_KELVIN))
        else:
            if entity_id := self.options.get(CONF_BRIGHTNESS_ENTITY):
                if state := self.hass.states.get(entity_id):
                    brightness = _to_float(state.state)
            if entity_id := self.options.get(CONF_COLOR_TEMP_ENTITY):
                if state := self.hass.states.get(entity_id):
                    kelvin = _to_float(state.state)

        if not self.options.get(CONF_SYNC_BRIGHTNESS, True):
            brightness = None
        if not self.options.get(CONF_SYNC_COLOR_TEMP, True):
            kelvin = None
        if brightness is not None:
            brightness = min(max(brightness, 1.0), 100.0)
        if kelvin is not None and kelvin <= 0:
            kelvin = None
        self.last_source = {"brightness_pct": brightness, "color_temp_kelvin": kelvin}
        return brightness, kelvin

    # -------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        """Start listening."""
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, self.source_entities, self._handle_source_event
            )
        )
        interval = self.options.get(CONF_INTERVAL, DEFAULT_INTERVAL_MINUTES)
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_interval,
                timedelta(minutes=interval),
                name=f"{DOMAIN} safety interval",
            )
        )
        self._refresh_light_tracking()

        if self.hass.is_running:
            self.entry.async_create_task(self.hass, self._async_startup())
        else:
            self._unsubs.append(
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._async_startup
                )
            )

    async def async_stop(self) -> None:
        """Stop all listeners."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._light_unsub:
            self._light_unsub()
            self._light_unsub = None
        self._unsubscribe_matter()
        for timer in (self._off_timer, self._startup_timer):
            if timer:
                timer()
        self._off_timer = self._startup_timer = None
        self._source_debouncer.async_shutdown()
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_ON_LEVEL_REWRITTEN}_{self.entry.entry_id}"
        )

    @callback
    def _schedule_startup(self, delay: float) -> None:
        if self._startup_timer:
            self._startup_timer()
        self._startup_timer = async_call_later(self.hass, delay, self._async_startup)

    async def _async_startup(self, _now: Any = None) -> None:
        self._startup_timer = None
        if not self.manager.backend.available:
            LOGGER.debug("Matter/ZHA not ready, retrying in %ss", STARTUP_RETRY)
            self._schedule_startup(STARTUP_RETRY)
            return
        await self._async_ensure_backends()
        await self.async_apply_all()

    # --------------------------------------------------------------- applying

    async def async_apply_all(self, *, skip_if_unchanged: bool = False) -> None:
        """Apply the source values to every managed light that is off."""
        self._refresh_light_tracking()
        await self._async_apply(self.lights, skip_if_unchanged=skip_if_unchanged)

    async def _async_apply(
        self, entity_ids: list[str], *, skip_if_unchanged: bool
    ) -> None:
        brightness, kelvin = self.source_values()
        if brightness is None and kelvin is None:
            LOGGER.debug("%s: source has no usable values", self.entry.title)
            return
        result = await self.manager.async_apply(
            entity_ids,
            brightness_pct=brightness,
            color_temp_kelvin=kelvin,
            skip_if_unchanged=skip_if_unchanged,
        )
        LOGGER.debug("%s: applied %s", self.entry.title, result.as_dict())

    async def _async_interval(self, _now: Any) -> None:
        await self._async_ensure_backends()
        await self.async_apply_all()

    @callback
    def _handle_source_event(self, _event: Event[EventStateChangedData]) -> None:
        self._source_debouncer.async_schedule_call()

    async def _async_source_changed(self) -> None:
        await self.async_apply_all(skip_if_unchanged=True)

    # ------------------------------------------------------- light turn-off

    @callback
    def _refresh_light_tracking(self) -> None:
        tracked = set(self.lights) | set(expand_lights(self.hass, self.lights))
        if tracked == self._tracked and self._light_unsub is not None:
            return
        if self._light_unsub:
            self._light_unsub()
        self._tracked = tracked
        self._light_unsub = async_track_state_change_event(
            self.hass, sorted(tracked), self._handle_light_event
        )

    @callback
    def _handle_light_event(self, event: Event[EventStateChangedData]) -> None:
        old = event.data["old_state"]
        new = event.data["new_state"]
        if new is None or new.state != STATE_OFF:
            return
        if old is not None and old.state not in (
            STATE_ON,
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return
        # on -> off: the switch dims the bulb down to level 1, wait for it.
        # unavailable -> off: the light came back, retry it.
        self._pending_off.add(new.entity_id)
        if self._off_timer:
            self._off_timer()
        self._off_timer = async_call_later(
            self.hass, TURN_OFF_DELAY, self._async_apply_turned_off
        )

    async def _async_apply_turned_off(self, _now: Any) -> None:
        self._off_timer = None
        entity_ids = sorted(self._pending_off)
        self._pending_off.clear()
        if entity_ids:
            await self._async_apply(entity_ids, skip_if_unchanged=False)

    # ------------------------------------------------------------ OnLevel

    @callback
    def _unsubscribe_matter(self) -> None:
        for unsub in self._matter_unsubs:
            try:
                unsub()
            except ValueError:
                pass  # client already dropped its subscribers
        self._matter_unsubs.clear()
        self._matter_connection = None

    async def _async_ensure_backends(self) -> None:
        """(Re)subscribe to backend events and clear OnLevel if enabled."""
        if not self.options.get(CONF_CLEAR_ON_LEVEL, True):
            return
        backend = self.manager.backend
        connection = backend.connection_id()
        if connection is None:
            return
        if connection != self._matter_connection:
            self._unsubscribe_matter()
            self._matter_connection = connection
            self._matter_unsubs.append(
                backend.subscribe_node_updated(self._handle_node_updated)
            )
            for entity_id in expand_lights(self.hass, self.lights):
                light = backend.resolve(entity_id)
                if light is None:
                    continue
                self._matter_unsubs.append(
                    backend.subscribe_on_level(
                        light,
                        lambda value, eid=entity_id: self._handle_on_level(eid, value),
                    )
                )
        await self.async_clear_on_levels()

    async def async_clear_on_levels(self, node_id: int | None = None) -> None:
        """Write OnLevel = null on managed lights where it isn't null."""
        backend = self.manager.backend
        for entity_id in expand_lights(self.hass, self.lights):
            try:
                light = backend.resolve(entity_id)
                if light is None or (
                    node_id is not None and getattr(light, "node_id", None) != node_id
                ):
                    continue
                attrs = backend.get_attributes(light)
                if attrs is None or not attrs.available or not attrs.has_on_level:
                    continue
                if attrs.on_level is not None:
                    await backend.clear_on_level(light)
            except BackendUnavailable:
                return
            except Exception as err:  # noqa: BLE001
                LOGGER.warning("Could not clear OnLevel on %s: %s", entity_id, err)

    @callback
    def _handle_node_updated(self, node_id: int) -> None:
        self.entry.async_create_background_task(
            self.hass,
            self.async_clear_on_levels(node_id),
            f"{DOMAIN} clear OnLevel node {node_id}",
        )

    @callback
    def _handle_on_level(self, entity_id: str, value: int | None) -> None:
        if value is None:
            return
        count = self.on_level_rewrites.get(entity_id, 0) + 1
        self.on_level_rewrites[entity_id] = count
        LOGGER.warning(
            "OnLevel on %s was set to %s by something else (%s times). "
            "A non-null OnLevel brings back the flash on turn-on; clearing it again. "
            "Check for automations writing number.*_on_level for this light",
            entity_id,
            value,
            count,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_ON_LEVEL_REWRITTEN}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_ON_LEVEL_REWRITTEN,
            translation_placeholders={
                "rule": self.entry.title,
                "lights": ", ".join(sorted(self.on_level_rewrites)),
            },
        )
        self.entry.async_create_background_task(
            self.hass,
            self.async_clear_on_levels(),
            f"{DOMAIN} re-clear OnLevel",
        )
