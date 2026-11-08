import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import './TTSSettings.css'

const API = 'http://127.0.0.1:8080/api/tts'
const OMNIVOICE_PREVIEW_MAX_CHARACTERS = 200
const OMNIVOICE_PREVIEW_STORAGE_KEY = 'nexus_omnivoice_preview_id'
const LEGACY_OMNIVOICE_PREVIEW_STORAGE_KEY = 'autoyt_omnivoice_preview_id'
const ACTIVE_PREVIEW_STATUSES = new Set(['queued', 'processing'])
const ACTIVE_GENERATION_STATUSES = new Set(['queued', 'processing', 'running'])

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, options)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(payload.detail || `HTTP ${response.status}`)
    error.status = response.status
    throw error
  }
  return payload
}

function providerLabel(providerId, providers) {
  return providers.find(provider => provider.id === providerId)?.display_name || providerId
}

function healthLabel(provider) {
  if (!provider.enabled) return 'Đã tắt'
  const health = provider.health || {}
  if (!health.ok) return health.state === 'unconfigured' ? 'Chưa cấu hình' : 'Chưa sẵn sàng'
  if (health.busy) return 'Đang tạo audio'
  if (provider.id === 'vieneu_v3_turbo') return 'Sẵn sàng · VieNeu API'
  if (health.state === 'loading') return 'Đang nạp model'
  if (health.model_loaded) return 'Model đã nạp'
  return provider.type === 'local' ? 'Đang nghỉ' : 'Hoạt động'
}

function previewStatusLabel(preview) {
  if (!preview) return ''
  if (preview.status === 'queued') return 'Đang chờ'
  if (preview.status === 'processing') return 'Đang tạo audio'
  if (preview.status === 'completed') return 'Hoàn thành'
  if (preview.status === 'canceled') return 'Đã hủy'
  return 'Tạo bản nghe thử thất bại'
}

function generationStatusLabel(status) {
  return {
    queued: 'Đang chờ', processing: 'Đang tạo', running: 'Đang tạo',
    paused: 'Đang tạm dừng', completed: 'Hoàn thành', failed: 'Thất bại', canceled: 'Đã hủy',
  }[status] || status
}

