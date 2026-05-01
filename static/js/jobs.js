// Jobs 页面脚本

// ============ 本地存储管理 ============
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
    }
};

// 全局认证对象
window.__auth = null;

// ============ 授权检查 ============
async function checkAuthorization(username) {
    try {
        const resp = await fetch(`/api/auth/check?username=${encodeURIComponent(username)}`);
        const data = await resp.json();
        return data.ok && data.authorized;
    } catch (e) {
        console.error('授权检查失败:', e);
        return false;
    }
}

// ============ 登录对话框 ============
const loginDialog = document.getElementById('login-dialog');
const loginForm = document.getElementById('login-form');
const loginUsernameEl = document.getElementById('login-username');
const loginPasswordEl = document.getElementById('login-password');
const togglePwdBtn = document.getElementById('togglePwd');
const loginCancelBtn = document.getElementById('login-cancel');

async function promptLogin() {
    return new Promise(resolve => {
        togglePwdBtn.onclick = () => {
            const isPwd = loginPasswordEl.type === 'password';
            loginPasswordEl.type = isPwd ? 'text' : 'password';
            togglePwdBtn.textContent = isPwd ? '隐藏' : '显示';
        };

        loginCancelBtn.onclick = () => {
            loginDialog.close();
            resolve(false);
        };

        loginForm.onsubmit = async (e) => {
            e.preventDefault();
            const u = (loginUsernameEl.value || '').trim();
            const p = (loginPasswordEl.value || '').trim();
            if (!u || !p) {
                Toast.error('登录失败', '请输入学号与密码');
                return;
            }

            // 通过后端 API 验证是否为授权用户
            const isAuthorized = await checkAuthorization(u);
            if (!isAuthorized) {
                Toast.error('权限不足', '您没有权限访问该页面');
                return;
            }

            window.__auth = { username: u, password: p };
            Auth.save(window.__auth);
            loginDialog.close();
            resolve(true);
        };

        // 预填缓存
        const cached = Auth.load();
        if (cached) {
            loginUsernameEl.value = cached.username || '';
            loginPasswordEl.value = cached.password || '';
        }

        loginDialog.showModal();
    });
}

