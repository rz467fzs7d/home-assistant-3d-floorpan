# Home Assistant 3D Floor Plan

A Home Assistant custom integration and frontend toolkit for loading Pascal
3D floor plans, reflecting device states, and moving tracked models from
Home Assistant position updates. The toolkit plugs into a compatible renderer;
it is not a floor plan editor or a ready-made Lovelace card.

## Model Source

Install `custom_components/pascal_floorplan` under your Home Assistant
configuration directory. Restart Core for first discovery, then choose
**3D Floor Plan** in **Settings > Devices & services > Add integration**.
Requires Home Assistant 2026.11 or newer.

| Source | Setup |
| --- | --- |
| **Local models** | Put `lite.png` and `full.png` in `floorplan_models/` under the HA configuration directory, outside `www/`. Replace them by renaming completed files into place. |
| **Remote Pascal publisher** | Enter the publisher origin and dedicated read token. Use HTTPS for remote hosts; unencrypted HTTP is limited to private IPv4 addresses. |

Use **Reconfigure** to switch sources or update the address/token. Cards use
the same HA routes with either source:
`/api/pascal_floorplan/lite.png` and `/api/pascal_floorplan/full.png`.
Both require HA auth (or temporary signed URLs). The remote token stays on
the HA server. Remote mode streams on demand without polling or caching model
files on HA. Any authenticated HA user can read the configured floor plan.

### Publisher contract

Serve `GET /lite.png` and `GET /full.png` using a bearer read token. Each
response is a Pascal PNG carrier with embedded `psCl` scene data, served as
`image/png` with a content length (up to 64 MiB). Redirects and content
encoding are not supported. A SHA-256 ETag and conditional GET are optional.
The publisher can run anywhere reachable from HA. Point the integration at the
publisher, not directly at the Pascal Editor: the editor's scene/artifact API
is used to build the packages, but is not a model download endpoint.

## Live State and Position

The modules in `frontend/` run in the browser. `fetchPascalPackage` loads the
selected scene through HA. `createFloorplanSync` binds HA entities to node IDs
provided by the renderer. It uses the initial `hass.states` snapshot plus
`state_changed` events; no extra device queries, pollers, or device controls.

```js
import { createFloorplanSync } from './live-sync.mjs'
import { fetchPascalPackage } from './pascal-package.mjs'

const lifecycle = new AbortController()
const scene = await fetchPascalPackage(hass, 'lite', { signal: lifecycle.signal })
// Mount the package in your renderer, then apply patches to its live nodes.
const sync = createFloorplanSync({
  devices: [
    { entity: 'light.kitchen', node: 'lamp_1', property: 'lit', states: { on: true, off: false } },
    { entity: 'binary_sensor.front_door', node: 'door_1', property: 'open', states: { on: true, off: false } },
  ],
  positions: [{
    node: 'robot_1',
    sources: [
      { entity: 'sensor.robot_map_frame', timestamp: 'observed_at', match: { map_flag: 1 } },
      { entity: 'sensor.robot_fast_position', timestamp: 'observed_at', maxAgeMs: 10_000 },
    ],
    map: { entity: 'select.robot_map', state: 'First floor' },
    matrix: [[0.001, 0, -2], [0, 0.001, -3]],
    dock: { point: [2, 0, 3], when: [{ entity: 'vacuum.robot', states: ['docked'] }] },
  }],
}, patch => renderer.apply(patch))

sync.applySnapshot(hass.states)
await sync.subscribe(hass.connection, lifecycle.signal)
// Apply later Lovelace snapshots with sync.applySnapshot(nextHass.states).
// Abort lifecycle when the card disconnects. Rebuild bindings for a new scene.
```

- **Device state:** a patch contains `node`, `property`, `value`, and
  `available`. Unavailable/unknown is distinct from off. You can map a
  single entity attribute instead of its primary state by specifying `attribute`.
- **Device position:** configure a sensor that publishes real numeric
  coordinates. For Roborock, this can be a map-frame position entity or a
  separate coordinate feed. The standard vacuum activity state alone does not
  contain coordinates. The companion does not extract a robot position from
  a map image or require any vendor-specific integration.
- **Calibration:** each matrix row transforms the two sensor coordinates
  into a scene axis (`X=a*x+b*y+c`, `Z=d*x+e*y+f`). Calibrate per model and map.
  Sources can have different coordinate attribute names (`x`, `y`) and
  freshness limits. Reject stale/out-of-order or mismatched map samples, retain
  the last valid pose when data drops out, and use the configured dock
  point on docking. A position patch contains `node`, `point` and `docked`;
  use it to update the actual model transform, not just a marker.

## Tests

With Python 3.14 and Node 22+:

```sh
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
node --test tests/*.test.mjs
```

Tests use temporary local files, a loopback publisher, an isolated HA
instance, and simulated device events. They do not contact a live home.
