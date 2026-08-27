// ============ 工具函数 ============
const pad2 = n => n < 10 ? '0' + n : String(n);

// 防抖函数
function debounce(fn, delay) {
    let timer = null;
    return function(...args) {
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), delay);
    };
}

// ============ Toast 通知系统 ============
const Toast = {
    container: document.getElementById('toast-container'),

    show(type, title, message, duration = 3000) {
        const icons = { success: '✅', error: '❌', info: 'ℹ️', warning: '⚠️' };
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.innerHTML = `
            <span class="toast-icon">${icons[type] || 'ℹ️'}</span>
            <div class="toast-content">
                <div class="toast-title">${title}</div>
                ${message ? `<div class="toast-message">${message}</div>` : ''}
            </div>
            <button class="toast-close">×</button>
        `;

        toast.querySelector('.toast-close').onclick = () => this.remove(toast);
        this.container.appendChild(toast);

        if (duration > 0) {
            setTimeout(() => this.remove(toast), duration);
        }
        return toast;
    },

    remove(toast) {
        toast.style.animation = 'slideOut 0.3s ease forwards';
        setTimeout(() => toast.remove(), 300);
    },

    success(title, message) { return this.show('success', title, message); },
    error(title, message) { return this.show('error', title, message); },
    info(title, message) { return this.show('info', title, message); },
    warning(title, message) { return this.show('warning', title, message); }
};

// ============ 主题切换 ============
const Theme = {
    init() {
        const saved = localStorage.getItem('theme') || 'light';
        this.set(saved);
    },

    set(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('theme', theme);
        document.getElementById('themeBtn').textContent = theme === 'dark' ? '☀️' : '🌙';
    },

    toggle() {
        const current = document.documentElement.getAttribute('data-theme') || 'light';
        this.set(current === 'dark' ? 'light' : 'dark');
    }
};

// ============ 配置 ============
window.login_url = '';
window.captcha_url = '';
window.__auth = null;
window._configLoaded = false;

async function loadConfig() {
    if (window._configLoaded) return;
    try {
        const resp = await fetch('/api/config');
        const json = await resp.json();
        if (json.ok && json.data) {
            window.login_url = json.data.login_url || '';
            window.captcha_url = json.data.captcha_url || '';
            window._configLoaded = true;
        }
    } catch (e) {
        console.error('加载配置失败:', e);
        window.login_url = window.login_url || 'https://cas.shmtu.edu.cn/cas/login?service=https%3A%2F%2Fwf.shmtu.edu.cn%2Fsso%2Flogin%3Fredirect_uri%3Dhttps%253A%252F%252Fwf.shmtu.edu.cn%252Fsso%252Foauth2%252Fauthorize%253Fclient_id%253DkwxKbMKq3Nafw2mApFZz%2526redirect_uri%253Dhttps%25253A%25252F%25252Fwf.shmtu.edu.cn%25252Fyy-sys%25252Foidc-callback%25253FretUrl%25253Dhttps%25253A%25252F%25252Fwf.shmtu.edu.cn%25252Fyy-sys%25252Fpc%25252Fhome%2526response_type%253Did_token%252520token%2526scope%253Ddata%252520openid%252520process%252520task%252520app%252520submit%252520process_edit%252520start%252520profile%2526state%253D0bfb3977af75474bb82d611a7e4dde78%2526nonce%253Da9c8b134679249aaad587fc2200198a7%26x_client%3Dcas';
        window.captcha_url = window.captcha_url || 'https://cas.shmtu.edu.cn/cas/captcha';
    }
}

// ============ 本地存储 ============
const Auth = {
    save(auth) {
        try { localStorage.setItem('bb_auth', JSON.stringify(auth)); } catch(e) {}
    },
    load() {
        try {
            const s = localStorage.getItem('bb_auth');
            return s ? JSON.parse(s) : null;
        } catch(e) { return null; }
    },
    clear() {
        try { localStorage.removeItem('bb_auth'); } catch(e) {}
    },
    saveToken(token) {
        try { localStorage.setItem('bb_token', token); } catch(e) {}
    },
    loadToken() {
        try { return localStorage.getItem('bb_token'); } catch(e) { return null; }
    },
    clearToken() {
        try { localStorage.removeItem('bb_token'); } catch(e) {}
    }
};

// ============ 全局配置 ============
// 学校固定放号时间（21:00 放第 7 天的场地）。若学校调整放票时间，改这一处即可。
const RUSH_TIME = '21:00';

