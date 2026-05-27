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
            console.log('配置加载成功');
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

// ============ 状态管理 ============
const State = {
    isScheduleMode: true,
    pendingCell: null,
    dialogAction: '',
    isLoading: false,
    fetchToken: 0
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
    Elements.currentUsername.textContent = window.__auth?.username || 'Not logged in';
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

// ============ 骨架屏 ============
function showSkeleton() {
    const tbody = Elements.tbody;
    tbody.innerHTML = '';

    for (let i = 1; i <= 15; i++) {
        const tr = document.createElement('tr');
        tr.className = 'skeleton-row';
        tr.innerHTML = `
            <td class="skeleton skeleton-label"></td>
            ${Array(12).fill('<td class="skeleton skeleton-cell"></td>').join('')}
        `;
        tbody.appendChild(tr);
    }
}

// ============ 初始化表格 ============
function initTable() {
    const tbody = Elements.tbody;
    tbody.innerHTML = '';

    for (let i = 1; i <= 15; i++) {
        const tr = document.createElement('tr');
        const paddedIndex = i.toString().padStart(2, '0');
        tr.innerHTML = `<td class="court-label">场地 ${paddedIndex}</td>`;

        for (let h = 9; h <= 20; h++) {
            const td = document.createElement('td');
            td.className = 'hour-cell';
            td.dataset.court = i;
            td.dataset.hour = h;
            tr.appendChild(td);
        }
        tbody.appendChild(tr);
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
    console.log('fetchAndRenderBookings - date:', date);
    if (!window.__auth) {
        const ok = await promptLogin();
        if (!ok) {
            Elements.statusText.textContent = '未登录';
            return;
        }
    }
    const token = ++State.fetchToken;
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
            console.log('发送 availability 请求...');
            setLoading(true, '正在获取场地数据...');

            // 获取 token
            const token = window.__token || Auth.loadToken();
            console.log('Token 状态:', token ? '存在' : '不存在', 'window.__token:', window.__token ? '有' : '无', 'localStorage:', Auth.loadToken() ? '有' : '无');
            if (!token) {
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
                    token: token,
                    bookdate: date
                })
            });
            console.log('请求体:', JSON.stringify({ token: token?.substring(0, 20) + '...', bookdate: date }));
            data = await resp.json();
            console.log('availability 响应:', data);

            // 存入前端缓存
            if (data.ok) {
                AvailabilityCache.set(date, data);
            }
        } else {
            console.log('使用前端缓存');
        }

        if (token !== State.fetchToken) return;

        // 重置表格（移除骨架屏）
        initTable();

        // 重置所有单元格
        document.querySelectorAll('.hour-cell').forEach(cell => {
            cell.className = 'hour-cell';
            cell.style.pointerEvents = '';
        });

        if (!data.ok) {
            console.warn('availability error', data.error, data);

            // 登录失败，需要重新输入账号密码
            if (data.error === 'login_failed') {
                Elements.statusText.textContent = '登录失败';
                Toast.error('登录失败', '账号或密码错误，请重新登录');
                Auth.clear();
                window.__auth = null;
                renderCurrentUsername();
                AvailabilityCache.clear();

                // 弹出登录框
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
        if (token !== State.fetchToken) return;

        const list = data.data?.list || [];

        for (const res of list) {
            const name = res.resources_name || '';
            const m = name.match(/羽毛球(\d+)号场地/);
            if (!m) continue;

            const court = m[1];
            for (const s of res.slots) {
                const hour = (s.kssj || '').split(':')[0];
                const hourInt = parseInt(hour, 10);
                const cell = document.querySelector(`.hour-cell[data-court='${court}'][data-hour='${hourInt}']`);
                if (!cell) continue;

                if (s.bookedByMe) {
                    cell.classList.add('selected');
                } else if ((s.canAppointmentNumber ?? 0) <= 0) {
                    cell.classList.add('reserved');
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
                        } else {
                            cell.classList.add('others');
                        }
                    }
                }
            }
        }

        Elements.statusText.textContent = '已连接';
        Elements.lastUpdate.textContent = `上次更新: ${new Date().toLocaleTimeString()}`;

    } catch (e) {
        console.error('fetch error', e);
        Elements.statusText.textContent = '连接错误';
    } finally {
        if (token === State.fetchToken) {
            setLoading(false);
        }
    }
};

