import assert from 'node:assert/strict'
import { test } from 'node:test'
import { gzipSync } from 'node:zlib'
import { fetchPascalPackage } from '../frontend/pascal-package.mjs'

test('loads the selected package through a signed HA route', async () => {
  const room = { version: 1, graph: { nodes: { sample: { type: 'item' } } }, assets: {} }
  const png = Buffer.from('89504e470d0a1a0a', 'hex')
  const payload = gzipSync(JSON.stringify(room))
  const header = Buffer.alloc(8)
  header.writeUInt32BE(payload.length)
  header.write('psCl', 4)
  const bytes = Buffer.concat([png, header, payload, Buffer.alloc(4)])
  const hass = { callWS(message) {
    assert.equal(message.type, 'auth/sign_path')
    assert.equal(message.path, '/api/pascal_floorplan/full.png')
    return { path: message.path + '?authSig=fixture' }
  } }
  assert.deepEqual(await fetchPascalPackage(hass, 'full', { fetcher(path) {
    assert.equal(path, '/api/pascal_floorplan/full.png?authSig=fixture')
    return new Response(bytes)
  } }), room)
  await assert.rejects(fetchPascalPackage(hass, 'other'), /lite or full/)
  await assert.rejects(fetchPascalPackage(hass, 'full', {
    fetcher: () => new Response('not a package'),
  }), /Invalid Pascal package/)
})