// 开关文案随模式切换（开=到点自动抢 / 关=点了立刻约），一眼可辨
function syncModeLabel() {
    if (!Elements.modeLabel) return;
    Elements.modeLabel.textContent = State.isScheduleMode ? `${RUSH_TIME} 自动抢` : '立即预订';
}

// ============ 状态管理 ============
const State = {
    isScheduleMode: true,
    pendingCell: null,
    dialogAction: '',
    isLoading: false,
    fetchToken: 0,
    lastJobSig: '',
    submitting: false,
    fastPollTimer: null,
    lastPendingJobId: ''
};

// ============ 前端缓存管理 ============
const AvailabilityCache = {
    _cache: new Map(),
    _ttl: 25000,

    getKey(date) {
        return `${window.__auth?.username || ''}_${date}`;
    },

    get(date) {
        const key = this.getKey(date);
        const entry = this._cache.get(key);
        if (!entry) return null;
        if (Date.now() - entry.ts > this._ttl) {
            this._cache.delete(key);
            return null;
        }
        return entry.data;
    },

    set(date, data) {
        const key = this.getKey(date);
        this._cache.set(key, { data, ts: Date.now() });
    },

    clear() {
        this._cache.clear();
    }
};

// ============ DOM 元素 ============
const Elements = {
    dateInput: document.getElementById('date'),
    modeSwitch: document.getElementById('modeSwitch'),
    modeLabel: document.getElementById('modeLabel'),
    tbody: document.getElementById('schedule-tbody'),
    confirmDialog: document.getElementById('confirm-dialog'),
    loginDialog: document.getElementById('login-dialog'),
    dialogTitle: document.getElementById('dialog-title'),
    dialogInfo: document.getElementById('dialog-info'),
    statusText: document.getElementById('status-text'),
    lastUpdate: document.getElementById('last-update'),
    currentUsername: document.getElementById('current-username'),
    tableContainer: document.getElementById('table-container'),
    refreshBtn: document.getElementById('refreshBtn')
};

function renderCurrentUsername() {
    if (!Elements.currentUsername) return;
    Elements.currentUsername.textContent = window.__auth?.username || '未登录';
}

function setLoading(isLoading, message = '加载中...') {
    State.isLoading = isLoading;
    if (Elements.tableContainer) {
        Elements.tableContainer.classList.toggle('is-loading', isLoading);

        // 显示/隐藏加载覆盖层
        const loadingOverlay = Elements.tableContainer.querySelector('.loading-overlay');
        if (loadingOverlay) {
            loadingOverlay.style.display = isLoading ? 'flex' : 'none';
            const textEl = loadingOverlay.querySelector('.loading-text');
            if (textEl) textEl.textContent = message;
        }
    }
    if (Elements.refreshBtn) Elements.refreshBtn.disabled = isLoading;
    if (Elements.dateInput) Elements.dateInput.disabled = isLoading;
}

async function fetchLocalBookings(date, forceRefresh) {
    let others = [];
    try {
        const cacheBuster = forceRefresh ? `&_t=${Date.now()}` : '';
        const r = await fetch(`/api/local_bookings?bookdate=${encodeURIComponent(date)}${cacheBuster}`);
        const j = await r.json();
        if (j.ok) others = j.data.list || [];
    } catch (e) {}
    return others;
}

// ============ 骨架屏（已由 loading-overlay 承担，保留空实现兼容旧调用） ============
function showSkeleton() {}

// ============ 表格结构（一次性构建 + 单元格索引 + 余位统计） ============
const CellMap = { map: new Map() };

function buildCell(court, hour) {
    const key = court + '-' + hour;
    let cell = CellMap.map.get(key);
    if (cell) return cell;
    cell = document.createElement('td');
    cell.className = 'hour-cell';
    cell.dataset.court = court;
    cell.dataset.hour = hour;
    CellMap.map.set(key, cell);
    return cell;
}

function initTable() {
    const tbody = Elements.tbody;
    tbody.innerHTML = '';
    CellMap.map.clear();
    for (let i = 1; i <= 15; i++) {
        const tr = document.createElement('tr');
        const paddedIndex = i.toString().padStart(2, '0');
        tr.innerHTML = `<td class="court-label">场地 ${paddedIndex}</td>`;
        for (let h = 9; h <= 20; h++) {
            tr.appendChild(buildCell(i, h));
        }
        tbody.appendChild(tr);
    }
}

// 只重置样式，不重建 DOM（刷新时表格不闪烁）
function clearCells() {
    for (const cell of CellMap.map.values()) {
        cell.className = 'hour-cell';
        cell.style.pointerEvents = '';
    }
}