// ============ 轮询后台任务 ============
async function pollJobs() {
    const username = window.__auth?.username;
    const date = Elements.dateInput.value;
    if (!username || !date) return;

    try {
        const resp = await fetch('/api/jobs');
        const j = await resp.json();
        if (!j.ok) return;

        const all = j.data?.db_jobs || [];
        const mine = all.filter(x => x.username === username && x.bookdate === date);
        if (mine.length === 0) return;

        const finished = mine.some(x =>
            ['done', 'failed', 'cancelled', 'skipped'].includes(String(x.status || '').toLowerCase())
        );
        if (finished) {
            await fetchAndRenderBookings();
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
            if (titleEl) titleEl.textContent = '🔐 登录';
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
                    console.log('登录成功, token:', window.__token ? '已获取' : '未获取');
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
                    if (titleEl) titleEl.textContent = '🔐 登录 (验证码)';
                } else if (result.error_type === 'password_error') {
                    // 密码错误
                    showLoginError('用户名或密码错误');
                    if (titleEl) titleEl.textContent = '🔐 登录 (密码错误)';
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
            <span class="dialog-info-value" style="color: var(--primary)">21:00 自动抢票</span>
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

    if (State.dialogAction === 'book') {
        try {
            const username = window.__auth?.username;
            const password = window.__auth?.password;
            if (!username || !password) {
                Toast.error('未登录', '请先登录');
                return;
            }

            let resp;
            if (State.isScheduleMode) {
                resp = await fetch('/api/book/schedule', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        login_url: window.login_url,
                        captcha_url: window.captcha_url,
                        username, password,
                        bookdate: date,
                        kssj: `${hour}:00`,
                        jssj: `${end}:00`,
                        resources_name: `羽毛球${court}号场地`,
                        target_time_str: '21:00:00',
                        num_threads: 5,
                        run_async: true
                    })
                });
            } else {
                resp = await fetch('/api/book', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        login_url: window.login_url,
                        captcha_url: window.captcha_url,
                        username, password,
                        bookdate: date,
                        kssj: `${hour}:00`,
                        jssj: `${end}:00`,
                        resources_name: `羽毛球${court}号场地`
                    })
                });
            }

            const data = await resp.json();
            if (data.ok) {
                cell.classList.add('selected');
                Toast.success('预约成功', State.isScheduleMode ? '已加入21:00抢票队列' : '预约请求已提交');
            } else {
                // 资源已被其他用户预约
                if (data.error === 'resource_already_booked') {
                    Toast.error('预约失败', '该时间段已被其他用户预约');
                    await fetchAndRenderBookings(true);
                    return;
                }
                // 登录失败，需要重新登录
                if (data.error === 'login_failed') {
                    Toast.error('登录失败', '账号或密码错误，请重新登录');
                    Auth.clear();
                    window.__auth = null;
                    renderCurrentUsername();
                    AvailabilityCache.clear();
                    const ok = await promptLogin();
                    if (ok) {
                        await fetchAndRenderBookings(true);
                    }
                    return;
                }
                Toast.error('预约失败', data.error || '未知错误');
            }
        } catch (e) {
            Toast.error('请求异常', e.message);
        }
    } else if (State.dialogAction === 'cancel') {
        try {
            const username = window.__auth?.username;
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
                        current_username: username
                    })
                });
                const data = await resp.json();
                if (!data.ok) {
                    Toast.error('取消失败', data.data?.message || '无权取消此任务');
                    return;
                }
            }
            // 先移除样式，立即给用户反馈
            cell.classList.remove('selected', 'others');
            Toast.success('已取消', '预约已取消');
            // 强制刷新数据，跳过缓存
            await fetchAndRenderBookings(true);
        } catch (e) {
            Toast.error('取消失败', e.message);
        }
    }

    State.pendingCell = null;
}

// ============ 事件绑定 ============
function bindEvents() {
    // 主题切换
    document.getElementById('themeBtn').addEventListener('click', () => Theme.toggle());

    // 模式切换
    Elements.modeSwitch.addEventListener('click', () => {
        State.isScheduleMode = !State.isScheduleMode;
        Elements.modeSwitch.classList.toggle('active', State.isScheduleMode);
        Toast.info('模式切换', State.isScheduleMode ? '已切换为21:00抢票模式' : '已切换为直接预约模式');
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

    // 轮询任务状态（10秒）
    setInterval(pollJobs, 10000);
}

// 启动
document.addEventListener('DOMContentLoaded', init);
// cache bust: 1779900696
