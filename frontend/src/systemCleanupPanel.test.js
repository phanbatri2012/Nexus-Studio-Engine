import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'


test('wires bounded and observable system cleanup into Job Center', async () => {
  const jobCenterSource = await readFile(new URL('./JobCenter.jsx', import.meta.url), 'utf8')
  const cleanupSource = await readFile(new URL('./SystemCleanupPanel.jsx', import.meta.url), 'utf8')
  const securityFetchSource = await readFile(new URL('./securityFetch.js', import.meta.url), 'utf8')

  assert.match(jobCenterSource, /import SystemCleanupPanel from ['"]\.\/SystemCleanupPanel\.jsx['"]/)
  assert.match(jobCenterSource, /<SystemCleanupPanel\s*\/>/)
  assert.match(cleanupSource, /api\/system\/cleanup\/status/)
  assert.match(cleanupSource, /api\/system\/cleanup\/stats/)
  assert.match(cleanupSource, /api\/system\/cleanup\/run\?vacuum=true/)
  assert.match(cleanupSource, /api\/system\/cleanup\/runs\//)
  assert.match(cleanupSource, /MAX_CLEANUP_POLLS = 120/)
  assert.match(cleanupSource, /STATUS_TIMEOUT_MS = 30000/)
  assert.match(cleanupSource, /CLEANUP_POLL_DEADLINE_MS = 5 \* 60 \* 1000/)
  assert.match(cleanupSource, /new AbortController\(\)/)
  assert.match(cleanupSource, /blocking_jobs/)
  assert.match(cleanupSource, /Dọn rác an toàn/)
  assert.match(cleanupSource, /thử lại mỗi 1 giờ/)
  assert.match(securityFetchSource, /signal: init\?\.signal/)
})
