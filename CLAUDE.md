# CLAUDE.md — "Preset While Off" Home Assistant custom integration

## Goal

Build a Home Assistant custom integration (HACS-installable) that sets the
**brightness and color temperature of Matter lights while they are off**, so they
turn on directly at the right values.

Two problems this solves:

1. **HA can't do it natively.** `light.turn_on` always turns the light on. HA has no
   "change brightness or color without turning on" action.
2. **Flash and slow fade with switch bindings.** The owner's Inovelli White switches
   control the bulbs directly through Matter group binding, without going through HA.
   Today the turn-on brightness comes from the bulbs' `OnLevel`. That makes IKEA
   KAJPLATS E12 bulbs flash to 100% before dropping to the level. Without `OnLevel`,
   bulbs come back on at their last level, and Adaptive Lighting then corrects them
   with a visible fade. Pre-setting level and color while off removes both problems.
   This was **verified on real hardware** (see below).

Working name: `preset_while_off` (domain). It can be renamed before the first release.

## Verified facts (tested 2026-10-22 on the owner's setup)

- **Matter "execute if off" works on IKEA KAJPLATS E12 WS B38 (fw 1.2.0) for both
  brightness and color temperature.**
  - Test: with the bulb off, send `MoveToLevel` and `MoveToColorTemperature` with
    `optionsMask=1, optionsOverride=1`.
  - Result: `CurrentLevel` and `ColorTemperatureMireds` update while `OnOff` stays
    `false`. When the wall switch then sends a group `On`, the bulb comes on directly
    at the preset values.
  - **No flash**, provided `OnLevel` is null.
- **The flash is caused by `OnLevel`.** E12 bulbs with a non-null `OnLevel` jump to
  100% and then drop to `OnLevel` when they receive `On`. The KAJPLATS GU10 CWS with
  identical attributes and firmware does not do this. A non-zero `OnTransitionTime`
  doesn't help.
- **Inovelli switches turn bulbs off by dimming down.** After a switch turn-off, bulbs
  sit at `CurrentLevel = 1`. So **every turn-off destroys the preset**, and the
  integration must re-apply the preset right after each turn-off.
- **`OnLevel = 255` means null.** `OnLevel` is a nullable uint8, so 255 means "use the
  previous level", and the real max is 254. The owner's existing automation computes
  `brightness_pct * 2.55` and writes 255 at 100%, which silently clears `OnLevel`.
  Clamp every level to 1–254.
- **Zigbee reports differ.** Other users report that IKEA Zigbee bulbs often ignore
  brightness while off, and that a persistent `Options` attribute gets "forgotten"
  after weeks. The integration therefore uses **per-command `optionsMask` /
  `optionsOverride`** and never writes the persistent `Options` attribute. Zigbee/ZHA
  support is out of scope for v1.
- **Not yet tested:** KAJPLATS GU10 CWS (full color), Nanoleaf Essentials A19/GU10,
  Inovelli switch load endpoints. The integration must detect support per light
  (see "Capability detection").

## Owner's environment

- Home Assistant OS, Core **2026.9.4**, Python 3.14.
- Matter Server app (formerly add-on) **9.2.0**: matter.js based, `matter-server/1.4.0
  (matter.js/0.17.9)`, WebSocket schema 13, port 5580. HA's Matter integration talks
  to it.
- Fabric index 2 is HA (controller node 112233). Devices are also on a Google fabric.
- Adaptive Lighting (HACS custom integration) with several instances. The one driving
  IKEA/Matter lights is:
  - switch: `switch.ikea_matter_adaptive_lights_adaptive_lighting_ikea_matter_adaptive_lights`
  - attributes used as source: `brightness_pct` (float 0–100) and
    `color_temp_kelvin` (int)
  - settings: `take_over_control: true`, `detect_non_ha_changes: true`,
    `initial_transition: 3`, `interval: 300`
- Existing automation `automation.adaptive_lighting_sync_matter_on_levels` copies
  Adaptive Lighting brightness into every Matter/ZHA `number.*_on_level` entity every
  30 min. It has the 255 bug above. **This integration is meant to replace it** for
  the lights it manages. Those lights must have `OnLevel` null, otherwise the flash
  returns.
- Matter group bindings are in place: each Inovelli switch EP2 is bound to a group
  for clusters 8 then 6, and the bulbs are group members. Groups:
  - 2 = Living room: switch 7, bulbs 72, 79, 82, 83, 84 (IKEA E12) + 23, 41 (Nanoleaf)
  - 3 = Dining: switch 3, bulbs 27, 28, 29
  - 4 = Kitchen ceiling: switch 9, bulbs 86, 88, 90, 92 (IKEA GU10)
  - 5 = Master bedroom: switch 30, bulbs 25, 26, 105, 106 (107 pending)
  - 6 = Large bedroom: switch 99, bulbs 93, 94, 96, 97, 100 (95 pending)

  **The integration must not touch bindings, groups, ACLs or group keys.**
