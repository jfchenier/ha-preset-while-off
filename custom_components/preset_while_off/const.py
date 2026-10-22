"""Constants for Preset While Off."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "preset_while_off"
LOGGER = logging.getLogger(__package__)

SERVICE_APPLY: Final = "apply"

# Service fields
ATTR_BRIGHTNESS_PCT: Final = "brightness_pct"
ATTR_BRIGHTNESS: Final = "brightness"
ATTR_COLOR_TEMP_KELVIN: Final = "color_temp_kelvin"
ATTR_APPLY_IF_ON: Final = "apply_if_on"

# Config entry options
CONF_LIGHTS: Final = "lights"
CONF_SOURCE_TYPE: Final = "source_type"
CONF_AL_SWITCH: Final = "adaptive_lighting_switch"
CONF_BRIGHTNESS_ENTITY: Final = "brightness_entity"
CONF_COLOR_TEMP_ENTITY: Final = "color_temp_entity"
CONF_SYNC_BRIGHTNESS: Final = "sync_brightness"
CONF_SYNC_COLOR_TEMP: Final = "sync_color_temp"
CONF_CLEAR_ON_LEVEL: Final = "clear_on_level"
CONF_INTERVAL: Final = "interval_minutes"

SOURCE_ADAPTIVE_LIGHTING: Final = "adaptive_lighting"
SOURCE_ENTITIES: Final = "entities"

# Adaptive Lighting switch attributes used as source
AL_ATTR_BRIGHTNESS_PCT: Final = "brightness_pct"
AL_ATTR_COLOR_TEMP_KELVIN: Final = "color_temp_kelvin"

DEFAULT_INTERVAL_MINUTES: Final = 10

# Timing (seconds)
SOURCE_DEBOUNCE: Final = 2.0
TURN_OFF_DELAY: Final = 1.5
COMMAND_SPACING: Final = 0.175
VERIFY_DELAY: Final = 1.0
STARTUP_RETRY: Final = 30.0

# Matter level limits. OnLevel / CurrentLevel are uint8 where 255 means null.
LEVEL_MIN: Final = 1
LEVEL_MAX: Final = 254

# Capability states
CAP_UNKNOWN: Final = "unknown"
CAP_SUPPORTED: Final = "supported"
CAP_UNSUPPORTED: Final = "unsupported"

STORAGE_KEY: Final = f"{DOMAIN}.capabilities"
STORAGE_VERSION: Final = 1

ISSUE_UNSUPPORTED: Final = "unsupported_lights"
ISSUE_ON_LEVEL_REWRITTEN: Final = "on_level_rewritten"