// ============ 初始化日期 ============
function initDate() {
    const today = new Date();
    const sevenDaysLater = new Date(today.getTime() + 7 * 24 * 60 * 60 * 1000);
    Elements.dateInput.value = `${sevenDaysLater.getFullYear()}-${pad2(sevenDaysLater.getMonth() + 1)}-${pad2(sevenDaysLater.getDate())}`;
}

// ============ 获取并渲染预约数据 ============
window.fetchAndRenderBookings = async function(forceRefresh = false) {
    const date = Elements.dateInput.value;
    if (!window.__auth) {
        const ok = await promptLogin();
        if (!ok) {
            Elements.statusText.textContent = '未登录';
            return;
        }
    }
    const fetchId = ++State.fetchToken;
    const t0 = performance.now();

    // 显示骨架屏
    showSkeleton();
    setLoading(true, '正在获取场地数据...');

    // 先检查前端缓存
    let data = null;
    if (!forceRefresh) {
        data = AvailabilityCache.get(date);
    }

    if (data) {
        Elements.statusText.textContent = '从缓存加载...';
    } else {
        Elements.statusText.textContent = '加载中...';
    }

    const localBookingsPromise = fetchLocalBookings(date, forceRefresh);

    try {
        // 如果没有缓存，从服务器获取
        if (!data) {
            setLoading(true, '正在获取场地数据...');

            // 获取 access_token
            const accessToken = window.__token || Auth.loadToken();
            if (!accessToken) {
                // 没有 token，需要重新登录
                Elements.statusText.textContent = '需要登录';
                Toast.error('登录过期', '请重新登录');
                Auth.clear();
                Auth.clearToken();
                window.__auth = null;
                window.__token = null;
                renderCurrentUsername();
                AvailabilityCache.clear();
                const ok = await promptLogin();
                if (ok) {
                    return fetchAndRenderBookings(true);
                }
                return;
            }

            const resp = await fetch('/api/availability', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    token: accessToken,
                    bookdate: date
                })
            });
            data = await resp.json();

            // 存入前端缓存
            if (data.ok) {
                AvailabilityCache.set(date, data);
            }
        }

        if (fetchId !== State.fetchToken) return;

        // 复用既有表格结构，仅重置单元格样式（不再整表重建）。
        // 记录当前处于 pending 的格子：任务仍在跑时，周期性刷新不能把状态洗掉
        const pendingKeys = [...CellMap.map.entries()]
            .filter(([, c]) => c.classList.contains('pending'))
            .map(([k]) => k);
        if (!CellMap.map.size) initTable();
        clearCells();

        if (!data.ok) {
            console.warn('availability error', data.error, data);

            // token 过期或登录失败：先尝试静默续期，失败才弹登录框
            if (data.error === 'login_failed' || data.error === 'no_resources' || data.error === 'token_required') {
                const refreshed = await trySilentRefresh();
                if (refreshed) {
                    Toast.info('登录已续期', '');
                    return fetchAndRenderBookings(true);
                }
                Elements.statusText.textContent = '需要重新登录';
                Toast.warning('登录过期', '请重新登录');
                Auth.clearToken();
                AvailabilityCache.clear();

                // 弹出登录框（预填学号密码）
                const ok = await promptLogin();
                if (ok) {
                    return fetchAndRenderBookings(true);
                }
            } else {
                Elements.statusText.textContent = '加载失败: ' + (data.error || '未知错误');
                Toast.error('加载失败', data.error || '未知错误');
            }
            return;
        }

        const elapsed = Math.round(performance.now() - t0);
        Elements.statusText.textContent = `已更新 (${elapsed}ms)`;

        const now = new Date();
        Elements.lastUpdate.textContent = `上次更新: ${pad2(now.getHours())}:${pad2(now.getMinutes())}:${pad2(now.getSeconds())}`;

        // 获取本地预约记录
        const others = await localBookingsPromise;
        if (fetchId !== State.fetchToken) return;

        const list = data.data?.list || [];

        for (const res of list) {
            const name = res.resources_name || '';
            const m = name.match(/羽毛球(\d+)号场地/);
            if (!m) continue;

            const court = m[1];
            for (const s of res.slots) {
                const hour = (s.kssj || '').split(':')[0];
                const hourInt = parseInt(hour, 10);
                const cell = CellMap.map.get(`${court}-${hourInt}`);
                if (!cell) continue;

                if (s.bookedByMe) {
                    cell.classList.add('selected');
                    cell.title = '我的预约 · 点击可取消排队';
                } else if ((s.canAppointmentNumber ?? 0) <= 0) {
                    cell.classList.add('reserved');
                    cell.title = '已被约满';
                    cell.style.pointerEvents = 'none';
                } else {
                    const me = window.__auth?.username || '';
                    const myEnd = `${(hourInt + 1).toString().padStart(2, '0')}:00`;
                    const hourStr = `${hourInt.toString().padStart(2, '0')}:00`;
                    const match = others.find(o =>
                        o.resources_name === `羽毛球${court}号场地` &&
                        o.kssj === hourStr &&
                        o.jssj === myEnd
                    );
                    if (match) {
                        if (match.username === me) {
                            cell.classList.add('selected');
                            cell.title = '我的预约 · 点击可取消排队';
                        } else {
                            cell.classList.add('others');
                            cell.title = '他人正在排队';
                        }
                    } else {
                        cell.title = '可预约 · 点击预订';
                    }
                }
            }
        }

        // 已过时段降权显示（今天之前的日期整格标灰；仅外观）
        const _now = new Date();
        for (const [key, cell] of CellMap.map) {
            const [, hr] = key.split('-').map(Number);
            const endAt = new Date(`${date}T${String(hr).padStart(2, '0')}:00:00+08:00`);
            const isPast = !isNaN(endAt) && endAt <= _now;
            cell.classList.toggle('past', isPast);
            if (isPast) cell.title = '已过时段';
        }

        // 绘制完成后，把仍在排队中的格子恢复 pending 标记
        for (const key of pendingKeys) {
            const cell = CellMap.map.get(key);
            if (cell && !cell.classList.contains('selected')
                     && !cell.classList.contains('reserved')
                     && !cell.classList.contains('others')) {
                cell.classList.add('pending');
            }
        }
        Elements.statusText.textContent = '已连接';
        Elements.lastUpdate.textContent = `上次更新: ${new Date().toLocaleTimeString()}`;

    } catch (e) {
        console.error('fetch error', e);
        Elements.statusText.textContent = '连接错误';
    } finally {
        if (fetchId === State.fetchToken) {
            setLoading(false);
        }
    }
};

