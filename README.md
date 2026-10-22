# Preset While Off

A Home Assistant custom integration that sets the **brightness and color (color
temperature or full color) of Matter lights while they are off**, so they turn on directly at the right values.
This works even when they're turned on by a wall switch bound directly to the bulbs.

It uses the Matter `ExecuteIfOff` option on each command (`MoveToLevel`,
`MoveToColorTemperature`, `MoveToColor` and `MoveToHueAndSaturation`, all with
`optionsMask=1, optionsOverride=1`). It never sends `On`,
`Off`, `Toggle` or `MoveToLevelWithOnOff`, and never writes the persistent `Options`
attributes. **It never turns a light on or off.**

Requires Home Assistant 2026.9+ with the Matter integration. Zigbee/ZHA is not supported.

## Installation

- **HACS:** add this repository as a custom repository (category *Integration*) and
  install **Preset While Off**.
- **Manual:** copy `custom_components/preset_while_off` to `/config/custom_components/`.

Restart Home Assistant.

- To use only the action, add `preset_while_off:` to `configuration.yaml`.
- To create a rule, go to *Settings → Devices & services → Add integration →
  Preset While Off*. This also loads the action.

## Action: `preset_while_off.apply`

```yaml
action: preset_while_off.apply
target:
  entity_id: light.branch_lamp   # light groups are expanded to their members
data:
  brightness_pct: 25             # or brightness: 1-255
  color_temp_kelvin: 4890        # or a full color, see below
  apply_if_on: false             # true: lights that are on get light.turn_on instead
response_variable: result
```

- **Off Matter lights** get the preset. HA keeps showing them as off with no
  brightness. That's expected.
- **Lights that are on** are skipped, unless `apply_if_on: true`.
- **Non-Matter and unavailable lights** are skipped.

The response lists the lights under `applied`, `applied_on`, `skipped` (with a
reason) and `unsupported`.

### Full color

Instead of `color_temp_kelvin`, give one of:

- `rgb_color: [255, 100, 0]`
- `hs_color: [30, 100]` (hue 0–360, saturation 0–100)
- `xy_color: [0.6, 0.38]`

You can't combine a color temperature and a full color in one call.

- Lights that support CIE xy get `MoveToColor`.
- Lights with only hue/saturation get `MoveToHueAndSaturation`, converted from the
  same color.
- Lights without full color get only the brightness. If no brightness was given,
  they're listed as unsupported.

Some bulbs move a color they can't show to the nearest one they can. Any change
while off counts as supported.

Brightness is converted to a Matter level and clamped to `max(MinLevel,1)`–`min(MaxLevel,254)`.
It never sends 255. Kelvin is converted to mireds and clamped to the bulb's physical range.
Color temperature is only sent to lights whose `ColorCapabilities` include it.

## Rules ("follow a source")

Each config entry is one rule:

- **Lights:** Matter lights or light groups.
- **Source:** either
  - an Adaptive Lighting switch, using its `brightness_pct` / `color_temp_kelvin`
    attributes, or
  - separate brightness (%) and color temperature (K) entities.
- **Toggles:** sync brightness and sync color temperature.
- **Clear OnLevel on managed lights** (default on).
- **Safety re-apply interval** (default 10 min).

The preset is re-applied to every managed light that is off:

- when the source changes (2 s debounce; only changed values are sent)
- 1.5 s after a managed light turns off. Inovelli switches dim bulbs down to level 1
  on turn-off, which destroys the preset.
- when a light comes back from unavailable
- when HA starts
- on the safety interval

### OnLevel

A non-null `OnLevel` makes some bulbs (e.g. IKEA KAJPLATS E12) flash to 100% before
dropping to the level when they turn on. With *Clear OnLevel* on, each rule:

- writes `OnLevel = null` at startup and after a node re-interview.
- clears it again, logs a warning and raises a repair issue if something else
  rewrites it.

If an automation writes `number.*_on_level` for these lights, disable it for them.

## Capability detection

The first time a light gets a preset, the integration reads back `OnOff`, `CurrentLevel`
and the color attributes about 1 s later. Level, color temperature and full color
are tracked separately.

- **Value changed while off:** that part is marked supported.
- **Value unchanged:** that part is marked unsupported and no longer sent. A repair
  issue lists these lights.

The results are kept across restarts and shown in the rule's diagnostics download.
`preset_while_off.reset_capabilities` forgets them, for some lights or all, so they
are tested again.

## Manual test (branch lamp)

1. Confirm `light.branch_lamp` is off.
2. Call `preset_while_off.apply` on it with `brightness_pct: 25`, `color_temp_kelvin: 4890`.
3. Check the response and the diagnostics: `current_level` 64 and `color_temp_mireds`
   204 with `on_off: false`, and capabilities `supported`.
4. Turn the lamp on with the wall switch. It should come on with no flash, at the
   right level.

## Development

```sh
python3.14 -m venv venv && . venv/bin/activate
pip install -r requirements_test.txt
pytest
```

All Matter access is in `matter_backend.py`. If a Home Assistant release changes the
Matter integration's internals, that's the one file to fix.
