import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

test('TTS settings exposes VieNeu catalog synchronization', async () => {
  const source = await readFile(new URL('./TTSSettings.jsx', import.meta.url), 'utf8')

  assert.match(source, /providers\/vieneu_v3_turbo\/sync/)
  assert.match(source, /Đồng bộ giọng VieNeu/)
})