// ============ 静默续期（服务端保存过账号时无需手动重登） ============
async function trySilentRefresh() {
    const username = window.__auth?.username;
    if (!username) return null;
    try {
        const resp = await fetch('/api/auth/refresh', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username })
        });
        const json = await resp.json();
        if (json.ok && json.data?.access_token) {
            window.__token = json.data.access_token;
            Auth.saveToken(window.__token);
            return window.__token;
        }
    } catch (e) {}
    return null;
}

// ============ 提交后的快轮询通道（3s 一次，待确认格清零后自动停止） ============
function armFastPolling() {
    if (State.fastPollTimer) return;
    State.fastPollTimer = setInterval(async () => {
        await pollJobs();
        if (!document.querySelector('.hour-cell.pending')) {
            clearInterval(State.fastPollTimer);
            State.fastPollTimer = null;
        }
    }, 3000);
}

// ============ 轮询后台任务（仅在状态迁移时刷新表格） ============
async function pollJobs() {
    const username = window.__auth?.username;
    const date = Elements.dateInput.value;
    if (!username || !date || State.isLoading) return;

    try {
        const resp = await fetch(`/api/jobs?username=${encodeURIComponent(username)}`);
        const j = await resp.json();
        if (!j.ok) return;

        const mine = (j.data?.db_jobs || []).filter(x => x.bookdate === date);
        // 用任务签名对比：只有状态真的变化才触发刷新，不再每30s重复全量拉取
        const sig = mine.map(x => `${x.job_id}:${x.status}`).sort().join('|');
        if (sig === State.lastJobSig) return;
        State.lastJobSig = sig;

        const finished = mine.some(x =>
            ['done', 'failed', 'cancelled', 'skipped'].includes(String(x.status || '').toLowerCase())
        );
        if (finished) {
            // 首次迁移到终态时给出明确结果提示（签名去重保证只提示一次）
            let t = '';
            if (State.lastPendingJobId) {
                const own = mine.find(x => x.job_id === State.lastPendingJobId);
                if (own && ['done','failed','cancelled','skipped'].includes(String(own.status||'').toLowerCase())) {
                    t = String(own.status).toLowerCase();
                    State.lastPendingJobId = '';
                }
            }
            if (t === 'done')        Toast.success('抢位成功', '预约已确认');
            else if (t === 'skipped') Toast.info('无需重复预约', '您当天已有预约记录');
            else if (t === 'failed')  Toast.error('未能预约成功', '时段可能已被抢完或未通过校验');
            await fetchAndRenderBookings(true);
        }
    } catch (e) {}
}