export default function TTSSettings() {
  const [providers, setProviders] = useState([])
  const [voices, setVoices] = useState([])
  const [defaultVoiceId, setDefaultVoiceId] = useState('')
  const [filter, setFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [newGenmax, setNewGenmax] = useState({ name: '', provider_voice_id: '' })
  const [sampleFile, setSampleFile] = useState(null)
  const [sampleName, setSampleName] = useState('')
  const [sampleStart, setSampleStart] = useState(0)
  const [sampleEnd, setSampleEnd] = useState(10)
  const [sampleDuration, setSampleDuration] = useState(0)
  const [sampleUrl, setSampleUrl] = useState('')
  const [sampleReferenceText, setSampleReferenceText] = useState('')
  const [previewVoiceId, setPreviewVoiceId] = useState('')
  const [previewText, setPreviewText] = useState('Xin chào, đây là bản nghe thử giọng local của Nexus Studio Engine.')
  const [preview, setPreview] = useState(null)
  const [generationTitle, setGenerationTitle] = useState('')
  const [generationText, setGenerationText] = useState('')
  const [generationVoiceId, setGenerationVoiceId] = useState('')
  const [generations, setGenerations] = useState([])
  const credentialRef = useRef(null)
  const previewRef = useRef(null)

  const load = useCallback(async () => {
    const [providerData, voiceData, generationData] = await Promise.all([
      api('/providers'), api('/voices'), api('/generations'),
    ])
    setProviders(providerData.items || [])
    setVoices(voiceData.items || [])
    setDefaultVoiceId(voiceData.default_voice_id || '')
    setGenerations(generationData.items || [])
  }, [])

  useEffect(() => { load().catch(err => setError(err.message)) }, [load])
  useEffect(() => () => {
    if (sampleUrl) URL.revokeObjectURL(sampleUrl)
  }, [sampleUrl])

  const previewVoices = useMemo(() => voices.filter(voice =>
    (voice.provider_id === 'omnivoice' && voice.status === 'active') ||
    (voice.provider_id === 'vieneu_v3_turbo' && ['active', 'draft'].includes(voice.status))
  ), [voices])
  const activeVieneuVoices = useMemo(() => voices.filter(voice =>
    voice.provider_id === 'vieneu_v3_turbo' && voice.status === 'active'
  ), [voices])

  useEffect(() => {
    if (!previewVoices.some(voice => voice.id === previewVoiceId)) {
      setPreviewVoiceId(previewVoices[0]?.id || '')
    }
  }, [previewVoices, previewVoiceId])
  useEffect(() => {
    if (!activeVieneuVoices.some(voice => voice.id === generationVoiceId)) {
      setGenerationVoiceId(activeVieneuVoices[0]?.id || '')
    }
  }, [activeVieneuVoices, generationVoiceId])

  const refreshPreview = useCallback(async previewId => {
    try {
      const result = await api(`/previews/${previewId}`)
      setPreview(result)
      setPreviewVoiceId(result.voice_id)
      setPreviewText(result.text)
      return result
    } catch (err) {
      if (err.status === 404) {
        localStorage.removeItem(OMNIVOICE_PREVIEW_STORAGE_KEY)
        localStorage.removeItem(LEGACY_OMNIVOICE_PREVIEW_STORAGE_KEY)
        setPreview(null)
        return null
      }
      setError(err.message)
      return null
    }
  }, [])

  useEffect(() => {
    const canonical = localStorage.getItem(OMNIVOICE_PREVIEW_STORAGE_KEY)
    const legacy = localStorage.getItem(LEGACY_OMNIVOICE_PREVIEW_STORAGE_KEY)
    const previewId = canonical || legacy
    if (!canonical && legacy) localStorage.setItem(OMNIVOICE_PREVIEW_STORAGE_KEY, legacy)
    if (legacy) localStorage.removeItem(LEGACY_OMNIVOICE_PREVIEW_STORAGE_KEY)
    if (previewId) refreshPreview(previewId)
  }, [refreshPreview])

  useEffect(() => {
    if (!preview || !ACTIVE_PREVIEW_STATUSES.has(preview.status)) return undefined
    const timer = setInterval(() => refreshPreview(preview.id), 2000)
    return () => clearInterval(timer)
  }, [preview, refreshPreview])
  useEffect(() => {
    if (!generations.some(item => ACTIVE_GENERATION_STATUSES.has(item.status))) return undefined
    const timer = setInterval(() => load().catch(err => setError(err.message)), 2500)
    return () => clearInterval(timer)
  }, [generations, load])

  const run = async (key, action, successMessage) => {
    setBusy(key); setError(''); setNotice('')
    try {
      const result = await action()
      setNotice(successMessage)
      await load()
      return result
    } catch (err) {
      setError(err.message)
      return null
    } finally {
      setBusy('')
    }
  }

  const visibleVoices = useMemo(() => voices.filter(voice => {
    if (filter === 'archived' && voice.status !== 'archived') return false
    if (filter !== 'all' && filter !== 'archived' && voice.provider_id !== filter) return false
    const query = search.trim().toLocaleLowerCase('vi')
    return !query || voice.name.toLocaleLowerCase('vi').includes(query) ||
      voice.provider_voice_id.toLocaleLowerCase('vi').includes(query)
  }), [filter, search, voices])

  const saveCredential = () => run('credential', async () => {
    const apiKey = credentialRef.current?.value?.trim() || ''
    if (!apiKey) throw new Error('Hãy nhập API key Genmax mới.')
    await api('/providers/genmax/credentials', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ api_key: apiKey }),
    })
    credentialRef.current.value = ''
  }, 'Đã thay thế API key Genmax an toàn.')

  const createGenmaxVoice = () => run('new-genmax', async () => {
    await api('/voices', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...newGenmax, provider_id: 'genmax', config: {} }),
    })
    setNewGenmax({ name: '', provider_voice_id: '' })
  }, 'Đã thêm giọng Genmax.')

  const patchVoice = (voice, changes, message) => run(`voice-${voice.id}`, () =>
    api(`/voices/${voice.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes),
    }), message)

  const patchCasting = (voice, changes) => patchVoice(voice, { casting_profile: {
    gender: 'any', age_group: 'any', languages: ['vi'], roles: ['guest'], styles: [],
    usage_modes: ['single_narrator'], voice_pools: [], ...(voice.casting_profile || {}), ...changes,
  } }, 'Đã cập nhật chính sách sử dụng giọng.')

  const toggleUsageMode = (voice, mode, enabled) => {
    const current = voice.casting_profile?.usage_modes || ['single_narrator']
    patchCasting(voice, { usage_modes: enabled ? [...new Set([...current, mode])] : current.filter(item => item !== mode) })
  }
  const editCastingTags = (voice, key, label) => {
    const raw = prompt(`${label} (phân tách bằng dấu phẩy):`, (voice.casting_profile?.[key] || []).join(', '))
    if (raw !== null) patchCasting(voice, { [key]: raw.split(',').map(value => value.trim().toLowerCase()).filter(Boolean) })
  }
  const testVoice = voice => {
    if (['omnivoice', 'vieneu_v3_turbo'].includes(voice.provider_id)) {
      setPreviewVoiceId(voice.id)
      previewRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      return
    }
    const billable = providers.find(item => item.id === voice.provider_id)?.capabilities?.billable
    if (billable && !confirm('Thử giọng Genmax có thể tốn credit. Tiếp tục?')) return
    run(`test-${voice.id}`, () => api(`/voices/${voice.id}/test`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm_billable: Boolean(billable) }),
    }), 'Đã gửi bản thử giọng.')
  }

  const chooseSample = event => {
    const file = event.target.files?.[0] || null
    if (sampleUrl) URL.revokeObjectURL(sampleUrl)
    setSampleFile(file); setSampleName(file ? file.name.replace(/\.[^.]+$/, '') : '')
    setSampleStart(0); setSampleEnd(10); setSampleDuration(0)
    setSampleUrl(file ? URL.createObjectURL(file) : '')
  }
  const cloneSample = () => run('clone-omni', async () => {
    if (!sampleFile) throw new Error('Hãy chọn file audio.')
    const duration = Number(sampleEnd) - Number(sampleStart)
    if (duration < 3 || duration > 10) throw new Error('Mẫu OmniVoice phải dài 3–10 giây.')
    if (!sampleReferenceText.trim()) throw new Error('Hãy nhập lời nói trong đoạn mẫu.')
    const form = new FormData()
    form.append('file', sampleFile); form.append('name', sampleName.trim())
    form.append('start_seconds', String(sampleStart)); form.append('end_seconds', String(sampleEnd))
    form.append('reference_text', sampleReferenceText.trim())
    await api('/providers/omnivoice/clone', { method: 'POST', body: form })
    setSampleFile(null); setSampleUrl(''); setSampleReferenceText('')
  }, 'Đã tạo profile OmniVoice.')

  const createPreview = async () => {
    const text = previewText.trim()
    if (!previewVoiceId || !text) { setError('Hãy chọn giọng và nhập nội dung nghe thử.'); return }
    setBusy('preview'); setError(''); setNotice('')
    try {
      const result = await api('/previews', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ voice_id: previewVoiceId, text, client_request_id: crypto.randomUUID() }),
      })
      localStorage.setItem(OMNIVOICE_PREVIEW_STORAGE_KEY, result.id)
      setPreview(result); setNotice('Đã đưa bản nghe thử vào hàng đợi.')
      await load()
    } catch (err) { setError(err.message) } finally { setBusy('') }
  }
  const cancelPreview = async () => {
    if (!preview || !ACTIVE_PREVIEW_STATUSES.has(preview.status)) return
    setBusy('preview-cancel')
    try { setPreview(await api(`/previews/${preview.id}/cancel`, { method: 'POST' })) }
    catch (err) { setError(err.message) } finally { setBusy('') }
  }

  const createGeneration = () => run('generation', async () => {
    if (!generationVoiceId) throw new Error('Chưa có giọng VieNeu active.')
    if (!generationText.trim()) throw new Error('Hãy nhập nội dung cần tạo audio.')
    await api('/generations', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: generationTitle.trim(), text: generationText.trim(), voice_id: generationVoiceId,
        client_request_id: crypto.randomUUID(),
      }),
    })
  }, 'Đã đưa audio VieNeu vào hàng đợi.')

  const activeCount = voices.filter(voice => voice.status === 'active').length
  const archivedCount = voices.filter(voice => voice.status === 'archived').length
  const queuedCount = providers.reduce((sum, provider) => sum + (provider.queued_jobs || 0), 0)

  return <div className="tts-page">
    <div className="tts-heading"><div><h1>Giọng đọc &amp; TTS</h1><p>Quản lý Genmax, OmniVoice; Nexus chỉ gọi VieNeu API để tạo audio.</p></div>
      <button className="tts-button" onClick={() => run('refresh', async () => {}, 'Đã làm mới.')}>↻ Làm mới</button></div>

    <div className="tts-summary"><span>🎙️ Hoạt động: <b>{activeCount}</b></span><span>📦 Lưu trữ: <b>{archivedCount}</b></span>
      <span>⏳ Job đang chờ: <b>{queuedCount}</b></span><label>Giọng mặc định chung
        <select value={defaultVoiceId} onChange={event => run('default', () => api('/voices/default', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ voice_id: event.target.value }),
        }), 'Đã đổi giọng mặc định chung.')}>
          {voices.filter(voice => voice.status === 'active').map(voice => <option key={voice.id} value={voice.id}>{providerLabel(voice.provider_id, providers)} · {voice.name}</option>)}
        </select></label></div>

    <div className="tts-provider-grid">{providers.map(provider => <article className={`tts-card ${provider.health?.ok ? 'healthy' : 'offline'}`} key={provider.id}>
      <div className="tts-card-title"><div><h2>{provider.display_name}</h2><span className="tts-provider-kind">
        {provider.id === 'vieneu_v3_turbo' ? 'Local API · model và giọng do VieNeu quản lý' : provider.type === 'local' ? 'Local · worker riêng' : 'Cloud · có credit'}
      </span></div><span className="tts-health">{healthLabel(provider)}</span></div>
      {provider.id === 'vieneu_v3_turbo' && <small>Clone, reference, revision và tuning được quản lý trực tiếp trong VieNeu.</small>}
      {provider.health?.error && <small className="tts-error-inline">{provider.health.error}</small>}
      <div className="tts-actions"><button className="tts-button" onClick={() => run(`provider-${provider.id}`, () => api(`/providers/${provider.id}/test`, { method: 'POST' }), `Đã kiểm tra ${provider.display_name}.`)}>Kiểm tra engine</button>
        <label className="tts-toggle"><input type="checkbox" checked={provider.enabled} onChange={event => run(`provider-${provider.id}`, () => api(`/providers/${provider.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: event.target.checked }),
        }), 'Đã cập nhật provider.')} /> Bật provider</label></div>
      {provider.id === 'genmax' && <div className="tts-provider-config"><label>API key<input ref={credentialRef} type="password" autoComplete="new-password" placeholder={provider.health?.configured ? '•••••••• (đã cấu hình)' : 'Nhập API key mới'} /></label><button className="tts-button primary" onClick={saveCredential}>Thay thế key</button><button className="tts-button danger" onClick={() => { if (confirm('Xóa API key Genmax đã lưu?')) run('credential-delete', () => api('/providers/genmax/credentials', { method: 'DELETE' }), 'Đã xóa API key Genmax.') }}>Xóa key</button></div>}
      {provider.id === 'omnivoice' && <button className="tts-button" onClick={() => run('sync', () => api('/providers/omnivoice/sync', { method: 'POST' }), 'Đã đồng bộ OmniVoice.')}>Đồng bộ profile .pt</button>}
    </article>)}</div>

    <section className="tts-card tts-preview" ref={previewRef}><div className="tts-card-title"><div><h2>Nghe thử OmniVoice và VieNeu</h2><span className="tts-provider-kind">Hỗ trợ OmniVoice active và VieNeu active/draft.</span></div>
      {preview && <span className={`tts-preview-status ${preview.status}`}>{previewStatusLabel(preview)}</span>}</div>
      {previewVoices.length ? <><label className="tts-preview-voice">Giọng<select value={previewVoiceId} disabled={preview && ACTIVE_PREVIEW_STATUSES.has(preview.status)} onChange={event => setPreviewVoiceId(event.target.value)}>
        {previewVoices.map(voice => <option key={voice.id} value={voice.id}>{providerLabel(voice.provider_id, providers)} · {voice.name}{voice.status === 'draft' ? ' · draft' : ''}</option>)}</select></label>
        <label className="tts-preview-text">Nội dung nghe thử<textarea rows="4" maxLength={OMNIVOICE_PREVIEW_MAX_CHARACTERS} value={previewText} onChange={event => setPreviewText(event.target.value)} /><small>{[...previewText].length}/200 ký tự</small></label>
        <div className="tts-actions"><button className="tts-button primary" disabled={busy === 'preview' || (preview && ACTIVE_PREVIEW_STATUSES.has(preview.status))} onClick={createPreview}>Tạo bản nghe thử</button>
          {preview && ACTIVE_PREVIEW_STATUSES.has(preview.status) && <button className="tts-button danger" onClick={cancelPreview}>Hủy</button>}</div>
        {preview?.status === 'completed' && <div className="tts-preview-result"><audio controls src={preview.audio_url} /><a className="tts-button" href={`${preview.audio_url}?download=1`}>Tải WAV</a><small>{preview.duration_seconds?.toFixed(1)} giây</small></div>}
        {preview?.status === 'failed' && <div className="tts-error-inline">{preview.error}</div>}</> : <p className="tts-empty">Chưa có giọng local để nghe thử.</p>}
    </section>

    <section className="tts-card tts-generation"><div className="tts-card-title"><div><h2>Tạo audio VieNeu</h2><span className="tts-provider-kind">Sinh audio đầy đủ độc lập với project video.</span></div></div>
      {activeVieneuVoices.length ? <div className="tts-generation-form"><input value={generationTitle} onChange={event => setGenerationTitle(event.target.value)} placeholder="Tên audio (tùy chọn)" />
        <select value={generationVoiceId} onChange={event => setGenerationVoiceId(event.target.value)}>{activeVieneuVoices.map(voice => <option key={voice.id} value={voice.id}>{voice.name} · r{voice.active_revision}</option>)}</select>
        <textarea rows="7" maxLength="10000" value={generationText} onChange={event => setGenerationText(event.target.value)} placeholder="Nội dung cần tạo audio…" /><small>{generationText.length}/10.000 ký tự</small>
        <button className="tts-button primary" disabled={busy === 'generation'} onClick={createGeneration}>Tạo audio</button></div> : <p className="tts-empty">Chưa có giọng VieNeu active. Có thể preview draft và kích hoạt revision ở danh mục giọng.</p>}
      <div className="tts-generation-list">{generations.map(item => <div className="tts-generation-item" key={item.id}><div><strong>{item.title || item.voice_name}</strong><small>{item.voice_name} · r{item.voice_revision} · {generationStatusLabel(item.status)}</small><span>{item.progress}</span></div>
        <div className="tts-actions">{item.status === 'completed' && <><audio controls src={item.audio_url} /><a className="tts-button" href={`${item.audio_url}?download=1`}>Tải WAV</a></>}
          {ACTIVE_GENERATION_STATUSES.has(item.status) && <button className="tts-button danger" onClick={() => run(`cancel-${item.id}`, () => api(`/generations/${item.id}/cancel`, { method: 'POST' }), 'Đã yêu cầu hủy.')}>Hủy</button>}
          {['failed', 'canceled', 'paused'].includes(item.status) && <button className="tts-button" onClick={() => run(`retry-${item.id}`, () => api(`/generations/${item.id}/retry`, { method: 'POST' }), 'Đã đưa job vào hàng đợi lại.')}>Tiếp tục</button>}
          {!ACTIVE_GENERATION_STATUSES.has(item.status) && <button className="tts-button danger" onClick={() => { if (confirm('Xóa job và audio này?')) run(`delete-${item.id}`, () => api(`/generations/${item.id}`, { method: 'DELETE' }), 'Đã xóa audio.') }}>Xóa</button>}</div>
        {item.error && <small className="tts-error-inline">{item.error}</small>}</div>)}</div>
    </section>

    <section className="tts-card tts-create-grid"><div><h2>Thêm giọng Genmax</h2><input value={newGenmax.name} onChange={event => setNewGenmax(value => ({ ...value, name: event.target.value }))} placeholder="Tên hiển thị" /><input value={newGenmax.provider_voice_id} onChange={event => setNewGenmax(value => ({ ...value, provider_voice_id: event.target.value }))} placeholder="Voice ID Genmax" /><button className="tts-button primary" onClick={createGenmaxVoice}>＋ Thêm giọng</button></div>
      <div><h2>Tạo profile OmniVoice</h2><input type="file" accept="audio/*,.wav,.mp3,.flac,.ogg" onChange={chooseSample} />{sampleUrl && <audio controls src={sampleUrl} onLoadedMetadata={event => { const duration = event.currentTarget.duration; if (Number.isFinite(duration)) { setSampleDuration(duration); setSampleEnd(Math.min(10, duration)) } }} />}
        <input value={sampleName} onChange={event => setSampleName(event.target.value)} placeholder="Tên giọng" /><textarea value={sampleReferenceText} onChange={event => setSampleReferenceText(event.target.value)} placeholder="Transcript chính xác" rows="3" /><div className="tts-time-range"><label>Bắt đầu<input type="number" value={sampleStart} onChange={event => setSampleStart(event.target.value)} /></label><label>Kết thúc<input type="number" value={sampleEnd} onChange={event => setSampleEnd(event.target.value)} /></label></div><small>{sampleDuration ? `${sampleDuration.toFixed(1)} giây` : ''}</small><button className="tts-button primary" onClick={cloneSample}>Tạo profile</button></div>
      <div><h2>Quản lý giọng VieNeu</h2><p>Hãy clone, cập nhật reference, tuning và revision trong giao diện/API của VieNeu. Nexus chỉ lưu ánh xạ voice ID để chọn giọng và tạo audio.</p></div>
    </section>

    <section className="tts-card"><div className="tts-list-tools"><h2>Danh mục giọng</h2><input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Tìm tên hoặc Voice ID…" /><select value={filter} onChange={event => setFilter(event.target.value)}><option value="all">Tất cả</option>{providers.map(provider => <option key={provider.id} value={provider.id}>{provider.display_name}</option>)}<option value="archived">Đã lưu trữ</option></select></div>
      <div className="tts-voice-list">{visibleVoices.map(voice => <div className="tts-voice" key={voice.id}><div><strong>{voice.name}</strong><span className={`tts-provider-badge ${voice.provider_id}`}>{providerLabel(voice.provider_id, providers)}</span><small>{voice.provider_voice_id} · revision {voice.revision} · {voice.status}{voice.provider_id === 'vieneu_v3_turbo' && ` · active ${voice.active_revision || '—'} · draft ${voice.draft_revision || '—'}`}</small>
        <div className="tts-casting-profile"><span className="tts-casting-label">Được dùng cho</span>{[['single_narrator', 'Đơn giọng'], ['multi_host', 'MC'], ['multi_guest', 'Khách']].map(([mode, label]) => <label className="tts-toggle" key={mode}><input type="checkbox" checked={(voice.casting_profile?.usage_modes || []).includes(mode)} onChange={event => toggleUsageMode(voice, mode, event.target.checked)} /> {label}</label>)}
          <label>Giới tính<select value={voice.casting_profile?.gender || 'any'} onChange={event => patchCasting(voice, { gender: event.target.value })}><option value="any">Linh hoạt</option><option value="male">Nam</option><option value="female">Nữ</option></select></label>
          <label>Nhóm tuổi<select value={voice.casting_profile?.age_group || 'any'} onChange={event => patchCasting(voice, { age_group: event.target.value })}><option value="any">Linh hoạt</option><option value="young">Trẻ</option><option value="adult">Trưởng thành</option><option value="senior">Lớn tuổi</option></select></label>
          <button className="tts-button" onClick={() => editCastingTags(voice, 'languages', 'Ngôn ngữ')}>🌐 {(voice.casting_profile?.languages || ['vi']).join(', ')}</button><button className="tts-button" onClick={() => editCastingTags(voice, 'roles', 'Vai trò')}>🎭 {(voice.casting_profile?.roles || ['guest']).join(', ')}</button><button className="tts-button" onClick={() => editCastingTags(voice, 'styles', 'Phong cách')}>✨ {(voice.casting_profile?.styles || []).join(', ') || 'Phong cách'}</button><button className="tts-button" onClick={() => editCastingTags(voice, 'voice_pools', 'Nhóm giọng (mã không dấu)')}>🗂 {(voice.casting_profile?.voice_pools || []).join(', ') || 'Chưa có nhóm'}</button></div></div>
        <div className="tts-actions">{(voice.status === 'active' || (voice.status === 'draft' && voice.provider_id === 'vieneu_v3_turbo')) && <button className="tts-button" onClick={() => testVoice(voice)}>Nghe thử</button>}{voice.provider_id === 'omnivoice' && <button className="tts-button" onClick={() => { const current = voice.config?.target_words_per_minute || 145; const raw = prompt('Tốc độ đọc mục tiêu (105–240 từ/phút):', String(current)); if (raw === null) return; const target = Number(raw); if (!Number.isFinite(target) || target < 105 || target > 240) { setError('Tốc độ OmniVoice phải từ 105 đến 240 từ/phút.'); return } patchVoice(voice, { config: { ...(voice.config || {}), target_words_per_minute: target } }, 'Đã lưu tốc độ OmniVoice; job mới sẽ dùng cấu hình này.') }}>Tốc độ</button>}<button className="tts-button" onClick={() => { const name = prompt('Tên hiển thị mới:', voice.name); if (name?.trim()) patchVoice(voice, { name: name.trim() }, 'Đã đổi tên giọng.') }}>Đổi tên</button>{voice.status === 'archived' ? <button className="tts-button primary" onClick={() => patchVoice(voice, { status: voice.provider_id === 'vieneu_v3_turbo' && !voice.active_revision ? 'draft' : 'active' }, 'Đã khôi phục giọng.')}>Khôi phục</button> : <button className="tts-button danger" onClick={() => { if (confirm('Lưu trữ giọng này?')) patchVoice(voice, { status: 'archived' }, 'Đã lưu trữ giọng.') }}>Lưu trữ</button>}</div>
      </div>)}</div></section>

    {busy && <div className="tts-status">Đang xử lý…</div>}{notice && <div className="tts-status success">{notice}</div>}{error && <div className="tts-status error">{error}</div>}
  </div>
}
