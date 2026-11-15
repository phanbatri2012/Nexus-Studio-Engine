import { useCallback, useEffect, useRef, useState } from 'react'
import { extractErrorMessage } from './apiError.js'

const API_BASE = 'http://127.0.0.1:8080'
const REQUEST_TIMEOUT_MS = 15000
const STATUS_TIMEOUT_MS = 30000
const SCAN_TIMEOUT_MS = 180000
const CLEANUP_POLL_INTERVAL_MS = 2000
const CLEANUP_POLL_DEADLINE_MS = 5 * 60 * 1000
export const MAX_CLEANUP_POLLS = 120

function formatBytes(value) {
  const bytes = Math.max(0, Number(value) || 0)
  if (bytes >= 1024 ** 3) return (bytes / (1024 ** 3)).toFixed(2) + ' GB'
  if (bytes >= 1024 ** 2) return (bytes / (1024 ** 2)).toFixed(2) + ' MB'
  if (bytes >= 1024) return (bytes / 1024).toFixed(2) + ' KB'
  return Math.round(bytes) + ' B'
}

function wait(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds))
}

async function fetchJson(path, options = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(API_BASE + path, {
      ...options,
      signal: controller.signal,
    })
    let data = {}
    try {
      data = await response.json()
    } catch {
      data = {}
    }
    if (!response.ok) {
      throw new Error(extractErrorMessage(data, 'HTTP ' + response.status))
    }
    return data
  } catch (requestError) {
    if (requestError?.name === 'AbortError') {
      throw new Error('Yêu cầu dọn rác đã quá thời gian chờ.')
    }
    throw requestError
  } finally {
    clearTimeout(timeoutId)
  }
}