// ============ 登录弹窗 ============
let _captchaSessionId = null;

async function fetchCaptchaImage() {
    // 获取验证码图片
    try {
        const resp = await fetch('/api/captcha', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                login_url: window.login_url,
                captcha_url: window.captcha_url
            })
        });
        const data = await resp.json();
        if (data.ok && data.data) {
            _captchaSessionId = data.data.session_id;
            return data.data.captcha_image;
        }
        return null;
    } catch (e) {
        console.error('获取验证码失败:', e);
        return null;
    }
}

function showLoginError(message) {
    const errorEl = document.getElementById('login-error');
    if (errorEl) {
        errorEl.textContent = message;
        errorEl.style.display = 'block';
    }
}

function hideLoginError() {
    const errorEl = document.getElementById('login-error');
    if (errorEl) {
        errorEl.style.display = 'none';
    }
}

function showCaptchaField() {
    const captchaField = document.getElementById('captcha-field');
    if (captchaField) {
        captchaField.style.display = 'block';
    }
}

function hideCaptchaField() {
    const captchaField = document.getElementById('captcha-field');
    if (captchaField) {
        captchaField.style.display = 'none';
    }
    const captchaInput = document.getElementById('login-captcha');
    if (captchaInput) {
        captchaInput.value = '';
    }
}

async function performLogin(username, password, captchaCode = null) {
    // 执行登录请求
    try {
        const body = {
            login_url: window.login_url,
            captcha_url: window.captcha_url,
            username,
            password,
            captcha_code: captchaCode
        };
        // 如果有验证码会话ID，传递给后端以复用session
        if (captchaCode && _captchaSessionId) {
            body.session_id = _captchaSessionId;
        }
        const resp = await fetch('/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        return await resp.json();
    } catch (e) {
        console.error('登录请求失败:', e);
        return { ok: false, error: e.message, error_type: 'network_error' };
    }
}

function promptLogin() {
    return new Promise(resolve => {
        const dlg = Elements.loginDialog;
        const form = document.getElementById('login-form');
        const uEl = document.getElementById('login-username');
        const pEl = document.getElementById('login-password');
        const cancelBtn = document.getElementById('login-cancel');
        const togglePwd = document.getElementById('togglePwd');
        const captchaImg = document.getElementById('captcha-image');
        const captchaInput = document.getElementById('login-captcha');
        const submitBtn = document.getElementById('login-submit');
        const titleEl = document.getElementById('login-dialog-title');

        if (!dlg || !form || !uEl || !pEl || !cancelBtn || !togglePwd) {
            resolve(false);
            return;
        }

        const closeDialog = () => {
            if (typeof dlg.close === 'function') {
                dlg.close();
            } else {
                dlg.removeAttribute('open');
            }
        };

        const openDialog = () => {
            if (typeof dlg.showModal === 'function') {
                if (!dlg.open) dlg.showModal();
            } else {
                dlg.setAttribute('open', 'open');
            }
        };

        const resetDialog = () => {
            hideLoginError();
            hideCaptchaField();
            if (titleEl) titleEl.textContent = '登录';
            if (submitBtn) submitBtn.textContent = '登录';
            _captchaSessionId = null;
        };

        togglePwd.onclick = () => {
            const isPwd = pEl.type === 'password';
            pEl.type = isPwd ? 'text' : 'password';
            togglePwd.textContent = isPwd ? '隐藏' : '显示';
        };

        // 验证码图片点击刷新
        if (captchaImg) {
            captchaImg.onclick = async () => {
                const img = await fetchCaptchaImage();
                if (img) {
                    captchaImg.src = img;
                }
            };
        }

        cancelBtn.onclick = () => {
            resetDialog();
            closeDialog();
            resolve(false);
        };

        form.onsubmit = async (e) => {
            e.preventDefault();
            const u = (uEl.value || '').trim();
            const p = (pEl.value || '').trim();
            if (!u || !p) {
                Toast.error('登录失败', '请输入学号与密码');
                return;
            }

            // 检查是否需要手动输入验证码
            const captchaField = document.getElementById('captcha-field');
            const needManualCaptcha = captchaField && captchaField.style.display !== 'none';
            const captchaCode = needManualCaptcha && captchaInput ? captchaInput.value.trim() : null;

            if (needManualCaptcha && !captchaCode) {
                Toast.error('登录失败', '请输入验证码');
                return;
            }

            // 禁用提交按钮，显示加载状态
            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.textContent = '登录中...';
            }

            try {
                // 执行登录
                const result = await performLogin(u, p, captchaCode);

                if (result.ok) {
                    // 登录成功，保存 token
                    window.__auth = { username: u, password: p };
                    window.__token = result.data?.access_token;
                    Auth.save(window.__auth);
                    if (window.__token) {
                        Auth.saveToken(window.__token);
                    }
                    renderCurrentUsername();
                    resetDialog();
                    closeDialog();
                    resolve(true);
                } else if (result.need_manual_captcha) {
                    // 需要手动输入验证码
                    showCaptchaField();
                    const img = await fetchCaptchaImage();
                    if (img && captchaImg) {
                        captchaImg.src = img;
                    }
                    showLoginError('验证码识别失败，请手动输入');
                    if (titleEl) titleEl.textContent = '登录 (验证码)';
                } else if (result.error_type === 'password_error') {
                    // 密码错误
                    showLoginError('用户名或密码错误');
                    if (titleEl) titleEl.textContent = '登录 (密码错误)';
                } else if (result.error_type === 'captcha_error') {
                    // 验证码错误（手动输入后仍然错误）
                    showLoginError('验证码错误，请重新输入');
                    // 刷新验证码图片
                    const img = await fetchCaptchaImage();
                    if (img && captchaImg) {
                        captchaImg.src = img;
                    }
                    if (captchaInput) captchaInput.value = '';
                } else {
                    // 其他错误
                    showLoginError(result.error || '登录失败');
                }
            } catch (err) {
                showLoginError(err.message || '登录请求失败');
            } finally {
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = '登录';
                }
            }
        };

        // 预填缓存
        const cached = Auth.load();
        if (cached) {
            uEl.value = cached.username || '';
            pEl.value = cached.password || '';
        }

        resetDialog();
        openDialog();
    });
}

