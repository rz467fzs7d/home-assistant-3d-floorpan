const offline = new Set(['unknown', 'unavailable'])
const hasState = state => state && !offline.has(state.state)
const stamp = value => typeof value === 'number' && Number.isFinite(value)
  ? value : typeof value === 'string' ? Date.parse(value) : NaN

function exactMatch(object, values) {
  return Object.entries(values ?? {}).every(([key, value]) => Object.is(object[key], value))
}

function requireEntity(id) {
  if (!/^[a-z0-9_]+\.[a-z0-9_]+$/.test(id ?? '')) throw new Error('Expected a Home Assistant entity ID')
  return id
}

function nextPosition(binding, state, memory, now) {
  const docking = binding.dock?.when ?? []
  const docked = docking.some(rule => rule.states.includes(state(rule.entity)?.state))
  if (docked) {
    memory.docked = true
    return { point: binding.dock.point, docked: true }
  }
  if (memory.docked) {
    const transitions = docking.map(rule => stamp(state(rule.entity)?.last_changed)).filter(Number.isFinite)
    memory.departed = transitions.length ? Math.max(...transitions) : now
    memory.docked = false
  }

  const selectedMap = binding.map && state(binding.map.entity)
  if (binding.map && selectedMap?.state !== binding.map.state) return
  const mapChanged = stamp(selectedMap?.last_changed)
  let latest
  for (const source of binding.sources) {
    const sample = state(source.entity)
    if (!hasState(sample)) continue
    const attributes = sample.attributes ?? {}
    const x = attributes[source.x ?? 'x'], y = attributes[source.y ?? 'y']
    const at = stamp(source.timestamp ? attributes[source.timestamp] : sample.last_updated)
    if (!Number.isFinite(at) || now - at > (source.maxAgeMs ?? 120_000) || at - now > 5000 ||
        at <= memory.lastSample || at < memory.departed || at < mapChanged ||
        !Number.isFinite(x) || !Number.isFinite(y) || !exactMatch(attributes, source.match)) continue
    const [worldX, worldZ] = binding.matrix.map(([a, b, c]) => a * x + b * y + c)
    const height = binding.height ?? 0
    if (![worldX, height, worldZ].every(Number.isFinite)) continue
    if (binding.bounds) {
      const [minX, minZ, maxX, maxZ] = binding.bounds
      if (worldX < minX || worldX > maxX || worldZ < minZ || worldZ > maxZ) continue
    }
    if (!latest || at > latest.at) latest = { at, point: [worldX, height, worldZ] }
  }
  if (!latest) return
  memory.lastSample = latest.at
  return { point: latest.point, docked: false }
}

/** Turn HA snapshots and push events into renderer-neutral node updates. */
export function createFloorplanSync({ devices = [], positions = [] }, apply, { clock = Date.now } = {}) {
  const dependencies = new Map()
  const records = new Map()
  const cachedState = id => records.get(id)?.state
  let initialized = false

  function watch(ids, run) {
    for (const id of new Set(ids.map(requireEntity))) {
      if (!dependencies.has(id)) dependencies.set(id, new Set())
      dependencies.get(id).add(run)
    }
  }

  for (const binding of devices) {
    if (!binding.node || !binding.property || !binding.states) throw new Error('Invalid device binding')
    let last
    watch([binding.entity], () => {
      const state = cachedState(binding.entity)
      const key = binding.attribute ? state?.attributes?.[binding.attribute] : state?.state
      const available = Boolean(hasState(state) && key != null && Object.hasOwn(binding.states, key))
      const value = available ? binding.states[key] : null
      if (!last || last.available !== available || !Object.is(last.value, value)) {
        last = { available, value }
        apply({ kind: 'device', node: binding.node, property: binding.property, ...last })
      }
    })
  }

  for (const binding of positions) {
    const { matrix, sources, map, dock } = binding
    if (!binding.node || !sources?.length || !Array.isArray(matrix) || matrix.length !== 2 ||
        matrix.some(row => !Array.isArray(row) || row.length !== 3 || !row.every(Number.isFinite)) ||
        Math.abs(matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0]) < 1e-12 ||
        (dock && (!Array.isArray(dock.point) || dock.point.length !== 3 ||
          !dock.point.every(Number.isFinite) || !dock.when?.length))) {
      throw new Error('Invalid position calibration')
    }
    let last
    const memory = { docked: false, departed: -Infinity, lastSample: -Infinity }
    watch([...sources.map(source => source.entity), ...(map ? [map.entity] : []),
      ...(dock?.when ?? []).map(rule => rule.entity)], () => {
      const next = nextPosition(binding, cachedState, memory, clock())
      if (next && (!last || last.docked !== next.docked || next.point.some((n, i) => n !== last.point[i]))) {
        last = next
        apply({ kind: 'position', node: binding.node, ...next, point: [...next.point] })
      }
    })
  }

  function record(id, state, at, fromEvent) {
    const previous = records.get(id)
    if (previous && (previous.state === state || at < previous.at || (!fromEvent && at === previous.at))) {
      return false
    }
    records.set(id, { state, at })
    return true
  }

  function applyEvent(event) {
    const id = event?.data?.entity_id
    if (!dependencies.has(id) || !Object.hasOwn(event.data, 'new_state')) return
    const state = event.data.new_state
    const at = [stamp(state?.last_updated), stamp(event.time_fired)].find(Number.isFinite) ?? clock()
    if (record(id, state, at, true)) for (const run of dependencies.get(id)) run()
  }

  return {
    applySnapshot(states) {
      const pending = new Set()
      for (const [id, runs] of dependencies) {
        const state = states[id]
        const at = stamp(state?.last_updated)
        const changed = state && record(id, state, Number.isFinite(at) ? at : -Infinity, false)
        if (changed || !initialized) for (const run of runs) pending.add(run)
      }
      initialized = true
      for (const run of pending) run()
    },
    applyEvent,
    async subscribe(connection, signal) {
      if (signal.aborted) return
      const unsubscribe = await connection.subscribeEvents(event => {
        if (!signal.aborted) applyEvent(event)
      }, 'state_changed')
      if (signal.aborted) unsubscribe()
      else signal.addEventListener('abort', unsubscribe, { once: true })
    },
  }
}
