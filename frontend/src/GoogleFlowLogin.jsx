import { useState, useEffect } from 'react';
import './AutoLogin.css';

const API = 'http://127.0.0.1:8080';

export default function GoogleFlowLogin() {
  const [account, setAccount] = useState({
    id: '',
    email: '',
    password: '',
    totp_secret: '',
    profile_name: 'PROFILE_GOOGLE_FLOW_1',
    password_configured: false,
    totp_configured: false,
    session_configured: false,
    plan_tier: 'Standard',
    quotas: {},
  });
  const [accounts, setAccounts] = useState([]);
  const [fleet, setFleet] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [browserStatus, setBrowserStatus] = useState(null);
  const [loadingMsg, setLoadingMsg] = useState('');
  const [resultMsg, setResultMsg] = useState('');
  const [resultType, setResultType] = useState('success');
  const [checkingQuotaId, setCheckingQuotaId] = useState(null);
  const [isCheckingAll, setIsCheckingAll] = useState(false);
  const [showAddForm, setShowAddForm] = useState(false);
  const [newAccount, setNewAccount] = useState({ email: '', password: '', totp_secret: '' });

  const readApiResponse = async (response) => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail || `HTTP ${response.status}`);
    }
    return data;
  };

  const fetchStatus = async (hydrateCredentials = false) => {
    try {
      const [accRes, brRes, accountsRes] = await Promise.all([
        fetch(`${API}/api/flow/account`),
        fetch(`${API}/api/flow/browser`),
        fetch(`${API}/api/flow/accounts`).catch(() => null),
      ]);
      const [accData, brData] = await Promise.all([
        readApiResponse(accRes),
        readApiResponse(brRes),
      ]);
      setAccount(prev => ({
        ...prev,
        id: accData.account_id || prev.id,
        email: hydrateCredentials ? (accData.email || '') : prev.email,
        password: hydrateCredentials ? '' : prev.password,
        totp_secret: hydrateCredentials ? '' : prev.totp_secret,
        profile_name: accData.profile_name || prev.profile_name,
        password_configured: Boolean(accData.password_configured),
        totp_configured: Boolean(accData.totp_configured),
        session_configured: Boolean(accData.session_configured),
        plan_tier: accData.plan_tier || prev.plan_tier,
        quotas: accData.quotas || prev.quotas,
      }));
      setBrowserStatus(brData);

      if (accountsRes && accountsRes.ok) {
        const accsData = await accountsRes.json().catch(() => ({}));
        if (Array.isArray(accsData.items)) {
          setAccounts(accsData.items);
        }
        if (accsData.fleet) {
          setFleet(accsData.fleet);
        }
        if (Array.isArray(accsData.catalog)) {
          setCatalog(accsData.catalog);
        }
      }
    } catch (err) {
      console.error('Lỗi lấy thông tin Flow:', err);
    }
  };

  useEffect(() => {
    fetchStatus(true);
    const interval = setInterval(() => fetchStatus(false), 5000);
    return () => clearInterval(interval);
  }, []);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setAccount(prev => ({ ...prev, [name]: value }));
  };

  const handleSave = async () => {
    const email = account.email.trim();
    if (!email) {
      setResultType('error');
      setResultMsg('❌ Email Google là bắt buộc.');
      return;
    }
    try {
      setLoadingMsg('Đang lưu thông tin tài khoản...');
      setResultMsg('');
      const response = await fetch(`${API}/api/flow/account`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email,
          password: account.password,
          totp_secret: account.totp_secret,
        }),
      });
      const data = await readApiResponse(response);
      setAccount(prev => ({
        ...prev,
        email: data.email || email,
        password: '',
        totp_secret: '',
        password_configured: Boolean(data.password_configured),
        totp_configured: Boolean(data.totp_configured),
        session_configured: Boolean(data.session_configured),
      }));
      setResultType('success');
      setResultMsg('✅ Đã lưu thông tin tài khoản Google Flow!');
      await fetchStatus();
    } catch (error) {
      setResultType('error');
      setResultMsg(`❌ Lỗi khi lưu thông tin: ${error.message}`);
    } finally {
      setLoadingMsg('');
    }
  };

  const handleClear = async () => {
    if (!confirm('Xác nhận xóa tài khoản Google Flow hiện tại?')) return;
    try {
      setLoadingMsg('Đang xóa...');
      const response = await fetch(`${API}/api/flow/account`, { method: 'DELETE' });
      await readApiResponse(response);
      setAccount({
        id: '',
        email: '',
        password: '',
        totp_secret: '',
        profile_name: 'PROFILE_GOOGLE_FLOW_1',
        password_configured: false,
        totp_configured: false,
        session_configured: false,
        plan_tier: 'Standard',
        quotas: {},
      });
      setResultType('success');
      setResultMsg('✅ Đã xóa thông tin Google Flow.');
      await fetchStatus();
    } catch (error) {
      setResultType('error');
      setResultMsg(`❌ Lỗi khi xóa: ${error.message}`);
    } finally {
      setLoadingMsg('');
    }
  };

  const handleBrowser = async (action, profileName = '') => {
    const labels = {
      start: 'Đang khởi động trình duyệt Flow...',
      stop: 'Đang dừng trình duyệt Flow...',
      show: 'Đang hiện trình duyệt Flow...',
      hide: 'Đang ẩn trình duyệt Flow...'
    };
    try {
      setLoadingMsg(labels[action] || '...');
      setResultMsg('');
      const response = await fetch(`${API}/api/flow/browser/${action}`, { method: 'POST' });
      const data = await readApiResponse(response);
      setBrowserStatus(data);
      setResultType('success');
      const messages = {
        start: '🌐 Trình duyệt Flow đã khởi động.',
        stop: '⏹️ Trình duyệt Flow đã dừng.',
        show: '👁 Trình duyệt Flow đang hiển thị trên màn hình.',
        hide: '🙈 Trình duyệt Flow đã được ẩn vào nền.'
      };
      setResultMsg(messages[action] || 'Thao tác thành công.');
    } catch (error) {
      setResultType('error');
      setResultMsg(`❌ Lỗi thao tác trình duyệt: ${error.message}`);
    } finally {
      setLoadingMsg('');
    }
  };

  const handleCheckLogin = async () => {
    try {
      setLoadingMsg('⏳ Đang kiểm tra phiên đăng nhập Google Flow...');
      setResultMsg('');
      const res = await fetch(`${API}/api/flow/login-check`, { method: 'POST' });
      const data = await readApiResponse(res);
      if (data.success) {
        setResultType('success');
        setResultMsg(`✅ Đã đăng nhập Google Flow thành công! ${data.message || ''}`);
        await fetchStatus();
      } else {
        setResultType('error');
        setResultMsg(`❌ Chưa đăng nhập: ${data.error || 'Phiên hết hạn'}`);
      }
    } catch (error) {
      setResultType('error');
      setResultMsg(`❌ Lỗi kết nối backend: ${error.message}`);
    } finally {
      setLoadingMsg('');
    }
  };

  const handleCheckQuota = async (accountId = '') => {
    const targetId = accountId || account.id;
    try {
      setCheckingQuotaId(targetId || 'active');
      setLoadingMsg('⚡ Đang kết nối trình duyệt và quét hạn mức các model...');
      setResultMsg('');
      const url = targetId
        ? `${API}/api/flow/accounts/${targetId}/check-quota`
        : `${API}/api/flow/check-quota`;
      const res = await fetch(url, { method: 'POST' });
      const data = await readApiResponse(res);
      if (data.success) {
        setResultType('success');
        setResultMsg(`✅ Đã cập nhật hạn mức các model thành công! Gói: ${data.plan_tier || 'Standard'}`);
        await fetchStatus();
      } else {
        setResultType('error');
        setResultMsg(`❌ Kiểm tra hạn mức thất bại: ${data.error || 'Không rõ nguyên nhân'}`);
      }
    } catch (error) {
      setResultType('error');
      setResultMsg(`❌ Lỗi kiểm tra hạn mức: ${error.message}`);
    } finally {
      setCheckingQuotaId(null);
      setLoadingMsg('');
    }
  };

  const handleCheckAllQuotas = async () => {
    try {
      setIsCheckingAll(true);
      setLoadingMsg('⚡ Đang kiểm tra hạn mức toàn bộ tài khoản Google Flow theo chuỗi...');
      setResultMsg('');
      const res = await fetch(`${API}/api/flow/accounts/check-all-quotas`, { method: 'POST' });
      const data = await readApiResponse(res);
      if (data.success) {
        setResultType('success');
        setResultMsg(`✅ Đã quét xong hạn mức cho ${(data.results || []).length} tài khoản!`);
        await fetchStatus();
      } else {
        setResultType('error');
        setResultMsg(`❌ Lỗi quét toàn bộ: ${data.error || 'Không rõ nguyên nhân'}`);
      }
    } catch (error) {
      setResultType('error');
      setResultMsg(`❌ Lỗi quét toàn bộ: ${error.message}`);
    } finally {
      setIsCheckingAll(false);
      setLoadingMsg('');
    }
  };

  const handleActivateAccount = async (accId) => {
    try {
      setLoadingMsg('Đang kích hoạt tài khoản...');
      const res = await fetch(`${API}/api/flow/accounts/${accId}/activate`, { method: 'POST' });
      await readApiResponse(res);
      setResultType('success');
      setResultMsg(`⭐ Đã đặt làm tài khoản chính!`);
      await fetchStatus(true);
    } catch (error) {
      setResultType('error');
      setResultMsg(`❌ Lỗi kích hoạt: ${error.message}`);
    } finally {
      setLoadingMsg('');
    }
  };

  const handleDeleteAccount = async (accId) => {
    if (!confirm('Xác nhận xóa tài khoản này khỏi danh sách?')) return;
    try {
      setLoadingMsg('Đang xóa tài khoản...');
      const res = await fetch(`${API}/api/flow/accounts/${accId}`, { method: 'DELETE' });
      await readApiResponse(res);
      setResultType('success');
      setResultMsg('✅ Đã xóa tài khoản.');
      await fetchStatus();
    } catch (error) {
      setResultType('error');
      setResultMsg(`❌ Lỗi xóa: ${error.message}`);
    } finally {
      setLoadingMsg('');
    }
  };

  const handleAddAccountSubmit = async (e) => {
    e.preventDefault();
    const email = newAccount.email.trim();
    if (!email) {
      alert('Vui lòng nhập Email Google');
      return;
    }
    try {
      setLoadingMsg('Đang thêm tài khoản mới...');
      const res = await fetch(`${API}/api/flow/accounts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newAccount),
      });
      await readApiResponse(res);
      setNewAccount({ email: '', password: '', totp_secret: '' });
      setShowAddForm(false);
      setResultType('success');
      setResultMsg('✅ Đã thêm tài khoản mới vào hồ chứa Google Flow!');
      await fetchStatus();
    } catch (error) {
      alert(`Lỗi khi thêm: ${error.message}`);
    } finally {
      setLoadingMsg('');
    }
  };

  const isConnected = browserStatus?.connected;
  const isAlive = browserStatus?.process_alive;
  const isInUse = Boolean(browserStatus?.in_use || browserStatus?.generation_active);
  const isVisible = Boolean(browserStatus?.window_visible);
  const isIdlePending = Boolean(browserStatus?.idle_shutdown_at);
  const browserStateLabel = isVisible
    ? '👁 Đang hiển thị'
    : isInUse
      ? '🟢 Đang dùng'
      : isConnected && isIdlePending
        ? '🟡 Chờ tự tắt'
        : isAlive
          ? '🟡 Đang khởi động'
          : '⚪ Chờ tác vụ';
  const browserStateColors = isVisible || isInUse
    ? { background: '#1a472a', color: '#4ade80' }
    : isAlive
      ? { background: '#4a3800', color: '#facc15' }
      : { background: '#263247', color: '#cbd5e1' };

  const currentQuotas = account.quotas || {};
  const activeProfile = account.profile_name || 'PROFILE_GOOGLE_FLOW_1';

  return (
    <div className="auto-login-container" style={{ maxWidth: '980px', margin: '0 auto', padding: '20px' }}>
      <h1 className="hero-title">Google Flow Fleet &amp; Quota Manager</h1>
      <p className="hero-subtitle">
        Quản lý hồ chứa đa tài khoản Google Flow, tự động xoay tua và kiểm tra hạn mức chi tiết cho toàn bộ các model Nano Banana 2.1 &amp; Veo 3.1.
      </p>

      {/* Fleet Overview Bar */}
      {fleet && (
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
          gap: '12px',
          marginBottom: '20px',
        }}>
          <div style={{ background: '#1e293b', padding: '14px', borderRadius: '10px', border: '1px solid #334155' }}>
            <span style={{ fontSize: '0.82em', color: '#94a3b8' }}>👥 Tổng tài khoản</span>
            <div style={{ fontSize: '1.4em', fontWeight: 700, color: '#f8fafc', marginTop: '4px' }}>
              {fleet.total_accounts} <span style={{ fontSize: '0.65em', color: '#38bdf8' }}>accounts</span>
            </div>
          </div>
          <div style={{ background: '#1e293b', padding: '14px', borderRadius: '10px', border: '1px solid #334155' }}>
            <span style={{ fontSize: '0.82em', color: '#94a3b8' }}>🎬 Sẵn sàng Video Veo</span>
            <div style={{ fontSize: '1.4em', fontWeight: 700, color: fleet.veo_ready > 0 ? '#4ade80' : '#f87171', marginTop: '4px' }}>
              {fleet.veo_ready}/{fleet.total_accounts} <span style={{ fontSize: '0.65em' }}>khả dụng</span>
            </div>
          </div>
          <div style={{ background: '#1e293b', padding: '14px', borderRadius: '10px', border: '1px solid #334155' }}>
            <span style={{ fontSize: '0.82em', color: '#94a3b8' }}>🍌 Sẵn sàng Tạo Ảnh</span>
            <div style={{ fontSize: '1.4em', fontWeight: 700, color: fleet.image_ready > 0 ? '#4ade80' : '#f87171', marginTop: '4px' }}>
              {fleet.image_ready}/{fleet.total_accounts} <span style={{ fontSize: '0.65em' }}>khả dụng</span>
            </div>
          </div>
          <div style={{ background: '#1e293b', padding: '14px', borderRadius: '10px', border: '1px solid #334155' }}>
            <span style={{ fontSize: '0.82em', color: '#94a3b8' }}>🤖 Tác nhân Flow Agent</span>
            <div style={{ fontSize: '1.4em', fontWeight: 700, color: fleet.agent_ready > 0 ? '#4ade80' : '#f87171', marginTop: '4px' }}>
              {fleet.agent_ready}/{fleet.total_accounts} <span style={{ fontSize: '0.65em' }}>khả dụng</span>
            </div>
          </div>
        </div>
      )}

      {/* Browser Status */}
      <div className="login-panel" style={{ marginBottom: '16px' }}>
        <h3 style={{ margin: '0 0 12px', color: '#ccc', fontSize: '1em' }}>
          Trạng thái Trình duyệt Flow (Profile: <code style={{ color: '#38bdf8' }}>{activeProfile}</code>)
        </h3>
        <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{
            padding: '4px 12px', borderRadius: '20px', fontSize: '0.85em', fontWeight: 600,
            ...browserStateColors,
          }}>
            {browserStateLabel}
          </span>
          {browserStatus?.cdp_url && (
            <span style={{ fontSize: '0.8em', color: '#888' }}>CDP: {browserStatus.cdp_url}</span>
          )}
          <div style={{ marginLeft: 'auto', display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            <button
              className="btn-secondary"
              style={{ width: 'auto', padding: '8px 16px' }}
              onClick={() => handleBrowser(browserStatus?.window_visible ? 'hide' : 'show')}
            >
              {browserStatus?.window_visible ? '🙈 Ẩn trình duyệt' : '👁 Hiện trình duyệt'}
            </button>
            {!isAlive && (
              <button className="btn-primary" style={{ width: 'auto', padding: '8px 16px' }} onClick={() => handleBrowser('show')}>
                ▶ Khởi động &amp; Mở
              </button>
            )}
            {isAlive && (
              <button
                className="btn-secondary"
                style={{ width: 'auto', padding: '8px 16px' }}
                onClick={() => handleBrowser('stop')}
                disabled={isInUse}
                title={isInUse ? 'Đang có tác vụ sử dụng trình duyệt' : ''}
              >
                ⏹ Dừng
              </button>
            )}
          </div>
        </div>
        <p className="help-text" style={{ margin: '12px 0 0', fontSize: '0.82em', color: '#888' }}>
          Trình duyệt tự mở ở chế độ ẩn khi có tác vụ và tự tắt sau 5 phút rảnh.
          Cửa sổ được hiện thủ công sẽ không tự tắt cho tới khi bạn ẩn lại.
        </p>
      </div>

      {/* Fleet Accounts Table */}
      <div className="login-panel" style={{ marginBottom: '20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '10px' }}>
          <h3 style={{ margin: 0, color: '#f8fafc', fontSize: '1.1em' }}>
            👥 Danh Sách Tài Khoản Google Flow ({accounts.length})
          </h3>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              type="button"
              className="btn-primary"
              style={{ width: 'auto', padding: '6px 14px', fontSize: '0.9em' }}
              disabled={isCheckingAll || isInUse}
              onClick={handleCheckAllQuotas}
            >
              {isCheckingAll ? '⏳ Đang quét tất cả...' : '⚡ Check hạn mức tất cả'}
            </button>
            <button
              type="button"
              className="btn-secondary"
              style={{ width: 'auto', padding: '6px 14px', fontSize: '0.9em' }}
              onClick={() => setShowAddForm(prev => !prev)}
            >
              {showAddForm ? '✕ Đóng form' : '+ Thêm tài khoản'}
            </button>
          </div>
        </div>

        {/* Add Account Inline Form */}
        {showAddForm && (
          <form onSubmit={handleAddAccountSubmit} style={{
            background: '#0f172a',
            padding: '16px',
            borderRadius: '8px',
            border: '1px dashed #38bdf8',
            marginBottom: '16px',
          }}>
            <h4 style={{ margin: '0 0 10px', color: '#38bdf8' }}>Thêm tài khoản Google Flow mới vào hồ chứa</h4>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '10px', marginBottom: '12px' }}>
              <div>
                <label style={{ fontSize: '0.82em', color: '#aaa', display: 'block', marginBottom: '4px' }}>Email Google:</label>
                <input
                  type="email"
                  required
                  value={newAccount.email}
                  onChange={e => setNewAccount(prev => ({ ...prev, email: e.target.value }))}
                  placeholder="name@gmail.com"
                  style={{ width: '100%', padding: '8px', borderRadius: '6px', background: '#1e293b', border: '1px solid #475569', color: '#fff' }}
                />
              </div>
              <div>
                <label style={{ fontSize: '0.82em', color: '#aaa', display: 'block', marginBottom: '4px' }}>Mật khẩu:</label>
                <input
                  type="password"
                  value={newAccount.password}
                  onChange={e => setNewAccount(prev => ({ ...prev, password: e.target.value }))}
                  placeholder="Mật khẩu Google..."
                  style={{ width: '100%', padding: '8px', borderRadius: '6px', background: '#1e293b', border: '1px solid #475569', color: '#fff' }}
                />
              </div>
              <div>
                <label style={{ fontSize: '0.82em', color: '#aaa', display: 'block', marginBottom: '4px' }}>Mã 2FA (TOTP Secret) — Tùy chọn:</label>
                <input
                  type="password"
                  value={newAccount.totp_secret}
                  onChange={e => setNewAccount(prev => ({ ...prev, totp_secret: e.target.value }))}
                  placeholder="Mã TOTP Secret..."
                  style={{ width: '100%', padding: '8px', borderRadius: '6px', background: '#1e293b', border: '1px solid #475569', color: '#fff' }}
                />
              </div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
              <button type="button" className="btn-secondary" style={{ width: 'auto', padding: '6px 14px' }} onClick={() => setShowAddForm(false)}>
                Hủy
              </button>
              <button type="submit" className="btn-primary" style={{ width: 'auto', padding: '6px 16px' }}>
                Lưu vào Pool
              </button>
            </div>
          </form>
        )}

        {/* Accounts Table */}
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '0.88em' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #334155', color: '#94a3b8' }}>
                <th style={{ padding: '8px 10px' }}>Tài khoản</th>
                <th style={{ padding: '8px 10px' }}>Profile Chrome</th>
                <th style={{ padding: '8px 10px' }}>Gói / Phiên</th>
                <th style={{ padding: '8px 10px' }}>Quota Tóm tắt</th>
                <th style={{ padding: '8px 10px', textAlign: 'right' }}>Thao tác</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map(acc => {
                const isCurrentActive = acc.is_active;
                const accQuotas = acc.quotas || {};
                const veoQuota = accQuotas.veo_3_1_quality?.status || 'eligible';
                const imgQuota = accQuotas.nano_banana_2_1?.status || 'eligible';
                const agentQuota = accQuotas.flow_creative_agent?.status || 'eligible';

                return (
                  <tr key={acc.id} style={{
                    borderBottom: '1px solid #1e293b',
                    background: isCurrentActive ? 'rgba(56, 189, 248, 0.08)' : 'transparent',
                  }}>
                    <td style={{ padding: '10px' }}>
                      <div style={{ fontWeight: 600, color: '#f8fafc', display: 'flex', alignItems: 'center', gap: '6px' }}>
                        {isCurrentActive && <span title="Tài khoản đang hoạt động" style={{ color: '#38bdf8' }}>⭐</span>}
                        {acc.email || '(Chưa nhập email)'}
                      </div>
                      <div style={{ fontSize: '0.8em', color: '#64748b' }}>
                        ID: <code>{acc.id}</code>
                      </div>
                    </td>
                    <td style={{ padding: '10px' }}>
                      <span style={{
                        padding: '2px 8px', borderRadius: '4px', background: '#334155', color: '#38bdf8', fontSize: '0.82em', fontFamily: 'monospace'
                      }}>
                        {acc.profile_name}
                      </span>
                    </td>
                    <td style={{ padding: '10px' }}>
                      <span style={{
                        padding: '2px 8px', borderRadius: '12px', fontSize: '0.78em',
                        background: acc.session_configured ? '#14532d' : '#450a0a',
                        color: acc.session_configured ? '#4ade80' : '#f87171',
                        marginRight: '6px'
                      }}>
                        {acc.session_configured ? '✓ Phiên OK' : '✗ Chưa phiên'}
                      </span>
                      <small style={{ color: '#94a3b8' }}>{acc.plan_tier}</small>
                    </td>
                    <td style={{ padding: '10px' }}>
                      <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
                        <span style={{
                          padding: '1px 6px', borderRadius: '4px', fontSize: '0.75em',
                          background: veoQuota === 'exhausted' ? '#7f1d1d' : '#064e3b',
                          color: veoQuota === 'exhausted' ? '#fca5a5' : '#6ee7b7'
                        }}>
                          Veo: {veoQuota === 'exhausted' ? 'Hết' : 'OK'}
                        </span>
                        <span style={{
                          padding: '1px 6px', borderRadius: '4px', fontSize: '0.75em',
                          background: imgQuota === 'exhausted' ? '#7f1d1d' : '#064e3b',
                          color: imgQuota === 'exhausted' ? '#fca5a5' : '#6ee7b7'
                        }}>
                          Ảnh: {imgQuota === 'exhausted' ? 'Hết' : 'OK'}
                        </span>
                        <span style={{
                          padding: '1px 6px', borderRadius: '4px', fontSize: '0.75em',
                          background: agentQuota === 'exhausted' ? '#7f1d1d' : '#064e3b',
                          color: agentQuota === 'exhausted' ? '#fca5a5' : '#6ee7b7'
                        }}>
                          Agent: {agentQuota === 'exhausted' ? 'Hết' : 'OK'}
                        </span>
                      </div>
                    </td>
                    <td style={{ padding: '10px', textAlign: 'right' }}>
                      <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
                        <button
                          type="button"
                          className="btn-primary"
                          style={{ width: 'auto', padding: '4px 10px', fontSize: '0.82em' }}
                          disabled={checkingQuotaId === acc.id || isInUse}
                          onClick={() => handleCheckQuota(acc.id)}
                          title="Quét hạn mức từng model trên tài khoản này"
                        >
                          {checkingQuotaId === acc.id ? '⏳' : '⚡ Check'}
                        </button>
                        {!isCurrentActive && (
                          <button
                            type="button"
                            className="btn-secondary"
                            style={{ width: 'auto', padding: '4px 8px', fontSize: '0.82em' }}
                            onClick={() => handleActivateAccount(acc.id)}
                            title="Chọn làm tài khoản hoạt động mặc định"
                          >
                            ⭐ Kích hoạt
                          </button>
                        )}
                        <button
                          type="button"
                          className="btn-danger"
                          style={{ width: 'auto', padding: '4px 8px', fontSize: '0.82em' }}
                          onClick={() => handleDeleteAccount(acc.id)}
                          title="Xóa tài khoản này"
                        >
                          🗑
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Full Per-Model Quota Breakdown Card */}
      <div className="login-panel" style={{ marginBottom: '20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px', flexWrap: 'wrap', gap: '8px' }}>
          <div>
            <h3 style={{ margin: 0, color: '#f8fafc', fontSize: '1.1em' }}>
              📊 Bảng Kiểm Tra Hạn Mức Từng Model Google Flow
            </h3>
            <span style={{ fontSize: '0.82em', color: '#94a3b8' }}>
              Đang xem cho: <strong style={{ color: '#38bdf8' }}>{account.email || 'Tài khoản chính'}</strong> ({account.plan_tier})
              {account.last_checked_at && (
                <span> · Lần quét cuối: {new Date(account.last_checked_at * 1000).toLocaleTimeString('vi-VN')} {new Date(account.last_checked_at * 1000).toLocaleDateString('vi-VN')}</span>
              )}
            </span>
          </div>
          <button
            type="button"
            className="btn-primary"
            style={{ width: 'auto', padding: '8px 18px', fontSize: '0.9em' }}
            disabled={Boolean(checkingQuotaId) || isInUse}
            onClick={() => handleCheckQuota(account.id)}
          >
            {checkingQuotaId ? '⏳ Đang quét...' : '⚡ Kiểm tra hạn mức'}
          </button>
        </div>

        {/* 9 Models Grid */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '12px' }}>
          {(catalog.length > 0 ? catalog : [
            { id: 'flow_creative_agent', name: 'Flow Creative Agent', category: 'agent', credit_label: 'Theo lượt hội thoại' },
            { id: 'nano_banana_2_1', name: 'Nano Banana 2.1', category: 'image', credit_label: '~1 credit/ảnh' },
            { id: 'nano_banana_2_lite', name: 'Nano Banana 2 Lite', category: 'image', credit_label: '~1 credit/ảnh' },
            { id: 'nano_banana_pro', name: 'Nano Banana Pro', category: 'image', credit_label: '~1 credit/ảnh' },
            { id: 'omni_1_1_flash', name: 'Omni 1.1 Flash', category: 'video', credit_label: '~10 credit/clip' },
            { id: 'veo_3_1_lite', name: 'Veo 3.1 – Lite', category: 'video', credit_label: '~8 credit/clip' },
            { id: 'veo_3_1_fast', name: 'Veo 3.1 – Fast', category: 'video', credit_label: '~12 credit/clip' },
            { id: 'veo_3_1_quality', name: 'Veo 3.1 – Quality', category: 'video', credit_label: '~15–20 credit/clip' },
            { id: 'google_veo_intro', name: 'Google Veo Intro', category: 'video', credit_label: '~10–20 credit/clip' },
          ]).map(m => {
            const q = currentQuotas[m.id] || {};
            const status = q.status || 'unknown';
            const statusLabel = q.status_label || (status === 'eligible' ? 'Còn hạn mức' : status === 'exhausted' ? 'Hết hạn mức' : 'Chưa kiểm tra');
            const statusColor = status === 'eligible' ? '#4ade80' : status === 'exhausted' ? '#f87171' : status === 'warning' ? '#facc15' : '#94a3b8';
            const statusBg = status === 'eligible' ? 'rgba(74, 222, 128, 0.12)' : status === 'exhausted' ? 'rgba(248, 113, 113, 0.12)' : 'rgba(148, 163, 184, 0.12)';

            const catIcon = m.category === 'agent' ? '🤖' : m.category === 'image' ? '🍌' : '🎬';

            return (
              <div key={m.id} style={{
                background: '#0f172a',
                border: `1px solid ${status === 'exhausted' ? '#7f1d1d' : '#1e293b'}`,
                borderRadius: '8px',
                padding: '12px',
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'space-between',
              }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '8px' }}>
                  <div>
                    <span style={{ fontSize: '0.78em', color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                      {catIcon} {m.category}
                    </span>
                    <div style={{ fontWeight: 600, color: '#f8fafc', fontSize: '0.95em', marginTop: '2px' }}>
                      {m.name}
                    </div>
                  </div>
                  <span style={{
                    padding: '2px 8px', borderRadius: '12px', fontSize: '0.75em', fontWeight: 600,
                    background: statusBg, color: statusColor, border: `1px solid ${statusColor}40`
                  }}>
                    {statusLabel}
                  </span>
                </div>

                <div style={{ fontSize: '0.8em', color: '#94a3b8', lineHeight: 1.4, margin: '4px 0 8px' }}>
                  {q.message || m.description || `Mức tiêu thụ: ${m.credit_label}`}
                </div>

                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '0.75em', color: '#64748b', borderTop: '1px solid #1e293b', paddingTop: '6px' }}>
                  <span>Chi phí: {m.credit_label}</span>
                  <code>{m.id}</code>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Account Credentials (Active Account) */}
      <div className="login-panel">
        <h3 style={{ margin: '0 0 16px', color: '#ccc', fontSize: '1em' }}>
          Cấu hình Thông tin Đăng nhập (Tài khoản hiện tại: {account.profile_name})
        </h3>

        <div className="form-group">
          <label>Email Google:</label>
          <input
            type="email"
            name="email"
            value={account.email}
            onChange={handleChange}
            placeholder="Nhập email Google"
          />
        </div>

        <div className="form-group">
          <label>Mật khẩu Google:</label>
          <input
            type="password"
            name="password"
            value={account.password}
            onChange={handleChange}
            placeholder={account.password_configured ? 'Đã lưu — để trống để giữ nguyên' : 'Nhập mật khẩu...'}
          />
        </div>

        <div className="form-group">
          <label>Mã bảo mật 2FA (TOTP Secret) — Tùy chọn:</label>
          <input
            type="password"
            name="totp_secret"
            value={account.totp_secret}
            onChange={handleChange}
            placeholder={account.totp_configured ? 'Đã lưu — để trống để giữ nguyên' : 'ABC123XYZ...'}
          />
        </div>

        {/* Status badges */}
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '16px' }}>
          {[
            { label: 'Email', ok: Boolean(account.email.trim()) },
            { label: 'Mật khẩu', ok: account.password_configured },
            { label: 'TOTP 2FA', ok: account.totp_configured },
            { label: 'Cookie phiên', ok: account.session_configured },
          ].map(({ label, ok }) => (
            <span key={label} style={{
              padding: '3px 10px', borderRadius: '12px', fontSize: '0.8em',
              background: ok ? '#1a3a2a' : '#2a1a1a',
              color: ok ? '#4ade80' : '#888',
            }}>
              {ok ? '✓' : '✗'} {label}
            </span>
          ))}
        </div>

        <p className="help-text" style={{ fontSize: '0.85em', color: '#888' }}>
          Thông tin được mã hóa DPAPI và lưu cục bộ. Bạn có thể đăng nhập thủ công trong trình duyệt Flow;
          sau đó nhấn <strong>Kiểm tra đăng nhập</strong> để xác nhận Google Flow mở được.
        </p>

        <div className="action-buttons">
          <button className="btn-save" onClick={handleSave}>💾 Lưu thông tin</button>
          <button className="btn-primary" onClick={handleCheckLogin}>
            🔍 Kiểm tra đăng nhập
          </button>
          <button
            className="btn-primary"
            style={{ background: '#38bdf8', color: '#000' }}
            disabled={Boolean(checkingQuotaId) || isInUse}
            onClick={() => handleCheckQuota(account.id)}
          >
            {checkingQuotaId ? '⏳ Đang quét...' : '⚡ Kiểm tra hạn mức'}
          </button>
          <button className="btn-danger" onClick={handleClear}>🗑️ Xóa tài khoản</button>
        </div>

        {(loadingMsg || resultMsg) && (
          <div className="status-box">
            {loadingMsg && <p className="loading">{loadingMsg}</p>}
            {resultMsg && <p className={`result ${resultType}`}>{resultMsg}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