// ============ 单元格点击处理 ============
async function handleCellClick(cell) {
    if (State.isLoading) {
        Toast.info('请等待', '数据加载中');
        return;
    }
    if (cell.classList.contains('pending')) {
        Toast.info('处理中', '该时段的预约请求正在执行');
        return;
    }
    if (State.submitting) {
        Toast.info('请稍候', '上一笔预约仍在处理中');
        return;
    }
    if (!window.__auth) {
        const ok = await promptLogin();
        if (!ok) return;
    }
    if (cell.classList.contains('reserved')) return;

    const date = Elements.dateInput.value;
    const court = cell.dataset.court;
    const hour = cell.dataset.hour;
    const end = (parseInt(hour) + 1).toString().padStart(2, '0');

    State.pendingCell = cell;

    // 判断是否是他人的预约
    if (cell.classList.contains('others')) {
        Toast.warning('无法取消', '这是其他用户的预约任务，您无权取消');
        return;
    }

    if (cell.classList.contains('selected')) {
        // 取消自己的预约
        Elements.dialogTitle.textContent = '❌ 取消预约';
        State.dialogAction = 'cancel';
    } else {
        // 新预约
        Elements.dialogTitle.textContent = State.isScheduleMode ? '⏰ 定时抢票' : '✅ 确认预约';
        State.dialogAction = 'book';
    }

    Elements.dialogInfo.innerHTML = `
        <div class="dialog-info-item">
            <span class="dialog-info-label">日期</span>
            <span class="dialog-info-value">${date}</span>
        </div>
        <div class="dialog-info-item">
            <span class="dialog-info-label">场地</span>
            <span class="dialog-info-value">羽毛球 ${court} 号场地</span>
        </div>
        <div class="dialog-info-item">
            <span class="dialog-info-label">时间</span>
            <span class="dialog-info-value">${hour}:00 - ${end}:00</span>
        </div>
        ${State.dialogAction === 'book' && State.isScheduleMode ? `
        <div class="dialog-info-item">
            <span class="dialog-info-label">模式</span>
            <span class="dialog-info-value" style="color: var(--primary)">${RUSH_TIME} 自动抢票</span>
        </div>
        ` : ''}
    `;

    Elements.confirmDialog.showModal();
}