// ============ Toast 通知系统 ============
const Toast = {
    container: document.getElementById('toast-container'),

    show(type, title, message, duration = 3000) {
        const icons = { success: '✅', error: '❌', info: 'ℹ️' };
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

        if (duration > 0) setTimeout(() => this.remove(toast), duration);
        return toast;
    },

    remove(toast) {
        toast.style.animation = 'slideOut 0.3s ease forwards';
        setTimeout(() => toast.remove(), 300);
    },

    success(title, message) { return this.show('success', title, message); },
    error(title, message) { return this.show('error', title, message); },
    info(title, message) { return this.show('info', title, message); }
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

// ============ DOM 元素 ============
const tbody = document.getElementById('jobs-tbody');
const paramsDialog = document.getElementById('params-dialog');
const paramsSummary = document.getElementById('params-summary');

// ============ 获取任务列表 ============
async function fetchJobs() {
    try {
        const resp = await fetch('/api/jobs');
        const data = await resp.json();

        if (!data.ok) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="6">
                        <div class="empty-state">
                            <div class="empty-icon">❌</div>
                            <div class="empty-text">加载失败</div>
                        </div>
                    </td>
                </tr>
            `;
            return;
        }

        // 合并活跃任务和数据库历史任务
        const memoryJobs = data.data.jobs || [];
        const dbJobs = data.data.db_jobs || [];

        // 构建内存任务的ID映射（用于去重）
        const memoryJobIds = new Set(memoryJobs.map(j => j.job_id));

        // 将数据库任务转换为统一格式，并过滤掉内存中已有的
        const dbJobsFormatted = dbJobs
            .filter(j => !memoryJobIds.has(j.job_id))
            .map(j => ({
                job_id: j.job_id,
                alive: false,
                type: j.target_time_str ? 'scheduled' : 'immediate',
                created_at: j.created_at,
                status: j.status,
                params: {
                    username: j.username,
                    bookdate: j.bookdate,
                    kssj: j.kssj,
                    jssj: j.jssj,
                    resources_name: j.resources_name,
                    target_time_str: j.target_time_str,
                    num_threads: j.num_threads
                }
            }));

        // 合并两个列表，按创建时间倒序排序
        const allJobs = [...memoryJobs, ...dbJobsFormatted]
            .sort((a, b) => b.created_at - a.created_at);

        updateStats(allJobs);
        renderJobs(allJobs);

        document.getElementById('last-update').textContent =
            `上次更新: ${new Date().toLocaleTimeString()}`;

    } catch (e) {
        console.error('fetch error', e);
        Toast.error('加载失败', e.message);
    }
}

// ============ 更新统计 ============
function updateStats(jobs) {
    const total = jobs.length;
    const running = jobs.filter(j => j.alive).length;
    const done = jobs.filter(j => !j.alive && j.status === 'done').length;
    const failed = jobs.filter(j => !j.alive && j.status === 'failed').length;

    document.getElementById('stat-total').textContent = total;
    document.getElementById('stat-running').textContent = running;
    document.getElementById('stat-done').textContent = done;
    document.getElementById('stat-failed').textContent = failed;
}

// ============ 渲染任务列表 ============
function renderJobs(jobs) {
    if (jobs.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6">
                    <div class="empty-state">
                        <div class="empty-icon">📭</div>
                        <div class="empty-text">暂无任务</div>
                        <div class="empty-subtext">返回预约页面创建新任务</div>
                    </div>
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = jobs.map(job => {
        const statusClass = job.alive ? 'running' : (job.status || 'done');
        const statusText = job.alive ? '执行中' : getStatusText(job.status, job.type);
        const createdAt = new Date(job.created_at * 1000).toLocaleString();

        return `
            <tr>
                <td><span class="job-id">${job.job_id.substring(0, 8)}...</span></td>
                <td>${getTypeText(job.type)}</td>
                <td>${createdAt}</td>
                <td><span class="status-badge ${statusClass}">${statusText}</span></td>
                <td class="params-preview">
                    <button class="params-btn" onclick="showParams('${encodeURIComponent(JSON.stringify(job.params, null, 2))}')">
                        查看参数
                    </button>
                </td>
                <td>
                    ${job.alive ? `
                        <button class="btn btn-danger btn-sm" onclick="stopJob('${job.job_id}')">
                            停止
                        </button>
                    ` : '-'}
                </td>
            </tr>
        `;
    }).join('');
}

function getStatusText(status, type) {
    const map = {
        'done': '✅ 预约成功',
        'failed': '❌ 失败',
        'cancelled': '已取消',
        'scheduled': '待执行',
        'running': '执行中',
        'skipped': '⏭️ 已跳过'
    };
    return map[status] || status || '已结束';
}

function getTypeText(type) {
    const map = {
        'immediate': '⚡ 即时任务',
        'scheduled': '⏰ 定时任务'
    };
    return map[type] || type || '-';
}

// ============ 显示参数 ============
function showParams(encodedParams) {
    const raw = decodeURIComponent(encodedParams);

    let params = {};
    try {
        params = JSON.parse(raw);
    } catch (e) {
        params = {};
    }

    const fields = [
        ['username', '用户'],
        ['bookdate', '日期'],
        ['kssj', '开始时间'],
        ['jssj', '结束时间'],
        ['resources_name', '场地'],
        ['target_time_str', '抢票时间'],
        ['num_threads', '并发线程'],
    ];

    const summaryHtml = fields
        .filter(([key]) => params[key] !== undefined && params[key] !== null && params[key] !== '')
        .map(([key, label]) => `
            <div class="params-card">
                <div class="params-label">${label}</div>
                <div class="params-value">${params[key]}</div>
            </div>
        `)
        .join('');

    paramsSummary.innerHTML = summaryHtml || `
        <div class="params-card">
            <div class="params-label">提示</div>
            <div class="params-value">这条任务没有可结构化展示的参数</div>
        </div>
    `;
    paramsDialog.showModal();
}

// ============ 停止任务 ============
async function stopJob(jobId) {
    try {
        const resp = await fetch(`/api/jobs/${jobId}/stop`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ current_username: window.__auth?.username || '' })
        });
        const data = await resp.json();

        if (data.ok) {
            Toast.success('已停止', '任务停止请求已发送');
            fetchJobs();
        } else {
            const msg = data?.data?.message || data.error || '未知错误';
            Toast.error('停止失败', msg);
        }
    } catch (e) {
        Toast.error('请求失败', e.message);
    }
}

// ============ 配置管理 ============
const configDialog = document.getElementById('config-dialog');
const currentLoginUrlEl = document.getElementById('current-login-url');
const newLoginUrlEl = document.getElementById('new-login-url');

async function openConfig() {
    try {
        const resp = await fetch('/api/config');
        const data = await resp.json();

        if (data.ok && data.data) {
            const currentUrl = data.data.login_url || '';
            currentLoginUrlEl.textContent = currentUrl || '未设置';
            newLoginUrlEl.value = currentUrl;
        } else {
            currentLoginUrlEl.textContent = '加载失败';
        }

        configDialog.showModal();
    } catch (e) {
        Toast.error('加载失败', e.message);
    }
}

async function saveConfig() {
    const newUrl = newLoginUrlEl.value.trim();

    if (!newUrl) {
        Toast.error('验证失败', '请输入登录地址');
        return;
    }

    try {
        const resp = await fetch('/api/config/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ login_url: newUrl })
        });

        const data = await resp.json();

        if (data.ok) {
            const reloaded = data.data?.reloaded;
            if (reloaded) {
                Toast.success('保存成功', '配置已更新并自动重载，立即生效');
            } else {
                Toast.success('保存成功', '配置已更新，但需要重启服务器生效');
            }
            configDialog.close();
        } else {
            Toast.error('保存失败', data.error || '未知错误');
        }
    } catch (e) {
        Toast.error('请求失败', e.message);
    }
}

// ============ 事件绑定 ============
document.getElementById('themeBtn').addEventListener('click', () => Theme.toggle());
document.getElementById('refreshBtn').addEventListener('click', () => {
    fetchJobs();
    Toast.info('正在刷新...');
});
document.getElementById('params-close').addEventListener('click', () => paramsDialog.close());

// 配置相关
document.getElementById('configBtn').addEventListener('click', openConfig);
document.getElementById('config-cancel').addEventListener('click', () => configDialog.close());
document.getElementById('config-save').addEventListener('click', saveConfig);

// ============ 初始化 ============
async function init() {
    Theme.init();

    // 尝试从缓存加载认证信息
    const cached = Auth.load();
    if (cached && cached.username && cached.password) {
        // 通过后端 API 验证是否为授权用户
        const isAuthorized = await checkAuthorization(cached.username);
        if (isAuthorized) {
            window.__auth = cached;
        } else {
            Auth.clear();
        }
    }

    // 如果没有登录信息，弹出登录框
    if (!window.__auth) {
        const ok = await promptLogin();
        if (!ok) {
            document.body.innerHTML = `
                <div style="display: flex; align-items: center; justify-content: center; min-height: 100vh; flex-direction: column; gap: 20px; padding: 20px; text-align: center;">
                    <div style="font-size: 64px;">🔒</div>
                    <h1 style="font-size: 24px; color: var(--text-primary);">未登录</h1>
                    <p style="color: var(--text-secondary); max-width: 400px;">
                        您需要登录才能访问该页面
                    </p>
                    <a href="/" style="color: var(--primary); text-decoration: none;">← 返回首页</a>
                </div>
            `;
            return;
        }
    }

    // 验证通过，加载任务列表
    fetchJobs();
    setInterval(fetchJobs, 3000);
}

init();
