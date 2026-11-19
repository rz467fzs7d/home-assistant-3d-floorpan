const routes = { lite: '/api/floorplan_3d/lite.png', full: '/api/floorplan_3d/full.png' }

export async function fetchPascalPackage(hass, mode = 'lite', { signal, fetcher = fetch } = {}) {
  if (!Object.hasOwn(routes, mode)) throw new Error('Expected lite or full')
  const { path } = await hass.callWS({ type: 'auth/sign_path', path: routes[mode], expires: 180 })
  const response = await fetcher(path, { signal })
  if (!response.ok) throw new Error(`Floor plan package unavailable (HTTP ${response.status})`)
  const bytes = await response.arrayBuffer()
  const view = new DataView(bytes)
  if (view.byteLength < 20 || view.getUint32(0) !== 0x89504e47 || view.getUint32(4) !== 0x0d0a1a0a) {
    throw new Error('Invalid Pascal package')
  }
  for (let offset = 8; offset + 12 <= view.byteLength;) {
    const size = view.getUint32(offset)
    const next = offset + 12 + size
    if (next > view.byteLength) break
    if (view.getUint32(offset + 4) === 0x7073436c) {
      const chunk = new Blob([new Uint8Array(bytes, offset + 8, size)])
      const contents = new Response(chunk.stream().pipeThrough(new DecompressionStream('gzip')))
      const model = await contents.json()
      if (model.version === 1 && model.graph?.nodes && model.assets) return model
      throw new Error('Unsupported Pascal package')
    }
    offset = next
  }
  throw new Error('Pascal scene chunk not found')
}