// ============ 确认/取消操作 ============
async function handleDialogConfirm() {
    const cell = State.pendingCell;
    if (!cell) return;

    const date = Elements.dateInput.value;
    const court = cell.dataset.court;
    const hour = cell.dataset.hour;
    const end = (parseInt(hour) + 1).toString().padStart(2, '0');

    Elements.confirmDialog.close();

    if (State.submitting) {
        Toast.info('请稍候', '上一笔预约仍在处理中');
        return;
    }
    State.submitting = true;
    State.pendingCell = null;

    try {
        if (State.dialogAction === 'book') {
            const username = window.__auth?.username;
            const password = window.__auth?.password;
            if (!username || !password) {
                Toast.error('未登录', '请先登录');
                return;
            }

            const baseBody = {
                login_url: window.login_url,
                captcha_url: window.captcha_url,
                username, password,
                bookdate: date,
                kssj: `${hour}:00`,
                jssj: `${end}:00`,
                resources_name: `羽毛球${court}号场地`
            };
            const postJson = (path, body) => fetch(path, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            }).then(r => r.json());

            let data;

            if (State.isScheduleMode) {
                // 定时抢票：入队即返回
                const body = { ...baseBody, target_time_str: `${RUSH_TIME}:00`, num_threads: 2 };
                data = await postJson('/api/book/schedule', body);
                if (!data.ok && data.error === 'login_failed') {
                    const refreshed = await trySilentRefresh();
                    if (refreshed) data = await postJson('/api/book/schedule', body);
                }
                if (data.ok) {
                    cell.classList.add('selected');
                    Toast.success('已排队', `已加入${RUSH_TIME}抢票队列`);
                }
            } else {
                // 即时预约：走异步任务接口，毫秒级返回，结果经轮询刷新
                data = await postJson('/api/jobs/immediate', baseBody);
                if (!data.ok && data.error === 'login_failed') {
                    const refreshed = await trySilentRefresh();
                    if (refreshed) data = await postJson('/api/jobs/immediate', baseBody);
                }
                if (data.ok) {
                    cell.classList.add('pending');
                    State.lastPendingJobId = data.data?.job_id || '';
                    State.lastJobSig = '';   // 重置签名，让快轮询能感知状态迁移
                    armFastPolling();
                    Toast.info('已提交', '正在为您抢位，请留意格子变化…');
                }
            }

            if (!data.ok) {
                // 资源已被其他用户预约
                if (data.error === 'resource_already_booked') {
                    Toast.error('预约失败', '该时间段已被其他用户预约');
                    await fetchAndRenderBookings(true);
                    return;
                }
                // 登录失败（含刷新失败），需要重新登录
                if (data.error === 'login_failed') {
                    Toast.error('登录失败', '账号或密码错误，请重新登录');
                    Auth.clear();
                    window.__auth = null;
                    renderCurrentUsername();
                    AvailabilityCache.clear();
                    const okLogin = await promptLogin();
                    if (okLogin) {
                        await fetchAndRenderBookings(true);
                    }
                    return;
                }
                Toast.error('提交失败', data.error || '未知错误');
            }
        } else if (State.dialogAction === 'cancel') {
            const username = window.__auth?.username;
            let stopData = null;
            if (username) {
                const resp = await fetch('/api/jobs/stop_by_params', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        username,
                        bookdate: date,
                        kssj: `${hour}:00`,
                        jssj: `${end}:00`,
                        resources_name: `羽毛球${court}号场地`,
                        current_username: username,
                        access_token: window.__token || Auth.loadToken() || ''
                    })
                });
                const data = await resp.json();
                if (!data.ok) {
                    Toast.error('取消失败', data.data?.message || data.data?.error || '无权取消此任务');
                    return;
                }
                stopData = data.data || {};
            }
            // 先移除样式，立即给用户反馈
            cell.classList.remove('selected', 'others', 'pending');
            // 按上游撤销结果给出如实提示
            const status = stopData?.upstream_status;
            if (status === 'cancelled') {
                Toast.success('已取消', '学校侧预约已撤销');
            } else if (status === 'failed') {
                Toast.warning('已停止排队', `学校侧撤销失败：${stopData.message || '未知原因'}，请到学校网站确认`, 5000);
            } else if (stopData?.message) {
                Toast.info('已取消排队', stopData.message, 4500);
            } else {
                Toast.success('已取消', '预约已取消');
            }
            // 强制刷新数据，跳过缓存
            await fetchAndRenderBookings(true);
        }
    } catch (e) {
        Toast.error('请求异常', e.message);
        cell.classList.remove('pending');
    } finally {
        State.submitting = false;
    }
}