- Good first test target: `light.branch_lamp` (a light group of the 5 E12 bulbs
  `light.branch_lamp_light_bulb_[1-5]_living_room`, Matter nodes 72, 79, 82, 83, 84).

## Matter protocol details

All on the light's endpoint (usually 1):

| What | Cluster | ID |
|---|---|---|
| OnOff (attribute) | OnOff 0x0006 | attr 0x0000 |
| CurrentLevel | LevelControl 0x0008 | attr 0x0000 |
| MinLevel / MaxLevel | LevelControl | attr 0x0002 / 0x0003 |
| Options (persistent, **do not write**) | LevelControl | attr 0x000F |
| OnLevel (nullable, must be null) | LevelControl | attr 0x0011 |
| MoveToLevel (**not** MoveToLevelWithOnOff) | LevelControl | cmd 0x00 |
| ColorMode | ColorControl 0x0300 | attr 0x0008 |
| ColorTemperatureMireds | ColorControl | attr 0x0007 |
| Options (persistent, **do not write**) | ColorControl | attr 0x000F |
| ColorTempPhysicalMin / Max Mireds | ColorControl | attr 0x400B / 0x400C |
| ColorCapabilities | ColorControl | attr 0x400A |
| MoveToColorTemperature | ColorControl | cmd 0x0A |

Command payloads that worked through the Matter Server WebSocket API
(`device_command`, matter.js field names):

```json
{"command": "device_command", "args": {"node_id": 72, "endpoint_id": 1, "cluster_id": 8,
 "command_name": "MoveToLevel",
 "payload": {"level": 64, "transitionTime": 0, "optionsMask": 1, "optionsOverride": 1}}}

{"command": "device_command", "args": {"node_id": 72, "endpoint_id": 1, "cluster_id": 768,
 "command_name": "MoveToColorTemperature",
 "payload": {"colorTemperatureMireds": 204, "transitionTime": 0, "optionsMask": 1, "optionsOverride": 1}}}
```

`optionsMask` / `optionsOverride` bit 0 = ExecuteIfOff. Mireds = round(1,000,000 / K),
clamped to the bulb's physical min/max.

### How to send commands from inside HA (verify against current core source)

Prefer reusing HA's own Matter integration connection over opening a second WebSocket:

- `homeassistant/components/matter/` keeps a `MatterAdapter` per config entry, holding
  the `MatterClient` (python `matter_server` client). Look at `helpers.py` for:
  - `get_matter(hass)` or equivalent
  - `get_node_from_device_entry(hass, device)` to map an HA device to a Matter node
  - the entity's endpoint
- The python client's `send_device_command(node_id, endpoint_id, command)` takes CHIP
  cluster command objects, e.g.
  `clusters.LevelControl.Commands.MoveToLevel(level=..., transitionTime=0, optionsMask=1, optionsOverride=1)`
  and `clusters.ColorControl.Commands.MoveToColorTemperature(...)`. Check how
  `homeassistant/components/matter/light.py` builds its commands and mirror it.
- **Internal APIs can change between HA releases.** Isolate all Matter access in one
  module (`matter_backend.py`) so a breakage is a one-file fix. If reusing the
  internal adapter proves too fragile, fall back to a direct WebSocket client to the
  Matter Server URL from the Matter config entry, using the JSON shown above.
- Read attributes from the node's cached attribute map (kept live by the server's
  subscriptions) to verify results. Attribute paths look like `"1/8/0"`.

## Features

### v1 (MVP)

1. **Action `preset_while_off.apply`**
   - target: `light` entities, areas, or light groups. Expand groups to member entities.
   - fields: `brightness_pct` (1–100) and/or `color_temp_kelvin`, both optional, at
     least one required.
   - For each resolved light:
     - Matter light that is **off**: send the commands with ExecuteIfOff.
     - Light that is **on**: do nothing by default. Option `apply_if_on: true` calls
       `light.turn_on` with the values instead.
     - Non-Matter light: skip and log at debug level.
   - Return a response (`supports_response=SupportsResponse.OPTIONAL`) listing
     applied / skipped / unsupported lights.
2. **"Follow a source" config entries** (config flow + options flow, one entry per
   rule):
   - lights: entity selector (multiple, light domain, groups allowed)
   - source type:
     - an Adaptive Lighting switch, reading its `brightness_pct` and
       `color_temp_kelvin` attributes
     - or separate entities for brightness (%) and color temp (K)
   - toggles: sync brightness, sync color temp
   - Re-apply to every managed light that is off when any of these happens:
     - the source changes (debounce ~2 s)
     - **a managed light turns off** (state `on` → `off`, wait ~1.5 s for the switch's
       dim-down to finish, then re-apply)
     - HA starts
     - a periodic safety interval (default 10 min)
   - Option "clear OnLevel on managed lights", default on: write `OnLevel = null` once
     at setup and after node re-interview, and warn if something else (like the old
     automation) keeps rewriting it.
