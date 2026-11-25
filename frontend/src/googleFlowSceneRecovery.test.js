import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const appSource = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const jobCenterSource = readFileSync(new URL('./JobCenter.jsx', import.meta.url), 'utf8')

test('video screen renders structured Flow scene recovery actions', () => {
  assert.match(appSource, /flowAttentionDetails = renderInfo\?\.job\?\.attention_details/)
  assert.match(appSource, /data-testid="flow-scene-attention-card"/)
  assert.match(appSource, /handleRetryFailedFlowScenes/)
  assert.match(appSource, /tối đa 5 lượt gửi Google Flow/)
  assert.match(appSource, /Thử lại cảnh/)
  assert.match(appSource, /flowAttentionDetails\.can_retry_new_canvas/)
  assert.match(appSource, /Tạo lại từ cảnh\.\.\./)
})

test('Job Center uses the same structured retry action instead of generic resume', () => {
  assert.match(jobCenterSource, /job\.attention_details\?\.kind === 'flow_scene_failure'/)
  assert.match(jobCenterSource, /data-testid="flow-scene-attention-card"/)
  assert.match(jobCenterSource, /job\.can_retry_failed_scenes/)
  assert.match(jobCenterSource, /runAction\(job, 'resume'\)/)
  assert.match(jobCenterSource, /tối đa 5 lượt gửi Google Flow/)
  assert.match(jobCenterSource, /job\.can_flow_reset_project/)
})
