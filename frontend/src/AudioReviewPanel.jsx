function formatDuration(seconds) {
  const totalMinutes = Math.max(0, Math.round((Number(seconds) || 0) / 60))
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  return hours > 0 ? `${hours} giờ ${minutes} phút` : `${minutes} phút`
}

function AudioReviewPanel({
  review,
  loading,
  submitting,
  voiceName,
  hasAudio,
  onConfirmExpansion,
  bodyRevision,
  regeneratingBody,
  applyingBodyRevision,
  onRegenerateBody,
  onApplyBodyRevision
}) {
  if (loading && !review && !bodyRevision) {
    return <div className="result-panel" style={{ padding: '18px 20px' }}>Đang kiểm duyệt kịch bản...</div>
  }
  if (!review && !bodyRevision) return null

  const metrics = review?.metrics || {}
  const errors = Array.isArray(review?.errors) ? review.errors : []
  const reviewRequired = Array.isArray(review?.review_required) ? review.review_required : []
  const warnings = Array.isArray(review?.warnings) ? review.warnings : []
  const approved = review?.status === 'approved'
  const blocked = review?.status === 'blocked' || errors.length > 0
  const needsReview = !approved && !blocked && reviewRequired.length > 0
  const expansionConfirmationOnly = reviewRequired.length > 0 && reviewRequired.every(
    item => item.code === 'excessive_source_expansion'
  )
  const statusColor = !review ? '#7ed6df' : approved ? '#2ecc71' : blocked ? '#ff6b6b' : '#f5b041'
  const statusLabel = !review
    ? 'Có lời đọc mới chờ áp dụng'
    : approved
    ? 'Đã tự động duyệt'
    : blocked
      ? 'Tự động kiểm tra không đạt'
      : needsReview ? 'Cần xem lại trước khi tạo audio' : 'Đang chờ hệ thống xử lý'
  const revisionReport = bodyRevision?.report || {}
  const revisionMetrics = revisionReport.metrics || {}
  const revisionErrors = Array.isArray(revisionReport.errors) ? revisionReport.errors : []
  const revisionWarnings = Array.isArray(revisionReport.warnings) ? revisionReport.warnings : []
  const revisionSectionWords = revisionMetrics.section_word_counts || {}
  const isNarrationRevision = bodyRevision?.scope === 'narration'
  const originalRevisionContent = isNarrationRevision
    ? bodyRevision?.original_narration
    : bodyRevision?.original_body
  const candidateRevisionContent = isNarrationRevision
    ? bodyRevision?.candidate_narration
    : bodyRevision?.candidate_body
  const revisionStatusLabels = {
    queued: 'Đang chờ ChatGPT',
    generating: 'Đang viết lại lời đọc',
    draft: 'Đã audit · chờ áp dụng',
    needs_review: 'Cần kiểm tra thủ công',
    applied: 'Đã áp dụng',
    stale: 'Kịch bản đã thay đổi',
    error: 'Tạo lại lời đọc thất bại',
    canceled: 'Đã hủy'
  }

  return (
    <div className="result-panel" style={{ padding: '18px 20px', borderColor: `${statusColor}66` }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '14px', flexWrap: 'wrap' }}>
        <div>
          <div style={{ color: 'var(--accent)', fontWeight: '700' }}>
            {review ? '🔎 Tự động kiểm tra trước khi tạo audio' : '♻️ Bản lời đọc mới'}
          </div>
          <div style={{ color: '#aaa', fontSize: '0.82em', marginTop: '5px' }}>
            {review
              ? `Phiên bản nội dung hiện tại · Giọng: ${voiceName || 'Giọng đã chọn'}`
              : 'Bản tạo lại đã hoàn tất kiểm định và đang chờ xem trước, áp dụng.'}
          </div>
        </div>
        <span style={{ color: statusColor, border: `1px solid ${statusColor}`, borderRadius: '12px', padding: '3px 10px', fontSize: '0.8em', fontWeight: '700' }}>
          {statusLabel}
        </span>
      </div>

      {review && (
        <>
          <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap', color: '#ddd', marginTop: '14px', fontSize: '0.88em' }}>
            <span>📝 {(metrics.word_count || 0).toLocaleString('vi-VN')} từ</span>
            <span>🔤 {(metrics.character_count || 0).toLocaleString('vi-VN')} ký tự</span>
            <span>
              ⏱ Ước tính {formatDuration(metrics.estimated_min_seconds)}–{formatDuration(metrics.estimated_max_seconds)}
            </span>
            {Number(metrics.source_word_count || 0) > 0 && (
              <span>
                📏 Nguồn {Number(metrics.source_word_count).toLocaleString('vi-VN')} từ
                {' · '}đầu ra ×{Number(metrics.source_expansion_ratio || 0).toLocaleString('vi-VN', { maximumFractionDigits: 2 })}
              </span>
            )}
          </div>
          <div style={{ color: '#777', fontSize: '0.76em', marginTop: '6px' }}>
            Thời lượng chỉ là ước tính theo tốc độ đọc, không phải ngưỡng bắt buộc.
          </div>
        </>
      )}

      {errors.length > 0 && (
        <div style={{ marginTop: '13px', color: '#ff7b7b', fontSize: '0.86em' }}>
          {errors.map((item, index) => (
            <div key={`${item.code}-${index}`} style={{ marginTop: '4px' }}>
              ⛔ {item.message}
            </div>
          ))}
        </div>
      )}
      {reviewRequired.length > 0 && (
        <details open style={{ marginTop: '13px', color: '#f5b041', fontSize: '0.84em' }}>
          <summary>{reviewRequired.length} vấn đề cần xem lại</summary>
          {reviewRequired.map((item, index) => {
            const location = [
              item.section,
              Number.isInteger(item.paragraph_index) ? `đoạn ${item.paragraph_index + 1}` : ''
            ].filter(Boolean).join(' · ')
            return (
              <div key={`${item.code}-${index}`} style={{ marginTop: '8px', paddingLeft: '10px', borderLeft: '2px solid rgba(245,176,65,0.45)' }}>
                <div>⚠️ {location ? `${location}: ` : ''}{item.message}</div>
                {item.preview && (
                  <div style={{ color: '#bbb', marginTop: '3px', whiteSpace: 'pre-wrap' }}>{item.preview}</div>
                )}
              </div>
            )
          })}
        </details>
      )}
      {warnings.length > 0 && (
        <details style={{ marginTop: '13px', color: '#f5b041', fontSize: '0.84em' }}>
          <summary>{warnings.length} cảnh báo cần xem lại</summary>
          {warnings.map((item, index) => (
            <div key={`${item.code}-${index}`} style={{ marginTop: '5px' }}>
              ⚠️ {item.section ? `${item.section}: ` : ''}{item.message}
              {item.preview && <div style={{ color: '#aaa', marginTop: '3px' }}>{item.preview}</div>}
            </div>
          ))}
        </details>
      )}

      {expansionConfirmationOnly && !hasAudio && typeof onConfirmExpansion === 'function' && (
        <button
          type="button"
          onClick={onConfirmExpansion}
          disabled={submitting}
          style={{
            marginTop: '14px',
            padding: '7px 12px',
            borderRadius: '6px',
            border: '1px solid #f5b041',
            background: submitting ? '#333' : 'rgba(245,176,65,0.12)',
            color: submitting ? '#888' : '#f5b041',
            cursor: submitting ? 'not-allowed' : 'pointer',
            fontWeight: 700
          }}
        >
          Xác nhận dung lượng và tạo audio
        </button>
      )}

      <div style={{ marginTop: '16px', paddingTop: '14px', borderTop: '1px solid rgba(255,255,255,0.1)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'center', flexWrap: 'wrap' }}>
          <div>
            <div style={{ color: '#7ed6df', fontWeight: 700 }}>♻️ Tạo lại toàn bộ lời đọc</div>
            <div style={{ color: '#888', fontSize: '0.78em', marginTop: '4px' }}>
              Viết mới INTRO, BODY và OUTRO theo prompt của video; metadata và thumbnail giữ nguyên.
            </div>
          </div>
          <button
            type="button"
            onClick={onRegenerateBody}
            disabled={Boolean(hasAudio || regeneratingBody || applyingBodyRevision)}
            style={{
              padding: '7px 12px',
              borderRadius: '6px',
              border: '1px solid #7ed6df',
              background: regeneratingBody ? '#333' : 'rgba(126,214,223,0.12)',
              color: hasAudio || regeneratingBody ? '#888' : '#7ed6df',
              cursor: hasAudio || regeneratingBody ? 'not-allowed' : 'pointer',
              fontWeight: 700
            }}
            title={hasAudio ? 'Video đã có audio nên không thể thay lời đọc.' : ''}
          >
            {regeneratingBody ? 'Đang viết lại...' : 'Tạo lại lời đọc'}
          </button>
        </div>

        {bodyRevision && (
          <div style={{ marginTop: '13px', border: '1px solid rgba(126,214,223,0.25)', borderRadius: '8px', padding: '12px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: '10px', flexWrap: 'wrap', fontSize: '0.84em' }}>
              <span style={{ color: '#ddd', fontWeight: 700 }}>
                {revisionStatusLabels[bodyRevision.status] || bodyRevision.status}
              </span>
              <span style={{ color: '#999' }}>{bodyRevision.job_progress || ''}</span>
            </div>

            {Number(revisionMetrics.source_word_count || 0) > 0 && (
              <div style={{ color: '#bbb', fontSize: '0.8em', marginTop: '8px' }}>
                Lời đọc mới {Number(revisionMetrics.word_count || 0).toLocaleString('vi-VN')} từ
                {' · '}nguồn {Number(revisionMetrics.source_word_count).toLocaleString('vi-VN')} từ
                {' · '}tỷ lệ ×{Number(revisionMetrics.source_expansion_ratio || 0).toLocaleString('vi-VN', { maximumFractionDigits: 2 })}
              </div>
            )}

            {isNarrationRevision && Object.keys(revisionSectionWords).length > 0 && (
              <div style={{ color: '#999', fontSize: '0.78em', marginTop: '6px' }}>
                INTRO {Number(revisionSectionWords.INTRO || 0).toLocaleString('vi-VN')} từ
                {' · '}BODY {Number(revisionSectionWords.BODY || 0).toLocaleString('vi-VN')} từ
                {' · '}OUTRO {Number(revisionSectionWords.OUTRO || 0).toLocaleString('vi-VN')} từ
              </div>
            )}

            {revisionErrors.map((item, index) => (
              <div key={`revision-error-${item.code}-${index}`} style={{ color: '#ff7b7b', fontSize: '0.82em', marginTop: '7px' }}>
                ⛔ {item.message || item.code}
              </div>
            ))}
            {revisionWarnings.map((item, index) => (
              <div key={`revision-warning-${item.code}-${index}`} style={{ color: '#f5b041', fontSize: '0.82em', marginTop: '7px' }}>
                ⚠️ {item.message || item.code}
              </div>
            ))}
            {bodyRevision.job_error && (
              <div style={{ color: '#ff7b7b', fontSize: '0.82em', marginTop: '7px' }}>
                ⛔ {bodyRevision.job_error}
              </div>
            )}

            {(originalRevisionContent || candidateRevisionContent) && (
              <details open={bodyRevision.status === 'draft'} style={{ marginTop: '12px' }}>
                <summary style={{ color: '#ddd', cursor: 'pointer', fontSize: '0.84em' }}>
                  So sánh lời đọc cũ và bản mới
                </summary>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '10px', marginTop: '10px' }}>
                  {[
                    [isNarrationRevision ? 'INTRO · BODY · OUTRO hiện tại' : 'BODY hiện tại', originalRevisionContent],
                    [isNarrationRevision ? 'INTRO · BODY · OUTRO mới' : 'BODY mới', candidateRevisionContent]
                  ].map(([label, value]) => (
                    <div key={label} style={{ minWidth: 0 }}>
                      <div style={{ color: '#aaa', fontSize: '0.78em', marginBottom: '5px' }}>{label}</div>
                      <div style={{
                        whiteSpace: 'pre-wrap',
                        overflow: 'auto',
                        maxHeight: '360px',
                        padding: '10px',
                        borderRadius: '6px',
                        background: 'rgba(0,0,0,0.24)',
                        color: '#ddd',
                        fontSize: '0.78em',
                        lineHeight: 1.55,
                        overflowWrap: 'anywhere'
                      }}>
                        {value || 'Chưa có nội dung.'}
                      </div>
                    </div>
                  ))}
                </div>
              </details>
            )}

            {bodyRevision.can_apply && (
              <button
                type="button"
                onClick={onApplyBodyRevision}
                disabled={Boolean(applyingBodyRevision || hasAudio)}
                style={{
                  marginTop: '12px',
                  padding: '7px 12px',
                  borderRadius: '6px',
                  border: '1px solid #2ecc71',
                  background: applyingBodyRevision ? '#333' : 'rgba(46,204,113,0.12)',
                  color: applyingBodyRevision ? '#888' : '#2ecc71',
                  cursor: applyingBodyRevision ? 'not-allowed' : 'pointer',
                  fontWeight: 700
                }}
              >
                {applyingBodyRevision ? 'Đang áp dụng...' : 'Dùng lời đọc mới'}
              </button>
            )}
          </div>
        )}
      </div>

      <div style={{ color: '#888', fontSize: '0.8em', marginTop: '13px' }}>
        {submitting
          ? 'Hệ thống đang gửi yêu cầu audio an toàn sang nhà cung cấp TTS...'
          : hasAudio
            ? 'Audio của nội dung này đã có trên hệ thống.'
            : blocked || needsReview
              ? 'Nhà cung cấp TTS chưa được gọi nên chưa phát sinh chi phí hoặc tải xử lý.'
              : 'Khi đạt kiểm tra, hệ thống tự duyệt và tạo audio; bạn không cần xác nhận.'}
      </div>
    </div>
  )
}

export default AudioReviewPanel
