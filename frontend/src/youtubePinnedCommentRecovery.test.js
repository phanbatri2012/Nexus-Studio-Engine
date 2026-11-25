import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const jobCenterSource = readFileSync(new URL('./JobCenter.jsx', import.meta.url), 'utf8')
const backendSource = readFileSync(
  new URL('../../src/nexus_engine/main.py', import.meta.url),
  'utf8'
)

test('pinned-comment retry after posting is labeled as a pin retry', () => {
  assert.match(jobCenterSource, /job\.publish_stage === 'posted'/)
  assert.match(jobCenterSource, /Đang chờ ghim lại/)
  assert.match(jobCenterSource, /Tự ghim lại lúc/)
})

test('pinned-comment recovery limit comes from the backend retry table', () => {
  assert.match(
    backendSource,
    /len\(youtube_pinned_comment\.RETRY_DELAYS_SECONDS\)[\s\S]{0,120}job_type == "youtube_pinned_comment"/
  )
})
