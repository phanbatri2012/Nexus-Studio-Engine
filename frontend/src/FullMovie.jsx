import { useEffect, useMemo, useRef, useState } from 'react'
import { extractErrorMessage } from './apiError.js'
import './FullMovie.css'


const API_BASE = 'http://127.0.0.1:8080'
const POLLABLE_STATUSES = new Set(['queued', 'running', 'retry_wait'])
const AUTO_RESUME_REASONS = new Set(['filename_review', 'full_movie_resume_after_restart'])

function formatBytes(value) {
  const bytes = Number(value) || 0
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let current = bytes / 1024
  let index = 0
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024
    index += 1
  }
  return `${current.toFixed(current >= 10 ? 1 : 2)} ${units[index]}`
}

function formatDuration(value) {
  const seconds = Math.max(0, Math.round(Number(value) || 0))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remaining = seconds % 60
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remaining).padStart(2, '0')}`
    : `${minutes}:${String(remaining).padStart(2, '0')}`
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options)
  const data = await response.json().catch(() => ({}))
  if (!response.ok || data.success === false) {
    throw new Error(extractErrorMessage(data, `Yêu cầu thất bại (${response.status}).`))
  }
  return data
}

function FullMovie() {
  const [sourcePath, setSourcePath] = useState('')
  const [scan, setScan] = useState(null)
  const [episodes, setEpisodes] = useState([])
  const [job, setJob] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [dragIndex, setDragIndex] = useState(null)
  const [thumbnailTitle, setThumbnailTitle] = useState('')
  const [thumbnailPath, setThumbnailPath] = useState('')
  const [thumbnailJob, setThumbnailJob] = useState(null)
  const [thumbnailBusy, setThumbnailBusy] = useState('')
  const [thumbnailError, setThumbnailError] = useState('')
  const appliedProposalJobRef = useRef('')
  const autoResumedJobRef = useRef('')
  const loadedThumbnailJobRef = useRef('')

  const reviewRequired = job?.result?.attention_required === 'filename_review'
  const isTerminal = ['completed', 'error', 'failed', 'canceled'].includes(job?.status)
  const controlsDisabled = Boolean(busy)
  const thumbnailTerminal = ['completed', 'error', 'failed', 'canceled'].includes(thumbnailJob?.status)
  const thumbnailControlsDisabled = Boolean(thumbnailBusy) || Boolean(thumbnailJob && !thumbnailTerminal)

  const loadJob = async jobId => {
    const data = await api(`/api/full-movie/jobs/${jobId}`)
    setJob(data.job)
    return data.job
  }

  const loadThumbnailJob = async jobId => {
    const data = await api(`/api/full-movie/thumbnail/jobs/${jobId}`)
    setThumbnailJob(data.job)
    return data.job
  }

  useEffect(() => {
    let active = true
    api('/api/full-movie/jobs/latest')
      .then(data => {
        if (!active || !data.job) return
        const restoredJob = data.job
        setJob(restoredJob)
        setScan(restoredJob.scan || null)
        setSourcePath(restoredJob.scan?.source_path || '')
      })
      .catch(loadError => {
        if (active) setError(loadError.message)
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    api('/api/full-movie/thumbnail/jobs/latest')
      .then(data => {
        if (!active || !data.job) return
        setThumbnailJob(data.job)
        setThumbnailTitle(data.job.input_title || '')
        setThumbnailPath(data.job.resolved_video_path || data.job.input_path || '')
      })
      .catch(loadError => {
        if (active) setThumbnailError(loadError.message)
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!job?.id || !POLLABLE_STATUSES.has(job.status)) return undefined
    const timer = window.setInterval(() => {
      loadJob(job.id).catch(pollError => setError(pollError.message))
    }, 1500)
    return () => window.clearInterval(timer)
  }, [job?.id, job?.status])

  useEffect(() => {
    if (!thumbnailJob?.id || !POLLABLE_STATUSES.has(thumbnailJob.status)) return undefined
    const timer = window.setInterval(() => {
      loadThumbnailJob(thumbnailJob.id).catch(pollError => setThumbnailError(pollError.message))
    }, 1500)
    return () => window.clearInterval(timer)
  }, [thumbnailJob?.id, thumbnailJob?.status])

  useEffect(() => {
    const thumbnailJobId = job?.result?.thumbnail_job_id
    if (!thumbnailJobId || loadedThumbnailJobRef.current === thumbnailJobId) return
    loadedThumbnailJobRef.current = thumbnailJobId
    setThumbnailTitle(job.result.movie_title_us || '')
    setThumbnailPath(job.result.output_path || '')
    loadThumbnailJob(thumbnailJobId).catch(loadError => {
      loadedThumbnailJobRef.current = ''
      setThumbnailError(loadError.message)
    })
  }, [job?.result?.thumbnail_job_id, job?.result?.movie_title_us, job?.result?.output_path])

  useEffect(() => {
    const reason = job?.result?.attention_required
    if (
      !job?.id ||
      job.status !== 'paused' ||
      !AUTO_RESUME_REASONS.has(reason) ||
      autoResumedJobRef.current === job.id
    ) return
    autoResumedJobRef.current = job.id
    setBusy('auto-resume')
    setError('')
    api(`/api/jobs/${job.id}/resume`, { method: 'POST' })
      .then(() => loadJob(job.id))
      .catch(resumeError => {
        autoResumedJobRef.current = ''
        setError(resumeError.message)
      })
      .finally(() => setBusy(''))
  }, [job?.id, job?.status, job?.result?.attention_required])

  useEffect(() => {
    const proposal = job?.translation_proposal
    if (!proposal?.episodes?.length || appliedProposalJobRef.current === job.id) return
    const sourceFiles = job.scan?.files || scan?.files || []
    const byIndex = new Map(proposal.episodes.map(item => [Number(item.index), item.title_us]))
    setEpisodes(sourceFiles.map((file, index) => ({
      ...file,
      source_name: file.name,
      title_us: byIndex.get(index + 1) || `Episode ${index + 1}`
    })))
    setScan(job.scan || scan)
    appliedProposalJobRef.current = job.id
  }, [job, scan])

  const summary = useMemo(() => {
    if (!scan) return ''
    return `${scan.episode_count} tập · ${formatDuration(scan.total_duration)} · ${formatBytes(scan.total_size)}`
  }, [scan])

  const chooseFolder = async () => {
    if (controlsDisabled) return
    setBusy('choose')
    setError('')
    try {
      const data = await api('/api/full-movie/select-folder', { method: 'POST' })
      if (!data.cancelled && data.path) {
        setSourcePath(data.path)
        setScan(null)
        setEpisodes([])
        setJob(null)
        appliedProposalJobRef.current = ''
      }
    } catch (chooseError) {
      setError(chooseError.message)
    } finally {
      setBusy('')
    }
  }

  const scanFolder = async () => {
    if (!sourcePath.trim() || controlsDisabled) return
    setBusy('scan')
    setError('')
    setJob(null)
    appliedProposalJobRef.current = ''
    try {
      const data = await api('/api/full-movie/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_path: sourcePath.trim() })
      })
      setScan(data.scan)
      setSourcePath(data.scan.source_path)
      setEpisodes((data.scan.files || []).map(file => ({
        ...file,
        source_name: file.name,
        title_us: ''
      })))
    } catch (scanError) {
      setScan(null)
      setEpisodes([])
      setError(scanError.message)
    } finally {
      setBusy('')
    }
  }

  const moveEpisode = (from, to) => {
    if (from === to || from < 0 || to < 0 || to >= episodes.length) return
    setEpisodes(current => {
      const next = [...current]
      const [moved] = next.splice(from, 1)
      next.splice(to, 0, moved)
      return next
    })
  }

  const startTranslation = async () => {
    if (!scan?.ready || !episodes.length || controlsDisabled) return
    setBusy('translate')
    setError('')
    try {
      const data = await api('/api/full-movie/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_path: scan.source_path,
          ordered_source_names: episodes.map(item => item.source_name)
        })
      })
      setJob(data.job)
      if (!data.created && data.job.status === 'paused') {
        await loadJob(data.job.id)
      }
    } catch (translateError) {
      setError(translateError.message)
    } finally {
      setBusy('')
    }
  }

  const controlJob = async action => {
    if (!job?.id || controlsDisabled) return
    if (action === 'cancel' && !window.confirm('Hủy job ghép phim hiện tại? Các tập gốc sẽ được giữ nguyên.')) return
    setBusy(action)
    setError('')
    try {
      await api(`/api/jobs/${job.id}/${action}`, { method: 'POST' })
      await loadJob(job.id)
    } catch (controlError) {
      setError(controlError.message)
    } finally {
      setBusy('')
    }
  }

  const chooseThumbnailVideo = async () => {
    if (thumbnailControlsDisabled) return
    setThumbnailBusy('choose-video')
    setThumbnailError('')
    try {
      const data = await api('/api/full-movie/thumbnail/select-video', { method: 'POST' })
      if (!data.cancelled && data.path) setThumbnailPath(data.path)
    } catch (chooseError) {
      setThumbnailError(chooseError.message)
    } finally {
      setThumbnailBusy('')
    }
  }

  const chooseThumbnailFolder = async () => {
    if (thumbnailControlsDisabled) return
    setThumbnailBusy('choose-folder')
    setThumbnailError('')
    try {
      const data = await api('/api/full-movie/select-folder', { method: 'POST' })
      if (!data.cancelled && data.path) setThumbnailPath(data.path)
    } catch (chooseError) {
      setThumbnailError(chooseError.message)
    } finally {
      setThumbnailBusy('')
    }
  }

  const startThumbnailJob = async () => {
    if (!thumbnailTitle.trim() || !thumbnailPath.trim() || thumbnailControlsDisabled) return
    setThumbnailBusy('start')
    setThumbnailError('')
    try {
      const data = await api('/api/full-movie/thumbnail/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: thumbnailTitle.trim(),
          movie_path: thumbnailPath.trim()
        })
      })
      setThumbnailJob(data.job)
      setThumbnailTitle(data.job.input_title || thumbnailTitle.trim())
      setThumbnailPath(data.job.resolved_video_path || thumbnailPath.trim())
    } catch (startError) {
      setThumbnailError(startError.message)
    } finally {
      setThumbnailBusy('')
    }
  }

  const controlThumbnailJob = async action => {
    if (!thumbnailJob?.id || thumbnailBusy) return
    if (action === 'cancel' && !window.confirm('Hủy job thumbnail hiện tại? Video phim sẽ được giữ nguyên.')) return
    setThumbnailBusy(action)
    setThumbnailError('')
    try {
      await api(`/api/jobs/${thumbnailJob.id}/${action}`, { method: 'POST' })
      await loadThumbnailJob(thumbnailJob.id)
    } catch (controlError) {
      setThumbnailError(controlError.message)
    } finally {
      setThumbnailBusy('')
    }
  }

  const thumbnailImageUrl = relativeUrl => {
    if (!relativeUrl) return ''
    const separator = relativeUrl.includes('?') ? '&' : '?'
    return `${API_BASE}${relativeUrl}${separator}v=${encodeURIComponent(thumbnailJob?.updated_at || '')}`
  }

  return (
    <div className="full-movie-page">
      <div className="full-movie-heading">
        <div>
          <span className="full-movie-kicker">LOCAL MEDIA WORKFLOW</span>
          <h1>Ghép phim Full</h1>
          <p>Tự động dịch tên sang US English, ghép video 16:9, kiểm tra chất lượng và đổi tên tập.</p>
        </div>
        <div className="full-movie-profile">
          <strong>1920×1080 · 25 fps</strong>
          <span>H.264 High · AAC 44.1 kHz</span>
          <span>Scale-to-fit · nền đen</span>
        </div>
      </div>

      <section className="full-movie-card source-card">
        <div className="full-movie-section-title">
          <span>1</span>
          <div><strong>Chọn thư mục phim</strong><small>Mỗi job xử lý một phim.</small></div>
        </div>
        <div className="full-movie-path-row">
          <input
            value={sourcePath}
            onChange={event => setSourcePath(event.target.value)}
            placeholder="E:\\Movies\\Tên phim"
            disabled={Boolean(job && !isTerminal)}
          />
          <button type="button" className="secondary" onClick={chooseFolder} disabled={controlsDisabled}>
            {busy === 'choose' ? 'Đang mở…' : 'Chọn thư mục'}
          </button>
          <button type="button" className="primary" onClick={scanFolder} disabled={!sourcePath.trim() || controlsDisabled}>
            {busy === 'scan' ? 'Đang quét…' : 'Quét tập phim'}
          </button>
        </div>
        {scan && (
          <div className="full-movie-scan-summary">
            <strong>{summary}</strong>
            <span>Trống: {formatBytes(scan.free_bytes)}</span>
            <span>Tạm tính cần: {formatBytes(scan.required_free_bytes)}</span>
            <span className={scan.ready ? 'ok' : 'bad'}>{scan.ready ? 'Media hợp lệ' : 'Có tập không hợp lệ'}</span>
          </div>
        )}
      </section>

      {error && <div className="full-movie-alert error">{error}</div>}
      {(scan?.warnings || []).map(warning => <div key={warning} className="full-movie-alert warning">{warning}</div>)}
      {scan?.requires_order_review && !job && (
        <div className="full-movie-alert warning">
          Một số số tập chưa rõ hoặc bị trùng. Nexus sẽ dùng thứ tự tự nhiên bên dưới; bạn có thể kéo thả để điều chỉnh trước khi bắt đầu.
        </div>
      )}

      {episodes.length > 0 && (
        <section className="full-movie-card">
          <div className="full-movie-section-title">
            <span>2</span>
            <div>
              <strong>{reviewRequired ? 'Đang tự động xác nhận tên US English' : 'Thứ tự và tên tập'}</strong>
              <small>Nexus tự dịch và xử lý toàn bộ; tên file gốc chỉ đổi sau khi video full đạt QC.</small>
            </div>
          </div>

          <div className="full-movie-table-wrap">
            <table className="full-movie-table">
              <thead>
                <tr><th>#</th><th>Tên file gốc</th><th>US English</th><th>Media</th></tr>
              </thead>
              <tbody>
                {episodes.map((episode, index) => (
                  <tr
                    key={episode.source_name}
                    draggable={!job}
                    onDragStart={() => setDragIndex(index)}
                    onDragOver={event => event.preventDefault()}
                    onDrop={() => {
                      if (dragIndex !== null) moveEpisode(dragIndex, index)
                      setDragIndex(null)
                    }}
                    className={dragIndex === index ? 'dragging' : ''}
                  >
                    <td className="order-cell">
                      <span className="drag-handle" title="Kéo để sắp xếp">⋮⋮</span>
                      <strong>{String(index + 1).padStart(3, '0')}</strong>
                      <div className="order-buttons">
                        <button onClick={() => moveEpisode(index, index - 1)} disabled={index === 0}>↑</button>
                        <button onClick={() => moveEpisode(index, index + 1)} disabled={index === episodes.length - 1}>↓</button>
                      </div>
                    </td>
                    <td>
                      <strong>{episode.source_name}</strong>
                      {(episode.warnings || []).map(warning => <small className="row-warning" key={warning}>{warning}</small>)}
                    </td>
                    <td>
                      <span className="translation-pending">{episode.title_us || 'Chờ ChatGPT dịch'}</span>
                    </td>
                    <td className="media-cell">
                      <span>{episode.width}×{episode.height}</span>
                      <span>{formatDuration(episode.duration)}</span>
                      <span>{episode.video_codec || 'video'} / {episode.audio_codec || 'audio'}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {!job && (
            <div className="full-movie-actions">
              <button className="primary" onClick={startTranslation} disabled={!scan?.ready || controlsDisabled}>
                {busy === 'translate' ? 'Đang khởi tạo tự động…' : 'Bắt đầu dịch & ghép tự động'}
              </button>
            </div>
          )}
        </section>
      )}

      {job && (
        <section className={`full-movie-card job-card status-${job.status}`}>
          <div className="full-movie-section-title">
            <span>3</span>
            <div><strong>Tiến độ job</strong><small>{job.id}</small></div>
          </div>
          <div className="job-status-line">
            <span className="status-pill">{job.status}</span>
            <strong>{job.progress || 'Đang chuẩn bị…'}</strong>
          </div>
          <div className="job-stage">Bước hiện tại: {job.stage || 'queued'}</div>
          {job.error && <div className="full-movie-alert error">{job.error}</div>}
          {job.result?.output_path && (
            <div className="full-movie-output">
              <strong>Video full đã hoàn thành</strong>
              <code>{job.result.output_path}</code>
              <span>{job.result.episode_count} tập · đã đổi tên {job.result.renamed_count} file</span>
              {job.result.thumbnail_job_id && <span>Thumbnail tự động: {job.result.thumbnail_job_id}</span>}
              {job.result.thumbnail_warning && <span className="output-warning">{job.result.thumbnail_warning}</span>}
            </div>
          )}
          <div className="full-movie-actions">
            {['queued', 'running', 'retry_wait'].includes(job.status) && (
              <button className="secondary" onClick={() => controlJob('pause')} disabled={controlsDisabled}>Tạm dừng</button>
            )}
            {job.status === 'paused' && (
              <button className="primary" onClick={() => controlJob('resume')} disabled={controlsDisabled}>Tiếp tục</button>
            )}
            {['queued', 'running', 'retry_wait', 'paused'].includes(job.status) && (
              <button className="danger" onClick={() => controlJob('cancel')} disabled={controlsDisabled}>Hủy job</button>
            )}
            {['error', 'failed', 'canceled'].includes(job.status) && (
              <button className="primary" onClick={() => controlJob('retry')} disabled={controlsDisabled}>Chạy lại từ checkpoint</button>
            )}
          </div>
        </section>
      )}

      <section className="full-movie-card thumbnail-card">
        <div className="full-movie-section-title">
          <span>4</span>
          <div>
            <strong>Tạo thumbnail tự động</strong>
            <small>Chỉ cần tiêu đề và đường dẫn. Nexus tự chọn cảnh, tạo 3 ảnh và chọn ảnh chính.</small>
          </div>
        </div>

        <label className="full-movie-title-field">
          <span>Tiêu đề phim hoặc video</span>
          <input
            value={thumbnailTitle}
            onChange={event => setThumbnailTitle(event.target.value)}
            placeholder="Alice and Her Three Fiancés"
            disabled={thumbnailControlsDisabled}
          />
          <small>Có thể nhập tiếng Việt; ChatGPT sẽ chuẩn hóa sang US English và tạo hook ngắn.</small>
        </label>

        <div className="thumbnail-path-row">
          <input
            value={thumbnailPath}
            onChange={event => setThumbnailPath(event.target.value)}
            placeholder="E:\\Movies\\Full Movie - Title.mp4 hoặc thư mục phim"
            disabled={thumbnailControlsDisabled}
          />
          <button type="button" className="secondary" onClick={chooseThumbnailVideo} disabled={thumbnailControlsDisabled}>
            {thumbnailBusy === 'choose-video' ? 'Đang mở…' : 'Chọn video'}
          </button>
          <button type="button" className="secondary" onClick={chooseThumbnailFolder} disabled={thumbnailControlsDisabled}>
            {thumbnailBusy === 'choose-folder' ? 'Đang mở…' : 'Chọn thư mục'}
          </button>
          <button
            type="button"
            className="primary"
            onClick={startThumbnailJob}
            disabled={!thumbnailTitle.trim() || !thumbnailPath.trim() || thumbnailControlsDisabled}
          >
            {thumbnailBusy === 'start' ? 'Đang khởi tạo…' : 'Tạo 3 thumbnail'}
          </button>
        </div>

        {thumbnailError && <div className="full-movie-alert error thumbnail-alert">{thumbnailError}</div>}

        {thumbnailJob && (
          <div className={`thumbnail-job status-${thumbnailJob.status}`}>
            <div className="job-status-line">
              <span className="status-pill">{thumbnailJob.status}</span>
              <strong>{thumbnailJob.progress || 'Đang chuẩn bị…'}</strong>
            </div>
            <div className="job-stage">Bước hiện tại: {thumbnailJob.stage || 'queued'} · {thumbnailJob.id}</div>
            {thumbnailJob.error && <div className="full-movie-alert error thumbnail-alert">{thumbnailJob.error}</div>}

            {thumbnailJob.result?.variants?.length > 0 && (
              <div className="thumbnail-gallery">
                {thumbnailJob.result.variants.map(variant => (
                  <article
                    key={variant.index}
                    className={variant.index === thumbnailJob.result.primary_index ? 'thumbnail-option primary-option' : 'thumbnail-option'}
                  >
                    <div className="thumbnail-image-wrap">
                      <img
                        src={thumbnailImageUrl(variant.url)}
                        alt={`Thumbnail ${variant.index}: ${variant.hook}`}
                      />
                      {variant.index === thumbnailJob.result.primary_index && <span className="primary-badge">PRIMARY</span>}
                    </div>
                    <div className="thumbnail-meta">
                      <strong>{variant.hook}</strong>
                      <span>Điểm {Number(variant.score || 0).toFixed(1)} · AI {Number(variant.creative_score || 0).toFixed(1)} · QC {Number(variant.technical_score || 0).toFixed(1)}</span>
                      {variant.reason && <small>{variant.reason}</small>}
                    </div>
                  </article>
                ))}
              </div>
            )}

            {thumbnailJob.result?.output_dir && (
              <div className="full-movie-output thumbnail-output">
                <strong>Đã lưu thumbnail cạnh phim</strong>
                <code>{thumbnailJob.result.output_dir}</code>
                <span>Ảnh chính: PRIMARY.jpg</span>
              </div>
            )}

            <div className="full-movie-actions">
              {['queued', 'running', 'retry_wait'].includes(thumbnailJob.status) && (
                <button className="secondary" onClick={() => controlThumbnailJob('pause')} disabled={Boolean(thumbnailBusy)}>Tạm dừng</button>
              )}
              {thumbnailJob.status === 'paused' && (
                <button className="primary" onClick={() => controlThumbnailJob('resume')} disabled={Boolean(thumbnailBusy)}>Tiếp tục</button>
              )}
              {['queued', 'running', 'retry_wait', 'paused'].includes(thumbnailJob.status) && (
                <button className="danger" onClick={() => controlThumbnailJob('cancel')} disabled={Boolean(thumbnailBusy)}>Hủy job</button>
              )}
              {['error', 'failed', 'canceled'].includes(thumbnailJob.status) && (
                <button className="primary" onClick={() => controlThumbnailJob('retry')} disabled={Boolean(thumbnailBusy)}>Chạy lại từ checkpoint</button>
              )}
            </div>
          </div>
        )}
      </section>
    </div>
  )
}

export default FullMovie
