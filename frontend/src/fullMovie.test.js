import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'


test('wires Full Movie into the Nexus route, sidebar and view', async () => {
  const appSource = await readFile(new URL('./App.jsx', import.meta.url), 'utf8')
  const routerSource = await readFile(new URL('./router.js', import.meta.url), 'utf8')

  assert.match(appSource, /import FullMovie from ['"]\.\/FullMovie['"]/)
  assert.match(appSource, /activeView === ['"]fullmovie['"]/)
  assert.match(appSource, />Ghép phim Full<\/li>/)
  assert.match(appSource, /<FullMovie\s*\/>/)
  assert.match(routerSource, /canonicalPath:\s*['"]\/full-movie['"]/)
  assert.match(routerSource, /['"]\/ghep-phim['"]/)
})


test('implements scan, automatic translation and durable job controls', async () => {
  const source = await readFile(new URL('./FullMovie.jsx', import.meta.url), 'utf8')

  assert.match(source, /api\/full-movie\/select-folder/)
  assert.match(source, /api\/full-movie\/scan/)
  assert.match(source, /api\/full-movie\/jobs/)
  assert.match(source, /api\/full-movie\/jobs\/latest/)
  assert.match(source, /ordered_source_names/)
  assert.match(source, /translation_proposal/)
  assert.match(source, /episodes\.map/)
  assert.match(source, /draggable=/)
  assert.match(source, /api\/jobs\/\$\{job\.id\}\/\$\{action\}/)
  assert.match(source, /AUTO_RESUME_REASONS/)
  assert.match(source, /Bắt đầu dịch & ghép tự động/)
  assert.match(source, /1920×1080 · 25 fps/)
})


test('implements automatic local movie thumbnail workflow', async () => {
  const source = await readFile(new URL('./FullMovie.jsx', import.meta.url), 'utf8')
  const styles = await readFile(new URL('./FullMovie.css', import.meta.url), 'utf8')
  const jobCenter = await readFile(new URL('./JobCenter.jsx', import.meta.url), 'utf8')

  assert.match(source, /api\/full-movie\/thumbnail\/select-video/)
  assert.match(source, /api\/full-movie\/thumbnail\/jobs\/latest/)
  assert.match(source, /title:\s*thumbnailTitle\.trim\(\)/)
  assert.match(source, /movie_path:\s*thumbnailPath\.trim\(\)/)
  assert.match(source, /thumbnail_job_id/)
  assert.match(source, /Tạo 3 thumbnail/)
  assert.match(source, /PRIMARY/)
  assert.match(source, /thumbnailJob\.result\.variants\.map/)
  assert.match(styles, /\.thumbnail-gallery/)
  assert.match(jobCenter, /value="full_movie_thumbnail">Tạo thumbnail phim/)
})
