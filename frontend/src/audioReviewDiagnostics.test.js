import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

test('audio review surfaces structured issue locations and source expansion', async () => {
  const reviewSource = await readFile(new URL('./AudioReviewPanel.jsx', import.meta.url), 'utf8')
  const jobCenterSource = await readFile(new URL('./JobCenter.jsx', import.meta.url), 'utf8')

  assert.match(reviewSource, /review\.review_required/)
  assert.match(reviewSource, /item\.paragraph_index \+ 1/)
  assert.match(reviewSource, /item\.preview/)
  assert.match(reviewSource, /metrics\.source_expansion_ratio/)
  assert.match(reviewSource, /item\.code === 'excessive_source_expansion'/)
  assert.match(reviewSource, /Xác nhận dung lượng và tạo audio/)
  assert.match(jobCenterSource, /job\.review_required/)
  assert.match(jobCenterSource, /Xem \{job\.review_required\.length\} vấn đề cần sửa/)
})

test('standalone narration revision remains visible without an audio review', async () => {
  const reviewSource = await readFile(new URL('./AudioReviewPanel.jsx', import.meta.url), 'utf8')

  assert.match(reviewSource, /if \(!review && !bodyRevision\) return null/)
  assert.doesNotMatch(reviewSource, /if \(!review\) return null/)
  assert.match(reviewSource, /bodyRevision\.can_apply/)
  assert.match(reviewSource, /Dùng lời đọc mới/)
})
