import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createFloorplanSync } from '../frontend/live-sync.mjs'

const base = Date.parse('2026-01-01T00:00:00Z')
const at = offset => new Date(base + offset).toISOString()
const entity = (state, offset, attributes = {}) => ({
  state, last_updated: at(offset), last_changed: at(offset), attributes,
})
const bindings = {
  devices: [
    { entity: 'light.kitchen', node: 'lamp_1', property: 'lit', states: { on: true, off: false } },
    { entity: 'binary_sensor.entry', node: 'door_1', property: 'open', states: { on: true, off: false } },
  ],
  positions: [{
    node: 'vacuum_1',
    sources: [
      { entity: 'sensor.map_frame', timestamp: 'observed_at', match: { map_flag: 1 } },
      { entity: 'sensor.fast_position', timestamp: 'observed_at', maxAgeMs: 10_000 },
    ],
    matrix: [[.001, 0, -2], [0, .001, -3]],
    map: { entity: 'select.floor', state: 'First floor' },
    dock: { point: [2, .024, 3], when: [
      { entity: 'vacuum.cleaner', states: ['docked'] },
      { entity: 'sensor.cleaner_status', states: ['charging'] },
    ] },
  }],
}

test('pushes real device state to its model node and does not regress to an older HA snapshot', () => {
  const output = []
  const sync = createFloorplanSync(bindings, patch => output.push(patch))
  const initial = { 'light.kitchen': entity('off', 0), 'binary_sensor.entry': entity('on', 0) }
  sync.applySnapshot(initial)
  assert.deepEqual(output.filter(p => p.kind === 'device').map(p => [p.node, p.value]), [
    ['lamp_1', false], ['door_1', true],
  ])
  output.length = 0
  sync.applyEvent({ data: { entity_id: 'light.kitchen', new_state: entity('on', 10) } })
  sync.applySnapshot(initial)
  assert.deepEqual(output, [{ kind: 'device', node: 'lamp_1', property: 'lit', available: true, value: true }])
  sync.applyEvent({ time_fired: at(20), data: { entity_id: 'light.kitchen', new_state: null } })
  sync.applySnapshot({ ...initial, 'light.kitchen': entity('on', 10) })
  assert.equal(output.at(-1).available, false)
  assert.equal(output.length, 2)
})

test('moves on fresh coordinates, rejects old or wrong-map data, and restores the dock pose', () => {
  const output = []
  let now = base
  const sync = createFloorplanSync(bindings, patch => output.push(patch), { clock: () => now })
  const update = (id, state, offset, attributes) => {
    now = Math.max(now, base + offset)
    sync.applyEvent({ time_fired: at(offset), data: { entity_id: id, new_state: entity(state, offset, attributes) } })
  }
  const point = (id, offset, x, y, flag = 1, witnessed = offset) =>
    update(id, 'ready', offset, { x, y, map_flag: flag, observed_at: at(witnessed) })
  update('select.floor', 'First floor', 0)
  update('vacuum.cleaner', 'docked', 10)
  assert.deepEqual(output.at(-1).point, [2, .024, 3])
  assert.equal(output.at(-1).docked, true)
  point('sensor.map_frame', 20, 6000, 7000)
  update('vacuum.cleaner', 'cleaning', 30)
  assert.equal(output.filter(p => p.kind === 'position').length, 1)
  point('sensor.fast_position', 40, 5000, 6000)
  assert.deepEqual(output.at(-1).point, [3, 0, 3])
  point('sensor.map_frame', 45, 8000, 8000, 1, 35)
  point('sensor.map_frame', 50, 8000, 8000, 2)
  assert.deepEqual(output.at(-1).point, [3, 0, 3])
  point('sensor.map_frame', 60, 7000, 9000)
  assert.deepEqual(output.at(-1).point, [5, 0, 6])
  update('select.floor', 'Second floor', 70)
  point('sensor.fast_position', 80, 9000, 9000)
  assert.deepEqual(output.at(-1).point, [5, 0, 6])
  update('sensor.cleaner_status', 'charging', 90)
  assert.deepEqual(output.at(-1).point, [2, .024, 3])
})

test('subscribes only while active and applies HA position events without polling', async () => {
  const controller = new AbortController()
  const output = []
  const sync = createFloorplanSync(bindings, patch => output.push(patch), { clock: () => base })
  let callback, unsubscribed = 0
  await sync.subscribe({
    subscribeEvents(handler, type) {
      assert.equal(type, 'state_changed')
      callback = handler
      return Promise.resolve(() => unsubscribed++)
    },
  }, controller.signal)
  callback({ data: { entity_id: 'select.floor', new_state: entity('First floor', 0) } })
  callback({ data: { entity_id: 'sensor.map_frame', new_state: entity('ready', 0, {
    x: 2500, y: 5000, map_flag: 1, observed_at: at(0),
  }) } })
  assert.deepEqual(output.at(-1).point, [.5, 0, 2])
  controller.abort()
  callback({ data: { entity_id: 'light.kitchen', new_state: entity('on', 1) } })
  assert.equal(unsubscribed, 1)
  assert.equal(output.at(-1).kind, 'position')
})
