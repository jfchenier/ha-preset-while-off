"""Types shared by the light backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class BackendUnavailable(Exception):
    """Raised when a backend's integration (Matter, ZHA) is not ready."""


@dataclass(slots=True)
class LightAttributes:
    """Snapshot of the light attributes this integration cares about."""

    available: bool
    on_off: bool | None
    current_level: int | None
    min_level: int | None
    max_level: int | None
    on_level: int | None
    has_on_level: bool
    color_temp_mireds: int | None
    ct_min_mireds: int | None
    ct_max_mireds: int | None
    supports_color_temp: bool
    current_x: int | None = None
    current_y: int | None = None
    current_hue: int | None = None
    current_saturation: int | None = None
    supports_xy: bool = False
    supports_hs: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return a dict for diagnostics."""
        return {
            "available": self.available,
            "on_off": self.on_off,
            "current_level": self.current_level,
            "min_level": self.min_level,
            "max_level": self.max_level,
            "on_level": self.on_level,
            "color_temp_mireds": self.color_temp_mireds,
            "ct_min_mireds": self.ct_min_mireds,
            "ct_max_mireds": self.ct_max_mireds,
            "supports_color_temp": self.supports_color_temp,
            "current_x": self.current_x,
            "current_y": self.current_y,
            "current_hue": self.current_hue,
            "current_saturation": self.current_saturation,
            "supports_xy": self.supports_xy,
            "supports_hs": self.supports_hs,
        }
