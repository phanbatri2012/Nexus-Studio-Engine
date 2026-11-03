import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'


test('wires Google Flow into navigation', async () => {
  const appSource = await readFile(new URL('./App.jsx', import.meta.url), 'utf8')

  assert.match(appSource, /import GoogleFlowLogin from ['"]\.\/GoogleFlowLogin['"]/)
  assert.match(appSource, />Google Flow<\/li>/)
  assert.match(appSource, /activeView === ['"]flowlogin['"]/)
  assert.match(appSource, /<GoogleFlowLogin\s*\/>/)
})


test('keeps unsaved credentials while status polling runs', async () => {
  const source = await readFile(new URL('./GoogleFlowLogin.jsx', import.meta.url), 'utf8')

  assert.match(source, /fetchStatus\(true\)/)
  assert.match(source, /email: hydrateCredentials \? \(accData\.email \|\| ''\) : prev\.email/)
  assert.match(source, /password: hydrateCredentials \? '' : prev\.password/)
  assert.match(source, /totp_secret: hydrateCredentials \? '' : prev\.totp_secret/)
})


test('validates API responses and renders failures as errors', async () => {
  const source = await readFile(new URL('./GoogleFlowLogin.jsx', import.meta.url), 'utf8')
  const styles = await readFile(new URL('./AutoLogin.css', import.meta.url), 'utf8')

  assert.match(source, /if \(!response\.ok\)/)
  assert.match(source, /placeholder="Nhập email Google"/)
  assert.match(source, /className=\{`result \$\{resultType\}`\}/)
  assert.doesNotMatch(source, /disabled=\{!isConnected\}/)
  assert.match(styles, /\.result\.error\s*\{[\s\S]*color: #e74c3c/)
})


test('shows Google Flow on-demand and idle shutdown states', async () => {
  const source = await readFile(new URL('./GoogleFlowLogin.jsx', import.meta.url), 'utf8')

  assert.match(source, /Chờ tác vụ/)
  assert.match(source, /Chờ tự tắt/)
  assert.match(source, /tự tắt sau 5 phút rảnh/)
  assert.match(source, /browserStatus\?\.in_use \|\| browserStatus\?\.generation_active/)
})


test('provides fleet multi-account management and per-model quota checks', async () => {
  const source = await readFile(new URL('./GoogleFlowLogin.jsx', import.meta.url), 'utf8')

  // Check Quota Buttons
  assert.match(source, /⚡ Kiểm tra hạn mức/)
  assert.match(source, /⚡ Check hạn mức tất cả/)

  // Fleet Endpoints
  assert.match(source, /\/api\/flow\/accounts/)
  assert.match(source, /\/api\/flow\/check-quota/)

  // Model & Agent coverage
  assert.match(source, /Flow Creative Agent/)
  assert.match(source, /Nano Banana 2\.1/)
  assert.match(source, /Omni 1\.1 Flash/)
  assert.match(source, /Veo 3\.1 – Quality/)
})