// ============ 事件绑定 ============
function bindEvents() {
    // 主题切换
    document.getElementById('themeBtn').addEventListener('click', () => Theme.toggle());

    // 模式切换
    Elements.modeSwitch.addEventListener('click', () => {
        State.isScheduleMode = !State.isScheduleMode;
        Elements.modeSwitch.classList.toggle('active', State.isScheduleMode);
        syncModeLabel();
        Toast.info('模式切换', State.isScheduleMode ? `已切换为${RUSH_TIME}自动抢票` : '已切换为立即预订');
    });

    // 日期变化
    Elements.dateInput.addEventListener('change', debounce(async () => {
        await fetchAndRenderBookings();
        Toast.info('数据已更新', '');
    }, 300));

    // 刷新按钮 - 强制刷新，跳过缓存
    document.getElementById('refreshBtn').addEventListener('click', async () => {
        await fetchAndRenderBookings(true);
        Toast.success('刷新成功', '');
    });

    // 退出登录
    document.getElementById('logoutBtn').addEventListener('click', async () => {
        const username = window.__auth?.username;

        // 清除服务端 token 缓存
        if (username) {
            try {
                await fetch('/api/logout', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username })
                });
            } catch(e) {
                console.warn('logout api failed', e);
            }
        }

        // 清除前端缓存
        Auth.clear();
        Auth.clearToken();
        window.__auth = null;
        window.__token = null;
        renderCurrentUsername();
        AvailabilityCache.clear();
        document.querySelectorAll('.hour-cell').forEach(cell => {
            cell.className = 'hour-cell';
        });
        Elements.statusText.textContent = '未登录';
        Toast.info('已退出登录', '');
        const ok = await promptLogin();
        if (ok) {
            await fetchAndRenderBookings(true);
            Toast.success('登录成功', '');
        }
    });

    // 聚焦系统（变暗式，不叠色）：
    //   悬停格子        -> 行 + 列 同时保持明亮（十字），其余变暗
    //   悬停场地标签/表头/底部统计 -> 单轴聚焦
    const gridTable = document.getElementById('schedule-table');
    let fxKey = null;   // "row:c" / "col:h" / null
    const clearOrigin = () => {};
    const setFx = (key) => {
        if (key === fxKey) { return; }
        gridTable.classList.toggle('dimming', key !== null);
        const parts = key ? key.split(':') : [];
        const kind = parts[0];
        for (const [ck, cell] of CellMap.map) {
            const seg = ck.split('-');
            const on =
                !key ? false :
                kind === 'row' ? seg[0] === parts[1] :
                                 seg[1] === parts[1];
            cell.classList.toggle('fx-on', on);
        }
        fxKey = key;
    };

    // 注：格子悬停的「列名发光」已改为纯 CSS :has() 声明式实现（见 main.css），
    // 与行名同为浏览器原生悬停态，无 JS 状态、不会残留。
    Elements.tableContainer.addEventListener('mouseover', (e) => {
        const courtTag = e.target.closest('.court-label');
        if (courtTag) {
            const tr = courtTag.closest('tr');
            const cell = tr && tr.querySelector('.hour-cell');
            setFx(cell ? `row:${cell.dataset.court}` : null);
            return;
        }
        const hourTag = e.target.closest('th[data-hour], .foot-num');
        if (hourTag) {
            setFx(`col:${hourTag.dataset.hour || hourTag.dataset.footHour}`);
            return;
        }
        setFx(null);
    });
    Elements.tableContainer.addEventListener('mouseleave', () => { setFx(null); });



    // 表格点击 (事件委托)
    Elements.tbody.addEventListener('click', (e) => {
        const cell = e.target.closest('.hour-cell');
        if (cell) { void handleCellClick(cell); }
    });

    // 弹窗按钮
    document.getElementById('dialog-cancel').addEventListener('click', () => {
        Elements.confirmDialog.close();
        State.pendingCell = null;
    });
    document.getElementById('dialog-ok').addEventListener('click', handleDialogConfirm);

    // ESC 关闭弹窗
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            Elements.confirmDialog.close();
            State.pendingCell = null;
        }
    });
}

// ============ 初始化 ============
async function init() {
    Theme.init();
    initTable();
    initDate();
    syncModeLabel();
    bindEvents();

    // 先加载配置
    await loadConfig();

    // 尝试使用缓存登录
    const cached = Auth.load();
    const cachedToken = Auth.loadToken();
    if (cached && cached.username && cached.password && cachedToken) {
        window.__auth = cached;
        window.__token = cachedToken;
    } else {
        const ok = await promptLogin();
        if (!ok) {
            Toast.error('未登录', '无法加载数据');
            return;
        }
    }

    renderCurrentUsername();
    await fetchAndRenderBookings();
    Toast.success('加载完成', '数据已更新');

    // 定时刷新
    setInterval(async () => {
        if (window.__auth) await fetchAndRenderBookings();
    }, 60000);

    // 轮询任务状态（30秒）
    setInterval(pollJobs, 30000);
}

// 启动
document.addEventListener('DOMContentLoaded', init);
// cache bust: 20260827