3. **Capability detection** per light, cached in the config entry or a `Store`:
   - First apply: read back `OnOff`, `CurrentLevel`, `ColorTemperatureMireds` ~1 s
     after sending.
   - Value changed while off: mark `supported`.
   - Value unchanged: mark `unsupported_level` / `unsupported_ct` and stop sending
     that part.
   - Expose the result in the diagnostics download and as a repair issue listing
     unsupported lights.

### Later (not v1)

- Per-light entities: `number.<light>_preset_brightness` and
  `number.<light>_preset_color_temp`, showing and editing the preset on dashboards.
- Full color (xy/hs) presets via `MoveToColorXY` / `MoveToHueAndSaturation`, for
  GU10 CWS and Nanoleaf.
- ZHA backend, using the same ZCL options fields.
- Fallback for bulbs without ExecuteIfOff support: write `OnLevel` (clamped 1–254) for
  brightness only.

## Behavior rules / edge cases

- Clamp level to `max(MinLevel, 1)`–`min(MaxLevel, 254)`. **Never send 255.**
- Clamp mireds to the physical min/max. Only send color temp to lights whose
  `ColorCapabilities` include color temperature (bit 4).
- Send commands sequentially with ~150–200 ms spacing per bulb. Thread mesh: 30+ bulbs
  is fine but don't burst.
- Skip lights that are `unavailable`. Retry them when they come back.
- Never send `MoveToLevelWithOnOff`, `On`, `Toggle`, or anything that changes `OnOff`.
  The integration must **never turn a light on or off**.
- Never write the persistent `Options` attributes. Use per-command
  `optionsMask` / `optionsOverride` only.
- After applying, HA still shows the light as off with no brightness or color. That's
  expected. Don't try to fake state on the light entity.
- Expect Adaptive Lighting to adjust a light right after turn-on. If the preset
  matches its curve, the adjustment is a no-op (no visible fade). That's the goal.

## Repo layout (HACS)

```
custom_components/preset_while_off/
  __init__.py          # setup, service registration, entry setup/unload
  manifest.json        # domain, name, version, dependencies: ["matter"], iot_class: local_push
  config_flow.py       # config + options flow ("follow a source" rules)
  const.py
  matter_backend.py    # ONLY place that touches the Matter integration / server
  preset.py            # resolve targets, clamp, apply, capability detection
  coordinator.py       # per-entry listeners: source changes, light turn-off, interval
  services.yaml
  strings.json
  translations/en.json
  diagnostics.py
hacs.json
README.md
tests/                 # pytest-homeassistant-custom-component
```

## Development & testing

- Python 3.14 to match the owner's HA. Use `pytest-homeassistant-custom-component`
  pinned to the HA 2026.9 line. Mock `matter_backend` in unit tests.
- Unit-test: target resolution (groups, areas), clamping (255 → 254, K → mireds and
  min/max), the turn-off re-apply timing, capability detection logic, and the
  "never changes OnOff" invariant (assert no On/Off/WithOnOff commands are ever
  built).
- Manual test on the owner's HA, branch lamp:
  1. Confirm the lamp is off.
  2. Call the action with e.g. 25% and 4890 K.
  3. Verify through diagnostics or the Matter Server that `CurrentLevel` = 64 and
     mireds = 204 while `OnOff` = false.
  4. **The owner** turns the lamp on with the wall switch: no flash, correct level.
- Install for testing: copy `custom_components/preset_while_off` to `/config/custom_components/`
  (the owner has the Samba NAS and File editor apps), restart HA, add the integration.

## Hard rules for any session working on this

- **Never turn lights on or off yourself.** The owner runs every physical test. In
  particular, **never toggle lights when the owner's wife is home but the owner is
  not** (their standing preference).
- Don't modify Matter bindings, groups, ACLs, group keys or switch parameters.
- Before changing anything in the owner's HA (automations, Adaptive Lighting
  settings, `OnLevel` values), state the exact change and keep a way to undo it.
- When the integration replaces the old automation, **disable**
  `automation.adaptive_lighting_sync_matter_on_levels` for the managed lights rather
  than deleting it. The owner decides. The owner already knows about its 255 bug;
  the fix is to clamp with `[target, 254] | min`.

## Open questions to settle early

- Does reusing HA's internal Matter adapter work cleanly from a custom integration on
  2026.9, or is a direct WebSocket client more robust?
- GU10 CWS and Nanoleaf: do they honor ExecuteIfOff for level and color temp? Run the
  capability check.
- Should the action also accept `brightness` (0–255) and `color_temp_kelvin` aliases
  that match `light.turn_on` naming? Probably yes, for familiarity.
- Name: `preset_while_off` vs something shorter before publishing to HACS.