export default function SystemCleanupPanel() {
  const mountedRef = useRef(true)
  const [cleanupStatus, setCleanupStatus] = useState(null)
  const [storage, setStorage] = useState(null)
  const [summary, setSummary] = useState(null)
  const [run, setRun] = useState(null)
  const [loadingStatus, setLoadingStatus] = useState(true)
  const [scanning, setScanning] = useState(false)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')

  const applySnapshot = useCallback(data => {
    if (data?.cleanup_status) setCleanupStatus(data.cleanup_status)
    if (data?.storage) setStorage(data.storage)
  }, [])

  const loadStatus = useCallback(async () => {
    setLoadingStatus(true)
    try {
      const data = await fetchJson('/api/system/cleanup/status', {}, STATUS_TIMEOUT_MS)
      if (!mountedRef.current) return
      applySnapshot(data)
      setError('')
    } catch (loadError) {
      if (mountedRef.current) {
        setError('Không thể kiểm tra trạng thái dọn rác: ' + loadError.message)
      }
    } finally {
      if (mountedRef.current) setLoadingStatus(false)
    }
  }, [applySnapshot])

  useEffect(() => {
    mountedRef.current = true
    loadStatus()
    return () => {
      mountedRef.current = false
    }
  }, [loadStatus])

  const scanCleanup = async () => {
    setScanning(true)
    setError('')
    try {
      const data = await fetchJson('/api/system/cleanup/stats', {}, SCAN_TIMEOUT_MS)
      if (!mountedRef.current) return
      applySnapshot(data)
      setSummary(data.summary || null)
    } catch (scanError) {
      if (mountedRef.current) setError('Quét dung lượng thất bại: ' + scanError.message)
    } finally {
      if (mountedRef.current) setScanning(false)
    }
  }

  const startCleanup = async () => {
    if (!cleanupStatus?.safe_to_cleanup) {
      setError('Hệ thống đang bận; hãy hoàn tất hoặc dừng an toàn các job đang chặn rồi kiểm tra lại.')
      return
    }
    const planned = summary
      ? formatBytes(summary.total_bytes_planned)
      : 'các file đã hết hạn theo chính sách'
    if (!window.confirm(
      'Dọn ' + planned + '?\n\nNexus chỉ xóa file đã qua kiểm tra an toàn và ghi manifest kiểm toán.'
    )) return

    setStarting(true)
    setError('')
    try {
      const queued = await fetchJson('/api/system/cleanup/run?vacuum=true', { method: 'POST' })
      if (!mountedRef.current) return
      setRun(queued.run || { status: queued.status, manifest_id: queued.manifest_id })
      const deadline = Date.now() + CLEANUP_POLL_DEADLINE_MS

      for (let attempt = 0; attempt < MAX_CLEANUP_POLLS && Date.now() < deadline; attempt += 1) {
        await wait(CLEANUP_POLL_INTERVAL_MS)
        if (!mountedRef.current) return
        const data = await fetchJson('/api/system/cleanup/runs/' + queued.manifest_id)
        const nextRun = data.run || null
        setRun(nextRun)
        if (['completed', 'blocked', 'error'].includes(nextRun?.status)) {
          if (nextRun?.summary) setSummary(nextRun.summary)
          await loadStatus()
          if (nextRun?.status === 'blocked') {
            setError(nextRun.message || 'Dọn rác đã dừng vì hệ thống trở lại trạng thái bận.')
          } else if (nextRun?.status === 'error') {
            setError(nextRun.message || 'Dọn rác thất bại.')
          }
          return
        }
      }
      throw new Error('Tác vụ vẫn đang chạy sau 5 phút; hãy bấm Kiểm tra lại để xem trạng thái hệ thống.')
    } catch (cleanupError) {
      if (mountedRef.current) setError(cleanupError.message)
    } finally {
      if (mountedRef.current) setStarting(false)
    }
  }

  const blockers = cleanupStatus?.blocking_jobs || []
  const categories = Object.entries(summary?.categories || {})
    .filter(([, item]) => Number(item?.files_planned) > 0)
  const freedBytes = Number(run?.summary?.total_bytes_freed ?? summary?.total_bytes_freed) || 0

  return (
    <section className="result-panel" style={{ padding: '16px', margin: '20px 0' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '16px', flexWrap: 'wrap' }}>
        <div>
          <h2 style={{ margin: 0, fontSize: '1.08rem', color: '#f1f5f9' }}>🧹 Dọn rác hệ thống</h2>
          <div style={{ color: '#94a3b8', fontSize: '0.84rem', marginTop: '5px' }}>
            Tự động mỗi 24 giờ; nếu bận, Nexus thử lại mỗi 1 giờ trong gần 24 giờ. Chỉ dữ liệu hết hạn được xóa khi toàn bộ worker và trình duyệt do Nexus quản lý đang rảnh.
          </div>
        </div>
        <div style={{ textAlign: 'right', fontSize: '0.85rem' }}>
          <div style={{ color: '#cbd5e1' }}>
            Ổ dữ liệu còn trống: <strong style={{ color: '#67e8f9' }}>{storage ? formatBytes(storage.free_bytes) : '—'}</strong>
          </div>
          <div style={{ color: cleanupStatus?.safe_to_cleanup ? '#4ade80' : '#fbbf24', marginTop: '4px' }}>
            {loadingStatus
              ? 'Đang kiểm tra...'
              : cleanupStatus?.safe_to_cleanup
                ? 'Sẵn sàng dọn an toàn'
                : 'Đang bị chặn để bảo vệ dữ liệu'}
          </div>
        </div>
      </div>

      {summary && (
        <div style={{ marginTop: '14px', color: '#e2e8f0', fontSize: '0.9rem' }}>
          Có thể dọn <strong>{summary.total_files_planned || 0} file</strong>, khoảng{' '}
          <strong style={{ color: '#67e8f9' }}>{formatBytes(summary.total_bytes_planned)}</strong>.
          {freedBytes > 0 && (
            <span style={{ color: '#4ade80' }}> Đã giải phóng {formatBytes(freedBytes)}.</span>
          )}
          {categories.length > 0 && (
            <div style={{ color: '#94a3b8', marginTop: '6px' }}>
              {categories.map(([key, item]) => (
                <span key={key} style={{ display: 'inline-block', marginRight: '14px' }}>
                  {item.name || key}: {item.files_planned} file / {formatBytes(item.bytes_planned)}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {blockers.length > 0 && (
        <div style={{ marginTop: '12px', padding: '10px 12px', border: '1px solid rgba(245, 158, 11, 0.35)', borderRadius: '8px', background: 'rgba(245, 158, 11, 0.08)', color: '#fde68a', fontSize: '0.84rem' }}>
          <strong>Đang chặn:</strong>{' '}
          {blockers.map(item => (
            <span key={(item.type || '') + ':' + (item.id || '')} style={{ marginRight: '12px' }}>
              {item.job_type || item.type || 'task'} ({item.status || 'active'})
            </span>
          ))}
        </div>
      )}

      {run && (
        <div style={{ marginTop: '12px', color: run.status === 'completed' ? '#4ade80' : '#93c5fd', fontSize: '0.86rem' }}>
          {run.message || ('Trạng thái dọn rác: ' + run.status)}
        </div>
      )}
      {error && <div style={{ marginTop: '12px', color: '#fca5a5', fontSize: '0.86rem' }}>{error}</div>}

      <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', marginTop: '14px' }}>
        <button
          type="button"
          className="btn-secondary"
          onClick={scanCleanup}
          disabled={scanning || starting}
          style={{ width: 'auto', padding: '8px 14px' }}
        >
          {scanning ? 'Đang quét...' : 'Quét dung lượng'}
        </button>
        <button
          type="button"
          className="btn-run"
          onClick={startCleanup}
          disabled={!cleanupStatus?.safe_to_cleanup || scanning || starting}
          style={{ width: 'auto', padding: '8px 14px' }}
        >
          {starting ? 'Đang dọn...' : 'Dọn rác an toàn'}
        </button>
        <button
          type="button"
          className="btn-secondary"
          onClick={loadStatus}
          disabled={loadingStatus || scanning || starting}
          style={{ width: 'auto', padding: '8px 14px' }}
        >
          Kiểm tra lại
        </button>
      </div>
    </section>
  )
}
