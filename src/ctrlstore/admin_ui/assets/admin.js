/* CtrlStore admin SPA.
 *
 * The UI only talks to /api/admin/v1. Session auth is an HttpOnly cookie;
 * all writes carry the CSRF token returned by /me or /session. No secrets are
 * kept in localStorage, and every value from the API is escaped before it is
 * inserted into a template.
 */
(() => {
  'use strict';

  const API = '/api/admin/v1';
  const KINDS = {
    image: { label: '图片', code: 'IMG' },
    video: { label: '视频', code: 'VID' },
    markdown: { label: 'Markdown', code: 'MD' },
    pdf: { label: 'PDF', code: 'PDF' },
    docx: { label: 'Word', code: 'DOC' },
    xlsx: { label: 'Excel', code: 'XLS' },
    text: { label: '纯文本', code: 'TXT' },
  };
  const PERMISSIONS = [
    ['asset.read', '查看内容'],
    ['asset.upload', '上传内容'],
    ['asset.update', '修改内容'],
    ['asset.publish', '发布与撤回'],
    ['asset.trash', '回收站'],
    ['folder.manage', '管理目录'],
    ['collection.manage', '管理集合'],
    ['tag.manage', '管理标签'],
  ];
  const STATUS = {
    draft: ['草稿', 'draft'],
    published: ['已发布', 'published'],
    archived: ['已归档', 'archived'],
    trashed: ['回收站', 'private'],
  };
  const VISIBILITY = {
    private: '私有',
    site: '仅站点',
    unlisted: '未列出',
    public: '公开',
  };
  const JOB_STATUS = {
    pending: ['等待中', 'draft'],
    running: ['进行中', 'pending'],
    done: ['已完成', 'published'],
    failed: ['失败', 'failed'],
  };
  const AUDIT_LABELS = {
    login: '登录',
    login_failed: '登录失败',
    logout: '退出登录',
    user_created: '创建账号',
    user_disabled: '停用账号',
    password_reset: '重置密码',
    permissions_changed: '权限变更',
    site_created: '创建站点',
    token_created: '签发令牌',
    token_revoked: '撤销令牌',
    upload_created: '开始上传',
    upload_completed: '上传完成',
    upload_cancelled: '取消上传',
    upload_failed: '上传失败',
    folder_created: '创建目录',
    folder_renamed: '重命名目录',
    folder_moved: '移动目录',
    tag_created: '创建标签',
    tag_merged: '合并标签',
    asset_created: '创建内容',
    asset_updated: '修改内容',
    asset_version_added: '新增版本',
    asset_version_pruned: '淘汰版本',
    asset_published: '发布',
    asset_unpublished: '撤回',
    asset_archived: '归档',
    asset_trashed: '移入回收站',
    asset_restored: '恢复',
    asset_purged: '彻底删除',
    asset_publish_scheduled: '定时发布',
    asset_publish_failed: '发布失败',
    collection_created: '创建集合',
    collection_updated: '修改集合',
    collection_item_added: '添加集合条目',
    collection_item_removed: '移除集合条目',
    site_grant_added: '新增站点授权',
    site_grant_removed: '移除站点授权',
  };

  const state = {
    me: null,
    csrf: null,
    route: '',
    param: null,
    routeToken: 0,
    dashboard: null,
    dashboardMonth: '',
    dashboardDay: null,
    selectedDay: '',
    library: {
      kind: '',
      folder_id: '',
      status: '',
      tag: '',
      q: '',
      items: [],
      next_cursor: null,
      loaded: false,
      loading: false,
      folders: [],
      tags: [],
    },
    asset: null,
    assetTags: [],
    editor: null,
    uploadQueue: [],
    uploadBusy: false,
    collections: [],
    collection: null,
    collectionItems: [],
    collectionAssets: [],
    collectionDirty: false,
    sites: [],
    tokens: [],
    grants: {},
    users: [],
    selectedUser: null,
    trash: [],
    health: null,
    jobs: [],
    audit: [],
    systemTab: 'health',
    sitesTab: 'sites',
    modalConfirm: null,
    searchTimer: null,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const esc = (value) => String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
  const attr = esc;
  const isOwner = () => state.me?.user?.role === 'owner';
  const can = (permission) => isOwner() || Boolean(state.me?.user?.permissions?.includes(permission));
  const num = (value) => Number(value || 0).toLocaleString('en-US');
  const bytes = (value) => {
    if (value === null || value === undefined) return '—';
    let n = Number(value);
    if (!Number.isFinite(n)) return '—';
    if (n < 1024) return `${n} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return `${n >= 100 ? n.toFixed(0) : n.toFixed(n >= 10 ? 1 : 2)} ${units[i]}`;
  };
  const dateFmt = (value, withTime = true) => {
    if (!value) return '—';
    const text = /Z$|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`;
    const d = new Date(text);
    if (Number.isNaN(d.getTime())) return String(value);
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric', month: '2-digit', day: '2-digit',
      ...(withTime ? { hour: '2-digit', minute: '2-digit', hour12: false } : {}),
    }).format(d).replace(/\//g, '-');
  };
  const timeOnly = (value) => {
    if (!value) return '—';
    const text = /Z$|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`;
    const d = new Date(text);
    if (Number.isNaN(d.getTime())) return '—';
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(d);
  };
  const todayShanghai = () => new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date());
  const monthShanghai = () => todayShanghai().slice(0, 7);
  const MONTH_NAMES = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十', '十一', '十二'];
  const monthName = (ym) => {
    const m = Number(String(ym).split('-')[1]);
    return `${MONTH_NAMES[m - 1] || m}月`;
  };
  const monthLabel = (ym) => {
    const [y, m] = ym.split('-').map(Number);
    return `${y} 年 ${m} 月`;
  };
  const monthShort = (ym) => String(Number(ym.split('-')[1])).padStart(2, '0');
  const isoDay = (ym, day) => `${ym}-${String(day).padStart(2, '0')}`;
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : `id-${Date.now()}-${Math.random().toString(16).slice(2)}`);

  class ApiFailure extends Error {
    constructor(message, status, code, details = null) {
      super(message);
      this.name = 'ApiFailure';
      this.status = status;
      this.code = code;
      this.details = details;
    }
  }

  /* ------------------------------------------------------------
     HTTP + session
     ------------------------------------------------------------ */
  async function api(path, options = {}) {
    const { method = 'GET', body, raw = false, auth = true, headers = {}, signal } = options;
    const opts = {
      method,
      credentials: 'same-origin',
      signal,
      headers: { Accept: 'application/json', ...headers },
    };
    if (body !== undefined && raw) {
      opts.body = body;
      if (!(body instanceof Blob) && !(body instanceof ArrayBuffer) && !opts.headers['Content-Type']) {
        opts.headers['Content-Type'] = 'application/octet-stream';
      }
    } else if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    if (auth && !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase()) && state.csrf) {
      opts.headers['X-CSRF-Token'] = state.csrf;
    }

    let response;
    try {
      response = await fetch(`${API}${path}`, opts);
    } catch {
      throw new ApiFailure('无法连接 CtrlStore API，请检查服务是否在运行。', 0, 'network_error');
    }

    let payload = null;
    const text = response.status === 204 ? '' : await response.text();
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = { message: text }; }
    }
    if (!response.ok) {
      const message = payload?.error?.message || payload?.message || `请求失败（HTTP ${response.status}）`;
      const failure = new ApiFailure(message, response.status, payload?.error?.code, payload?.error?.details);
      if (response.status === 401 && path !== '/session') showLogin('会话已过期，请重新登录。');
      throw failure;
    }
    return payload;
  }

  /* ------------------------------------------------------------
     Boot / login / shell
     ------------------------------------------------------------ */
  async function boot() {
    try {
      const me = await api('/me');
      establishSession(me);
    } catch (error) {
      if (error.status === 401) showLogin();
      else showLogin(error.message || '无法打开管理端。');
    } finally {
      const boot = $('#bootScreen');
      boot?.classList.add('hidden');
      setTimeout(() => boot?.remove(), 300);
    }
  }

  function establishSession(me, fromLogin = false) {
    state.me = me;
    state.csrf = me.csrf_token;
    applyIdentity();
    $('#loginScreen').classList.remove('active');
    $('#app').hidden = false;
    $('#loginError').classList.remove('show');
    if (!location.hash || location.hash === '#/' || location.hash === '#') {
      navigate(defaultRoute(), { replace: true });
    } else {
      renderRoute();
    }
    refreshShellHealth();
    if (fromLogin) toast('已进入内容库');
  }

  function applyIdentity() {
    const user = state.me?.user;
    if (!user) return;
    const display = user.username || '—';
    const role = user.role === 'owner' ? 'OWNER' : 'EDITOR';
    $('#sidebarAvatar').textContent = display.slice(0, 1).toUpperCase();
    $('#sidebarUserName').textContent = `${display} · ${user.role}`;
    $('#sidebarUserRole').textContent = `${role} ACCOUNT`;
    $('#topAvatar').textContent = display.slice(0, 1).toUpperCase();
    $('#topAvatar').title = display;

    $$('.nav-item[data-owner]').forEach(el => { el.hidden = !isOwner(); });
    $$('.nav-item[data-perm]').forEach(el => { el.hidden = !can(el.dataset.perm); });
    $$('.nav-group').forEach(group => {
      const items = $$('.nav-item', group).filter(item => !item.hidden);
      group.hidden = items.length === 0;
    });
    $('.health-pill').hidden = !isOwner();
    $('.disk-mini').hidden = !isOwner();
    $('#sysReadout').innerHTML = `
      <div class="row"><span class="sys-id"><i></i>NODE-01</span><b>ONLINE</b></div>
      <div class="row"><span>AUTH</span><b>${esc(role)}</b></div>
      <div class="row"><span>API</span><b>READY</b></div>`;
  }

  function defaultRoute() {
    if (isOwner()) return 'dashboard';
    if (can('asset.read')) return 'library';
    if (can('asset.upload')) return 'upload';
    return 'no-access';
  }

  async function login(event) {
    event.preventDefault();
    const username = $('#loginUser').value.trim();
    const password = $('#loginPass').value;
    const button = $('#loginSubmit');
    const error = $('#loginError');
    error.classList.remove('show');
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span>验证中';
    try {
      const me = await api('/session', { method: 'POST', auth: false, body: { username, password } });
      state.csrf = me.csrf_token;
      establishSession(me, true);
      $('#loginPass').value = '';
    } catch (failure) {
      error.textContent = failure.message || '登录失败';
      error.classList.add('show');
      $('#loginPass').focus();
    } finally {
      button.disabled = false;
      button.innerHTML = '进入内容库 <svg class="icon sm"><use href="#i-arrow-right"/></svg>';
    }
  }

  function showLogin(message = '') {
    state.me = null;
    state.csrf = null;
    closeModal();
    closeSearch();
    $('#app').hidden = true;
    $('#loginScreen').classList.add('active');
    $('#loginError').textContent = message;
    $('#loginError').classList.toggle('show', Boolean(message));
    setTimeout(() => $('#loginUser')?.focus(), 80);
  }

  async function logout() {
    try {
      await api('/session', { method: 'DELETE' });
    } catch (error) {
      if (error.status !== 401) toast(error.message, 'error');
    }
    showLogin();
  }

  function navigate(route, { replace = false } = {}) {
    const target = `#/${route}`;
    if (replace) {
      history.replaceState(null, '', target);
      renderRoute();
    } else if (location.hash === target) {
      renderRoute();
    } else {
      location.hash = target;
    }
  }

  function parseRoute() {
    const raw = location.hash.replace(/^#\/?/, '').replace(/\/+$/, '');
    const [name, ...rest] = (raw || 'dashboard').split('/');
    return { name: name || 'dashboard', param: rest.length ? rest.map(decodeURIComponent).join('/') : null };
  }

  function allowedRoute(name) {
    if (name === 'no-access') return true;
    if (['dashboard', 'sites', 'users', 'system'].includes(name)) return isOwner();
    if (name === 'upload') return can('asset.upload');
    if (name === 'trash') return can('asset.trash');
    if (['library', 'asset', 'editor', 'collections'].includes(name)) return can('asset.read');
    return true;
  }

  async function renderRoute() {
    const { name, param } = parseRoute();
    if (!state.me) return;
    let route = name;
    if (!allowedRoute(route)) route = defaultRoute();
    if (route !== name) history.replaceState(null, '', `#/${route}`);
    if (route === 'no-access') {
      updateChrome('no-access');
      renderNoAccess();
      return;
    }
    state.route = route;
    state.param = param;
    updateChrome(route);
    closeSidebar();
    window.scrollTo({ top: 0, behavior: 'instant' });
    const token = ++state.routeToken;
    $('#view').innerHTML = loadingPage();
    try {
      if (route === 'dashboard') await renderDashboard();
      else if (route === 'library') await renderLibrary();
      else if (route === 'asset') await renderAsset(param);
      else if (route === 'editor') await renderEditor(param);
      else if (route === 'upload') renderUploadPage();
      else if (route === 'collections') await renderCollections();
      else if (route === 'sites') await renderSites();
      else if (route === 'users') await renderUsers();
      else if (route === 'trash') await renderTrash();
      else if (route === 'system') await renderSystem();
      else await renderDashboard();
    } catch (error) {
      if (token !== state.routeToken) return;
      if (error.status === 401) return;
      $('#view').innerHTML = errorPage(error.message || '页面加载失败', route);
    }
    if (token === state.routeToken) bindImages($('#view'));
  }

  function updateChrome(route) {
    const labels = {
      dashboard: '总览', library: '内容库', asset: '内容库 / 内容详情', editor: '内容库 / Markdown 编辑',
      upload: '上传', collections: '集合', sites: '站点与令牌', users: '账号与权限',
      trash: '回收站', system: '系统状态', 'no-access': '访问受限',
    };
    $('#crumb').textContent = labels[route] || '总览';
    document.title = `${labels[route] || '总览'} · CtrlStore`;
    $$('.nav-item').forEach(item => {
      const key = item.dataset.route;
      item.classList.toggle('active', key === route || (route === 'asset' && key === 'library') || (route === 'editor' && key === 'library'));
    });
  }

  function renderNoAccess() {
    $('#view').innerHTML = `
      <div class="page-head"><div><div class="eyebrow"><span class="dot"></span>Access<span class="slash">/</span><span class="en">权限</span></div><h1>还没有可用的管理权限</h1><p class="desc">这个账号可以登录，但所有者尚未授予任何内容能力。</p></div></div>
      <div class="empty-state"><div class="empty-icon"><svg class="icon"><use href="#i-lock"/></svg></div><h2>请联系所有者授予权限</h2><p>所有者可以在「账号与权限」里为这个账号勾选查看、上传、修改、发布或回收站能力。权限变更后刷新页面即可生效。</p></div>`;
  }

  function loadingPage() {
    return '<div class="loading-page"><span class="spinner"></span><span>Loading / 正在读取</span></div>';
  }

  function errorPage(message, route = '') {
    return `<div class="page-head"><div><div class="eyebrow"><span class="dot"></span>Error<span class="slash">/</span><span class="en">读取失败</span></div><h1>这一页没有打开</h1></div></div>
      <div class="empty-state"><div class="empty-icon"><svg class="icon"><use href="#i-alert"/></svg></div><h2>${esc(message)}</h2><p>检查服务状态或稍后重试。若会话已过期，需要重新登录。</p>${route ? `<div class="empty-actions"><button class="btn secondary sm" data-route="${attr(route)}">重新加载</button></div>` : ''}</div>`;
  }

  function toast(message, kind = 'ok') {
    const el = $('#toast');
    if (!el) return;
    el.classList.toggle('error', kind === 'error');
    $('#toastText').textContent = message;
    el.classList.add('show');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => el.classList.remove('show'), 3000);
  }

  function bindImages(root) {
    $$('img[data-preview-image]', root).forEach(img => {
      const done = () => {
        img.classList.add('loaded');
        img.closest('.type-art')?.classList.add('has-image');
      };
      const fail = () => img.remove();
      if (img.complete && img.naturalWidth > 0) done();
      img.addEventListener('load', done, { once: true });
      img.addEventListener('error', fail, { once: true });
    });
  }

  async function refreshShellHealth() {
    if (!isOwner()) return;
    try {
      const health = await api('/health');
      state.health = health;
      const pill = $('#healthPill');
      pill.classList.toggle('degraded', health.status !== 'ok');
      pill.innerHTML = `<span class="pulse"></span>${health.status === 'ok' ? 'NODE-01 · API OK' : 'NODE-01 · DEGRADED'}`;
      const disk = health.disk || {};
      const free = disk.free_bytes;
      const total = disk.total_bytes;
      if (total) {
        const percent = Math.max(0, Math.min(100, Math.round((free / total) * 100)));
        $('#diskPercent').textContent = `${percent}%`;
        $('#diskMeter').style.width = `${percent}%`;
        $('#diskText').textContent = `${bytes(total - free)} / ${bytes(total)} · ${health.status === 'ok' ? '正常' : '降级'}`;
      } else {
        $('#diskPercent').textContent = '—';
        $('#diskText').textContent = '磁盘不可读';
      }
      const jobs = health.jobs || {};
      const queue = Number(jobs.pending || 0) + Number(jobs.running || 0);
      $('#sysReadout').innerHTML = `
        <div class="row"><span class="sys-id"><i></i>NODE-01</span><b>${health.status === 'ok' ? 'ONLINE' : 'DEGRADED'}</b></div>
        <div class="row"><span>APP ${esc(health.app_version || '—')}</span><b>SCHEMA ${esc(health.schema_version || '—')}</b></div>
        <div class="row"><span>QUEUE</span><b>${queue}</b></div>`;
    } catch (error) {
      if (error.status !== 403 && error.status !== 401) $('#healthPill').innerHTML = '<span class="pulse"></span>HEALTH UNKNOWN';
    }
  }

  /* ------------------------------------------------------------
     Modal / confirm
     ------------------------------------------------------------ */
  function openModal({ kicker = 'CtrlStore', title, body, footer = '', wide = false }) {
    $('#modalKicker').textContent = kicker;
    $('#modalTitle').textContent = title;
    $('#modalBody').innerHTML = body;
    $('#modalFoot').innerHTML = footer;
    $('#modalCard').classList.toggle('wide', wide);
    $('#modalBody').scrollTop = 0;
    $('#modalLayer').classList.add('open');
    setTimeout(() => {
      const first = $('#modalBody input:not([type=hidden]), #modalBody select, #modalBody textarea, #modalFoot button');
      first?.focus();
    }, 30);
  }

  function closeModal() {
    $('#modalLayer').classList.remove('open');
    state.modalConfirm = null;
  }

  function modalError(message) {
    const box = $('#modalBody .form-error');
    if (box) { box.textContent = message; box.classList.add('show'); }
    else toast(message, 'error');
  }

  function confirmDialog({ kicker = 'Confirm', title, body, confirmLabel = '确认', onConfirm }) {
    state.modalConfirm = onConfirm;
    openModal({
      kicker, title,
      body: `<div class="confirm-icon"><svg class="icon"><use href="#i-alert"/></svg></div><div class="confirm-copy">${body}</div>`,
      footer: `<button class="btn secondary sm" data-close-modal>取消</button><button class="btn primary sm" id="modalConfirm">${esc(confirmLabel)}</button>`,
    });
    $('#modalConfirm').addEventListener('click', async () => {
      const action = state.modalConfirm;
      if (!action) return;
      const button = $('#modalConfirm');
      $('#modalBody .reference-errors')?.remove();
      button.disabled = true;
      const old = button.textContent;
      button.innerHTML = '<span class="spinner"></span>';
      try {
        await action();
        closeModal();
      } catch (error) {
        if (error.status !== 401) {
          button.disabled = false;
          button.textContent = old;
          showConfirmError(error);
        }
      }
    });
  }

  function showConfirmError(error) {
    const references = error.details?.references;
    if (!references) { modalError(error.message); return; }
    const covers = (references.covers || []).map(item => `<li>${esc(item.title || item.id)} · ${esc(item.id)}（${[item.current ? '编辑中封面' : '', item.published ? '已发布封面' : ''].filter(Boolean).join('、')}）</li>`).join('');
    const collections = (references.collections || []).map(item => `<li>${esc(item.title || item.slug)} · ${esc(item.slug)}</li>`).join('');
    $('#modalBody').insertAdjacentHTML('beforeend', `<div class="reference-errors" role="alert"><p>先解除以下引用，再彻底删除：</p>${covers ? `<h4>封面引用（含已发布封面）</h4><ul>${covers}</ul>` : ''}${collections ? `<h4>集合引用</h4><ul>${collections}</ul>` : ''}</div>`);
  }

  function formDataObject(form) {
    return Object.fromEntries(new FormData(form).entries());
  }

  function splitTags(value) {
    return String(value || '').split(/[,，]/).map(v => v.trim()).filter(Boolean);
  }

  function tagPills(tags) {
    return (tags || []).map(tag => `<span class="tag">${esc(tag)}</span>`).join('');
  }

  function kindArt(kind, extra = '') {
    const meta = KINDS[kind] || { label: kind || '—', code: '—' };
    return `<div class="asset-art type-art" data-kind="${attr(kind)}">
      <span class="type-mark">${esc(meta.code)}<span class="type-sub">${esc(meta.label)}</span></span>
      <span class="asset-type-label">${esc(meta.code)} / ${esc(kind || '—')}</span>
      ${extra}
    </div>`;
  }

  function hasReadyPreview(asset) {
    const version = asset?.current_version;
    return Boolean((version?.derivatives || []).some(item => item.status === 'ready' && ['thumb', 'display', 'poster'].includes(item.purpose)));
  }

  function thumbImg(asset) {
    if (!hasReadyPreview(asset)) return '';
    const src = `${API}/assets/${encodeURIComponent(asset.id)}/thumb?rev=${encodeURIComponent(asset.revision)}`;
    return `<img class="asset-preview-img" data-preview-image src="${attr(src)}" alt="" loading="lazy">`;
  }

  function starPill(status, visibility) {
    if (status === 'published') return '<span class="pill published"><i></i>已发布</span>';
    if (status === 'draft' && visibility === 'site') return '<span class="pill site"><i></i>仅站点</span>';
    if (status === 'draft' && visibility === 'private') return '<span class="pill draft"><i></i>草稿</span>';
    const [label, cls] = STATUS[status] || [status, 'draft'];
    return `<span class="pill ${cls}"><i></i>${esc(label)}</span>`;
  }

  /* ------------------------------------------------------------
     Dashboard
     ------------------------------------------------------------ */
  async function renderDashboard() {
    if (!state.dashboardMonth) state.dashboardMonth = monthShanghai();
    const data = await api(`/dashboard?month=${encodeURIComponent(state.dashboardMonth)}`);
    state.dashboard = data;
    if (data.today?.day?.startsWith(state.dashboardMonth)) {
      state.selectedDay = data.today.day;
    } else {
      state.selectedDay = (data.month?.days || []).find(d => d.uv)?.day || `${state.dashboardMonth}-01`;
    }
    state.dashboardDay = null;
    if (data.reported) {
      try { state.dashboardDay = await api(`/dashboard/day?date=${encodeURIComponent(state.selectedDay)}`); }
      catch { state.dashboardDay = null; }
    }
    $('#view').innerHTML = dashboardHTML(data, state.dashboardDay);
    renderMonthChrome();
    if (data.inventory?.trash_count !== undefined) {
      const badge = $('#trashBadge');
      badge.textContent = data.inventory.trash_count;
      badge.hidden = !data.inventory.trash_count;
    }
  }

  function dashboardHTML(data, day) {
    const inv = data.inventory || { kinds: [], total_count: 0, total_bytes: 0, disk_free_bytes: null, trash_count: 0 };
    const totalPublished = inv.kinds.reduce((n, item) => n + (item.published || 0), 0);
    const today = data.today;
    const reported = data.reported;
    const uv = today?.uv;
    const pv = today?.pv;
    const delta = today?.uv_delta;
    const deltaHTML = !reported || today === null
      ? '<span class="mono" style="font-size:8px;color:#A093A8">等待首次上报</span>'
      : delta === 0
        ? '<span class="mono" style="font-size:8px;color:#9A8CA2">与昨日持平</span>'
        : `<span class="${delta > 0 ? 'up' : 'down'}">${delta > 0 ? '↑' : '↓'} ${num(Math.abs(delta))}</span><span>较昨日</span>`;
    return `
      <div class="page-intro">
        <div>
          <div class="eyebrow"><span class="dot"></span>Overview<span class="slash">/</span><span class="en">总览</span><span class="slash">/</span><span class="en">${esc(todayShanghai())} · Asia/Shanghai</span></div>
          <h1>让内容，<em>各得其所</em>。</h1>
          <p class="lede">${reported ? `今天已有 <b>${num(uv)}</b> 位读者，库中 <b>${num(totalPublished)}</b> 条内容在线。` : '网站还没有上报访问数据；库存与内容管理仍然可用。'}</p>
        </div>
        <div class="actions">
          ${can('asset.upload') ? '<button class="btn secondary" data-action="new-text"><svg class="icon sm"><use href="#i-text"/></svg>写一篇文章</button>' : ''}
          ${can('asset.upload') ? '<button class="btn primary" data-route="upload"><svg class="icon sm"><use href="#i-plus"/></svg>上传内容</button>' : ''}
        </div>
      </div>
      <section class="today-band">
        <div class="today-primary"><div class="stat-label">今日独立访客 <span class="en">Unique visitors</span></div><div class="stat-big">${reported ? num(uv) : '—'}</div><div class="delta">${deltaHTML}</div></div>
        <div class="today-side">
          <div class="today-side-row"><div><span>页面浏览 / PV</span><small>PAGE VIEWS</small></div><b>${reported ? num(pv) : '—'}</b></div>
          <div class="today-side-row"><div><span>线上内容 / PUBLISHED</span><small>VISIBLE ITEMS</small></div><b>${num(totalPublished)}</b></div>
          <div class="today-side-row"><div><span>数据状态 / REPORT</span><small>ASIA / SHANGHAI</small></div><b style="font-family:var(--mono);font-size:10px">${reported ? '已上报' : '尚未上报'}</b></div>
        </div>
        <div class="spark-wrap"><div class="spark-head"><span>本月每日访客</span><span class="live"><i></i>${reported ? 'REPORTED' : 'WAITING'}</span></div>${sparkline((data.month?.days || []).filter(d => d.day <= (data.today?.day || todayShanghai())), reported)}<div class="spark-labels"><span>01</span><span>08</span><span>15</span><span>22</span><span>${esc(monthLabel(state.dashboardMonth))}</span></div></div>
      </section>
      <div class="dash-grid">
        <section class="panel span-7">
          <div class="panel-head"><div class="panel-title">访问日历 <small>${esc(monthLabel(state.dashboardMonth))} / 每日独立访客</small></div><div class="calendar-nav"><div class="calendar-month" id="calendarMonth">${esc(monthName(state.dashboardMonth))}<span>${esc(state.dashboardMonth.slice(0, 4))}</span></div><button class="icon-btn" data-action="dash-prev" aria-label="上个月"><svg class="icon sm"><use href="#i-chevron-left"/></svg></button><button class="icon-btn" data-action="dash-next" aria-label="下个月"><svg class="icon sm"><use href="#i-chevron-right"/></svg></button></div></div>
          ${calendarHTML(data, state.selectedDay)}
          <div class="calendar-legend"><span><i></i>有上报</span><span><span class="swatch"></span>访问较集中</span><span><span class="today-ring"></span>今天</span></div>
          <div class="calendar-foot"><span>${reported ? '每天按 Asia/Shanghai 汇总' : '尚未收到网站上报'}</span><button class="panel-link" data-action="dash-today">回到今天 <svg class="icon sm"><use href="#i-arrow-right"/></svg></button></div>
        </section>
        <section class="panel span-5 day-panel" id="dayPanel">${dayPanelHTML(data, day)}</section>
      </div>
      <section class="panel inventory-panel">
        <div class="inventory-top"><div class="panel-title">库藏 <small>Inventory · 七类内容</small></div><div class="inventory-note"><span>含历史版本 ${bytes(inv.total_bytes)}</span><span class="bar-mini"><span></span></span><span>磁盘剩余 ${bytes(inv.disk_free_bytes)}</span></div></div>
        <div class="inventory-strip">${inventoryHTML(inv.kinds)}</div>
        <div class="inventory-foot"><span>全库 <b>${num(inv.total_count)}</b> 条 · 回收站 <b>${num(inv.trash_count)}</b> 条</span><span>磁盘剩余 <b>${bytes(inv.disk_free_bytes)}</b> · ${state.health?.status === 'degraded' ? '降级' : '健康'}</span></div>
      </section>`;
  }

  function renderMonthChrome() {
    const el = $('#calendarMonth');
    if (!el) return;
    el.innerHTML = `${esc(monthName(state.dashboardMonth))}<span>${esc(state.dashboardMonth.slice(0, 4))}</span>`;
  }

  function sparkline(days, reported) {
    const values = (days || []).map(d => Number(d.uv || 0));
    if (!reported || !values.length) {
      return '<div style="display:flex;align-items:center;justify-content:center;height:73px;color:#9B87BC;font-family:var(--mono);font-size:8px;letter-spacing:.08em">WAITING FOR FIRST PAGEVIEW REPORT</div>';
    }
    const max = Math.max(1, ...values);
    const w = 360, h = 76;
    const points = values.map((v, i) => {
      const x = values.length === 1 ? 0 : (i / (values.length - 1)) * w;
      const y = 68 - (v / max) * 56;
      return [x, y];
    });
    const line = points.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
    const area = `${line} L${w} ${h} L0 ${h} Z`;
    return `<svg class="sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-label="本月每日访客"><path d="${line}" fill="none" stroke="#B49AFF" stroke-width="1.6" stroke-linecap="round"/><path d="${area}" fill="url(#sparkFill)"/><circle cx="${points.at(-1)[0]}" cy="${points.at(-1)[1]}" r="3" fill="#2A1050" stroke="#B49AFF" stroke-width="1.5"/></svg>`;
  }

  function calendarHTML(data, selected) {
    const ym = state.dashboardMonth;
    const [year, month] = ym.split('-').map(Number);
    const total = new Date(year, month, 0).getDate();
    const firstWeekday = (new Date(year, month - 1, 1).getDay() + 6) % 7;
    const values = Object.fromEntries((data.month?.days || []).map(d => [d.day, d]));
    const today = data.today?.day || todayShanghai();
    const weekday = ['一', '二', '三', '四', '五', '六', '日'];
    let html = weekday.map(d => `<div class="weekday">${d}</div>`).join('');
    const cells = [];
    for (let i = 0; i < firstWeekday; i++) cells.push(null);
    for (let day = 1; day <= total; day++) cells.push(day);
    while (cells.length % 7) cells.push(null);
    cells.forEach(day => {
      if (!day) { html += '<span class="day out"></span>'; return; }
      const iso = isoDay(ym, day);
      const row = values[iso];
      const uv = row?.uv || 0;
      let cls = 'day';
      if (data.reported) {
        if (uv > 0) cls += uv > 1000 ? ' high' : uv > 700 ? ' mid' : ' low';
        if (iso === today) cls += ' today';
        if (iso === selected) cls += ' selected';
      } else if (iso === today) cls += ' today';
      html += `<button class="${cls}" data-action="dash-day" data-day="${iso}" title="${data.reported ? `${iso} · ${num(uv)} 位访客` : `${iso} · 尚未上报`}"><span class="d">${day}</span>${data.reported && uv ? '<i class="dot"></i>' : ''}</button>`;
    });
    return `<div class="calendar">${html}</div>`;
  }

  function dayPanelHTML(data, day) {
    const date = day?.day || state.selectedDay;
    const paths = day?.paths || [];
    const max = Math.max(1, ...paths.map(p => Number(p.pv || 0)));
    return `<div class="panel-head"><div class="panel-title">当天剖面 <small>Day detail · 按路径</small></div><div class="day-date">${esc(date || '—')}</div></div>
      <div class="day-stats"><div class="day-stat"><span>独立访客</span><b>${data.reported ? num(day?.uv || 0) : '—'} <small>${data.reported ? '人' : '未上报'}</small></b></div><div class="day-stat"><span>页面浏览</span><b>${data.reported ? num(day?.pv || 0) : '—'} <small>${data.reported ? '次' : '未上报'}</small></b></div></div>
      <div class="path-head"><span>Top paths / 被打开最多的页面</span><span>浏览</span></div>
      ${!data.reported ? '<div class="inline-note">网站还没有调用页面上报接口。接入后会在这里看到当天的人数、次数与 Top 路径。</div>' : paths.length === 0 ? '<div class="inline-note">这一天没有路径记录。可能还没有人打开页面，或网站尚未上报。</div>' : `<div id="pathRows">${paths.map(path => `<div class="path-row"><div><div class="path-meta"><span class="path-name">${esc(path.path)}</span><span class="path-count">${num(path.pv)}</span></div><div class="path-bar"><span style="width:${Math.max(4, (path.pv / max) * 100)}%"></span></div></div><span></span></div>`).join('')}</div>`}
      <div class="months-mini"><div class="months-mini-head"><span>近 12 个月 · 每日人数相加</span><span>SUM OF DAILY UV / BY MONTH</span></div>${monthsHTML(data.months || [])}</div>`;
  }

  function monthsHTML(months) {
    if (!months.length) return '<div class="inline-note" style="border:0;background:transparent;padding:0">尚无月度访问数据。</div>';
    const max = Math.max(1, ...months.map(m => Number(m.uv || 0)));
    return `<div class="months-chart">${months.map(m => `<div class="month-col"><i style="height:${Math.max(2, (m.uv / max) * 100)}%" title="${esc(m.month)} · ${num(m.uv)}"></i><span>${esc(monthShort(m.month))}</span></div>`).join('')}</div>`;
  }

  function inventoryHTML(kinds) {
    return (kinds || []).map(item => {
      const meta = KINDS[item.kind] || { label: item.kind, code: '—' };
      const percent = item.count ? Math.round((item.published / item.count) * 100) : 0;
      return `<div class="inv-item"><div class="inv-head"><svg class="icon"><use href="#i-${kindIcon(item.kind)}"/></svg>${esc(meta.label)}</div><div class="inv-count">${num(item.count)}<small>条</small></div><div class="inv-pub">已发布 <b>${num(item.published)}</b></div><div class="inv-bar"><span style="width:${percent}%"></span></div><div class="inv-size">${bytes(item.total_bytes)} · 含历史</div></div>`;
    }).join('');
  }

  async function dashboardDay(iso, offset = 0) {
    if (offset) {
      const [y, m] = state.dashboardMonth.split('-').map(Number);
      const d = new Date(y, m - 1 + offset, 1);
      state.dashboardMonth = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
      await renderDashboard();
      return;
    }
    if (!state.dashboard?.reported) {
      state.selectedDay = iso;
      $('#view').innerHTML = dashboardHTML(state.dashboard, null);
      return;
    }
    const day = await api(`/dashboard/day?date=${encodeURIComponent(iso)}`);
    state.selectedDay = iso;
    state.dashboardDay = day;
    $('#view').innerHTML = dashboardHTML(state.dashboard, day);
  }

  /* ------------------------------------------------------------
     Library
     ------------------------------------------------------------ */
  async function renderLibrary() {
    const lib = state.library;
    const [folders, tags] = await Promise.all([api('/folders'), api('/tags')]);
    lib.folders = folders;
    lib.tags = tags;
    // 每次进入页面都重新拉一轮：别让内存里的旧列表掩盖别的标签页刚创建的内容。
    await loadLibrary(true);
    $('#view').innerHTML = libraryHTML();
    bindImages($('#view'));
  }

  async function loadLibrary(reset = false) {
    const lib = state.library;
    if (lib.loading) return;
    lib.loading = true;
    if (reset) { lib.items = []; lib.next_cursor = null; }
    const params = new URLSearchParams({ limit: '24' });
    if (lib.kind) params.set('kind', lib.kind);
    if (lib.folder_id) params.set('folder_id', lib.folder_id);
    if (lib.status) params.set('status', lib.status);
    if (lib.tag) params.set('tag', lib.tag);
    if (lib.q) params.set('q', lib.q);
    if (lib.next_cursor && !reset) params.set('cursor', lib.next_cursor);
    try {
      const data = await api(`/assets?${params}`);
      lib.items = reset ? data.items : [...lib.items, ...data.items];
      lib.next_cursor = data.next_cursor;
      lib.loaded = true;
    } finally {
      lib.loading = false;
    }
  }

  function libraryHTML() {
    const lib = state.library;
    const filtered = Boolean(lib.kind || lib.folder_id || lib.status || lib.tag || lib.q);
    return `
      <div class="page-head">
        <div><div class="eyebrow"><span class="dot"></span>Library<span class="slash">/</span><span class="en">内容库</span></div><h1>所有内容 <span class="count">${num(lib.items.length)} LOADED</span></h1><p class="desc">目录、标签、状态和版本都收在同一处；打开一条内容可以查看线上快照与待发布版本。</p></div>
        <div class="actions"><button class="btn secondary" data-action="open-search"><svg class="icon sm"><use href="#i-search"/></svg>全库检索</button>${can('folder.manage') ? '<button class="btn secondary" data-action="new-folder"><svg class="icon sm"><use href="#i-folder"/></svg>新建目录</button>' : ''}${can('asset.upload') ? '<button class="btn primary" data-action="new-text"><svg class="icon sm"><use href="#i-plus"/></svg>新建文章</button>' : ''}</div>
      </div>
      <div class="library-layout">
        <aside class="filter-panel">
          <div class="filter-block"><div class="filter-head"><span class="section-label">目录 / FOLDERS</span>${can('folder.manage') ? '<button class="add-mini" data-action="new-folder" aria-label="新建目录"><svg class="icon sm"><use href="#i-plus"/></svg></button>' : ''}</div><div class="tree"><button class="tree-item ${!lib.folder_id ? 'active' : ''}" data-action="lib-folder" data-id=""><svg class="icon"><use href="#i-library"/></svg>全部内容</button>${folderTree(lib.folders, lib.folder_id)}</div></div>
          <div class="filter-block"><div class="filter-head"><span class="section-label">标签 / TAGS</span></div><div class="tag-filter">${lib.tags.map(tag => `<button class="${lib.tag === tag.slug ? 'on' : ''}" data-action="lib-tag" data-slug="${attr(tag.slug)}">${esc(tag.name)}</button>`).join('') || '<span class="muted" style="font-size:9px">还没有标签</span>'}</div></div>
          <div class="filter-rule">草稿与待发布版本只出现在这里。<br>公开区只认已发布快照和 <span class="mono">published_version_id</span>。</div>
        </aside>
        <div class="library-main">
          <form class="library-toolbar" data-form="library-search"><label class="library-search"><svg class="icon sm"><use href="#i-search"/></svg><input id="librarySearchInput" name="q" value="${attr(lib.q)}" placeholder="按标题、文件名或别名筛选…" aria-label="筛选内容"></label><select class="select-mini" data-onchange="lib-status" aria-label="状态"><option value="">全部状态</option>${Object.entries(STATUS).filter(([key]) => key !== 'trashed').map(([key, [label]]) => `<option value="${key}" ${lib.status === key ? 'selected' : ''}>${label}</option>`).join('')}</select><button class="btn secondary sm" type="submit">筛选</button></form>
          <div class="filter-chips"><button class="chip ${!lib.kind ? 'on' : ''}" data-action="lib-kind" data-kind="">全部</button>${Object.entries(KINDS).map(([key, meta]) => `<button class="chip ${lib.kind === key ? 'on' : ''}" data-action="lib-kind" data-kind="${key}">${esc(meta.label)}</button>`).join('')}</div>
          <div class="library-meta"><span>已加载 <b>${num(lib.items.length)}</b> 条${filtered ? ' · 当前有筛选' : ''}</span><span>${lib.next_cursor ? '还有更多' : '已到末尾'}</span></div>
          ${lib.items.length ? `<div class="asset-grid" id="assetGrid">${lib.items.map((asset, index) => assetCard(asset, index === 0)).join('')}</div>` : `<div class="empty-state"><div class="empty-icon"><svg class="icon"><use href="#i-library"/></svg></div><h2>${filtered ? '没有匹配的内容' : '内容库还是空的'}</h2><p>${filtered ? '换一个筛选条件，或清空标签与目录。' : '上传一张图片、一份文档，或先写一篇文章。内容不会在未发布时出现在公开区。'}</p><div class="empty-actions">${filtered ? '<button class="btn secondary sm" data-action="lib-clear">清除筛选</button>' : (can('asset.upload') ? '<button class="btn primary sm" data-route="upload">上传第一份内容</button>' : '')}</div></div>`}
          ${lib.next_cursor ? '<div class="pager"><button class="btn secondary sm" data-action="lib-more">加载更多</button></div>' : ''}
        </div>
      </div>`;
  }

  function folderTree(folders, selected) {
    return (folders || []).map(folder => {
      const depth = Math.max(0, String(folder.path || '').split('/').filter(Boolean).length - 1);
      return `<button class="tree-item ${selected === folder.id ? 'active' : ''}" style="padding-left:${8 + depth * 16}px" data-action="lib-folder" data-id="${attr(folder.id)}"><svg class="icon"><use href="#i-folder"/></svg>${esc(folder.name)}</button>`;
    }).join('');
  }

  function assetCard(asset, featured = false) {
    const version = asset.current_version;
    const kind = KINDS[asset.kind] || { label: asset.kind, code: '—' };
    const dims = version?.width && version?.height ? `${version.width} × ${version.height}` : '';
    return `<article class="asset-card ${featured ? 'featured' : ''}" data-route="asset/${attr(asset.id)}" tabindex="0" data-kind="${attr(asset.kind)}">
      <div class="asset-art-wrap">${kindArt(asset.kind, thumbImg(asset))}<span class="asset-status ${asset.status === 'draft' ? 'draft' : asset.visibility === 'site' ? 'site' : ''}"><i></i>${esc(asset.status === 'published' ? '已发布' : asset.status === 'archived' ? '已归档' : asset.visibility === 'site' ? '仅站点' : '草稿')}</span><button class="asset-card-menu" data-route="asset/${attr(asset.id)}" aria-label="打开详情"><svg class="icon sm"><use href="#i-more"/></svg></button></div>
      <div class="asset-info"><div class="asset-title"><span>${esc(asset.title || '未命名内容')}</span><span class="kind-glyph">${esc(kind.code)}</span></div><div class="asset-sub"><span>${esc(version?.original_filename || '无版本')}</span><span class="sep">·</span><span>${bytes(version?.byte_size)}</span>${dims ? `<span class="sep">·</span><span>${esc(dims)}</span>` : ''}</div><div class="asset-tags">${tagPills((asset.tags || []).map(t => t.name))}</div></div>
    </article>`;
  }

  function openNewTextModal() {
    openModal({
      kicker: 'Create / 新建', title: '写一篇新文章',
      body: `<form id="newTextForm" data-form="new-text"><div class="form-error"></div><div class="field"><label>标题 <span class="mini">TITLE</span></label><input class="input" name="title" required maxlength="255" placeholder="例如：十月刊：把光留在纸上"></div><div class="field"><label>类型 <span class="mini">KIND</span></label><select class="select" name="kind"><option value="markdown">Markdown</option><option value="text">纯文本</option></select></div><div class="field"><label>别名 <span class="mini">SLUG · OPTIONAL</span></label><input class="input" name="slug" maxlength="128" placeholder="journal/issue-10"></div><div class="field"><label>标签 <span class="mini">TAGS · 逗号分隔</span></label><input class="input" name="tags" placeholder="秋季, Journal"></div><div class="field"><label>初始正文 <span class="mini">BODY</span></label><textarea class="textarea mono" name="body" rows="8"># 新文章

从这里开始写。</textarea></div></form>`,
      footer: `<span class="hint">保存后仍是草稿；发布后才进入公开快照。</span><button class="btn secondary sm" data-close-modal>取消</button><button class="btn primary sm" data-submit-form="newTextForm">创建并编辑</button>`,
    });
  }

  async function createTextFromForm(form) {
    const data = formDataObject(form);
    const asset = await api('/assets', { method: 'POST', body: { kind: data.kind, title: data.title, body: data.body || ' ', slug: data.slug || null, tags: splitTags(data.tags) } });
    closeModal();
    toast('文章已创建');
    navigate(`editor/${asset.id}`);
  }

  function openNewFolderModal() {
    const options = state.library.folders.map(f => `<option value="${attr(f.id)}">${esc(f.path)}</option>`).join('');
    openModal({
      kicker: 'Folder / 目录', title: '新建一个目录',
      body: `<form id="newFolderForm" data-form="new-folder"><div class="form-error"></div><div class="field"><label>目录名称 <span class="mini">NAME</span></label><input class="input" name="name" required maxlength="128" placeholder="例如：2026 秋季"></div><div class="field"><label>父目录 <span class="mini">PARENT</span></label><select class="select" name="parent_id"><option value="">根目录</option>${options}</select></div></form>`,
      footer: `<span class="hint">同一父目录下不能重名。</span><button class="btn secondary sm" data-close-modal>取消</button><button class="btn primary sm" data-submit-form="newFolderForm">创建目录</button>`,
    });
  }

  async function reloadLibrary() {
    await loadLibrary(true);
    $('#view').innerHTML = libraryHTML();
    bindImages($('#view'));
  }

  /* ------------------------------------------------------------
     Search
     ------------------------------------------------------------ */
  function kindIcon(kind) {
    return { image: 'image', video: 'video', markdown: 'text', pdf: 'doc', docx: 'doc', xlsx: 'sheet', text: 'text' }[kind] || 'file';
  }

  function openSearch() {
    if (!state.me) return;
    $('#searchOverlay').classList.add('open');
    $('#searchInput').value = '';
    updateSearchResults('');
    setTimeout(() => $('#searchInput').focus(), 30);
  }

  function closeSearch() {
    $('#searchOverlay').classList.remove('open');
  }

  function closeSidebar() {
    $('#sidebar').classList.remove('open');
    $('#sidebarScrim').classList.remove('open');
  }

  async function updateSearchResults(query) {
    const q = String(query || '').trim();
    const box = $('#searchResults');
    if (!q) {
      const pages = [['总览', '页面 · 今日访问、日历与库藏', 'dashboard'], ['内容库', '页面 · 目录、标签与内容列表', 'library'], ['上传', '页面 · 分片上传与队列', 'upload'], ['集合', '页面 · 顺序与公开契约', 'collections']].filter(([, , route]) => allowedRoute(route));
      box.innerHTML = `<div class="result-group-label">页面</div>${pages.map(([title, meta, route]) => `<button class="result-row" data-route="${route}"><span class="result-icon"><svg class="icon sm"><use href="#i-home"/></svg></span><span class="result-main"><span class="result-title">${title}</span><span class="result-meta">${meta}</span></span><span class="result-type">页面</span></button>`).join('')}`;
      return;
    }
    if (!can('asset.read')) {
      box.innerHTML = '<div class="result-group-label">提示</div><div class="inline-note">当前账号没有 asset.read 权限，无法检索内容。</div>';
      return;
    }
    box.innerHTML = '<div class="loading-page" style="min-height:90px"><span class="spinner"></span><span>SEARCHING</span></div>';
    try {
      const data = await api(`/search?q=${encodeURIComponent(q)}`);
      if (!data.items.length) {
        box.innerHTML = `<div class="result-group-label">没有匹配</div><div class="inline-note">没有找到「${esc(q)}」。试试标题、文件名或标签。</div>`;
        return;
      }
      box.innerHTML = `<div class="result-group-label">内容 · ${esc(data.mode === 'fulltext' ? '全文' : '标题/标签')}</div>${data.items.map(item => `<button class="result-row" data-route="asset/${attr(item.id)}"><span class="result-icon"><svg class="icon sm"><use href="#i-${kindIcon(item.kind)}"/></svg></span><span class="result-main"><span class="result-title">${esc(item.title || item.filename || '未命名')}</span><span class="result-meta">${esc(KINDS[item.kind]?.label || item.kind)} · ${esc(item.filename || '')} · ${esc(STATUS[item.status]?.[0] || item.status)}</span></span><span class="result-type">${esc(KINDS[item.kind]?.code || '—')}</span></button>`).join('')}`;
    } catch (error) {
      if (error.status !== 401) box.innerHTML = `<div class="inline-error">${esc(error.message)}</div>`;
    }
  }


  /* ------------------------------------------------------------
     Asset detail
     ------------------------------------------------------------ */
  async function renderAsset(id) {
    if (!id) throw new ApiFailure('缺少内容 ID。', 400);
    if (!state.library.folders.length) {
      try { state.library.folders = await api('/folders'); } catch { state.library.folders = []; }
    }
    const asset = await api(`/assets/${encodeURIComponent(id)}`);
    state.asset = asset;
    state.assetTags = (asset.tags || []).map(tag => tag.name);
    $('#view').innerHTML = assetDetailHTML(asset);
    bindImages($('#view'));
    bindAssetInputs();
  }

  function assetDetailHTML(asset) {
    const version = asset.current_version;
    const publishedVersion = (asset.versions || []).find(v => v.id === asset.published_version_id) || null;
    const kind = KINDS[asset.kind] || { label: asset.kind, code: '—' };
    const dims = version?.width && version?.height ? `${version.width} × ${version.height}` : (version?.duration_ms ? `${Math.round(version.duration_ms / 1000)}s` : '—');
    const canUpdate = can('asset.update');
    const canPublish = can('asset.publish');
    const canTrash = can('asset.trash');
    const visibilityLocked = asset.status === 'published' && !canPublish;
    const previewSrc = `${API}/assets/${encodeURIComponent(asset.id)}/preview?rev=${encodeURIComponent(asset.revision)}`;
    const showPreview = asset.kind === 'image' || hasReadyPreview(asset);
    return `
      <div class="detail-top">
        <div><button class="back-link" data-route="library"><svg class="icon sm"><use href="#i-arrow-left"/></svg>返回内容库</button><div class="detail-title-row"><h1 class="detail-title">${esc(asset.title || '未命名内容')}</h1>${starPill(asset.status, asset.visibility)}</div><div class="detail-sub"><span>${esc(kind.label)}</span><span class="sep">/</span><span>${esc(version?.original_filename || '无版本')}</span><span class="sep">/</span><span>内容 ID ${esc(asset.id.slice(0, 8))}…</span></div></div>
        <div class="detail-actions"><a class="btn secondary sm" href="${API}/assets/${encodeURIComponent(asset.id)}/file" target="_blank" rel="noopener"><svg class="icon sm"><use href="#i-download"/></svg>下载原件</a>${canPublish && asset.status !== 'trashed' ? '<button class="btn primary sm" data-action="publish-asset"><svg class="icon sm"><use href="#i-spark"/></svg>发布设置</button>' : ''}</div>
      </div>
      <div class="asset-detail-layout">
        <div class="preview-panel">
          <div class="preview-frame"><div class="preview-art type-art" data-kind="${attr(asset.kind)}"><span class="type-mark">${esc(kind.code)}<span class="type-sub">${esc(kind.label)}</span></span>${showPreview ? `<img class="preview-img asset-preview-img" data-preview-image src="${attr(previewSrc)}" alt="">` : ''}<div class="preview-overlay"><svg class="icon sm"><use href="#i-image"/></svg>${asset.kind === 'image' ? 'PREVIEW' : 'DERIVATIVE'} · ${esc(version?.mime || kind.label)} ${dims !== '—' ? '· ' + esc(dims) : ''}</div><div class="preview-tools"><button class="icon-btn" data-action="copy" data-value="${attr(asset.id)}" aria-label="复制内容 ID"><svg class="icon sm"><use href="#i-copy"/></svg></button><a class="icon-btn" href="${API}/assets/${encodeURIComponent(asset.id)}/file" target="_blank" rel="noopener" aria-label="下载"><svg class="icon sm"><use href="#i-download"/></svg></a></div></div></div>
          <div class="file-facts"><div class="file-fact"><span>文件</span><b title="${attr(version?.original_filename || '')}">${esc(version?.original_filename || '—')}</b></div><div class="file-fact"><span>大小</span><b>${bytes(version?.byte_size)}</b></div><div class="file-fact"><span>尺寸 / 时长</span><b>${esc(dims)}</b></div><div class="file-fact"><span>SHA-256</span><b title="${attr(version?.sha256 || '')}">${esc((version?.sha256 || '').slice(0, 12))}${version?.sha256 ? '…' : ''}</b></div></div>
          <div class="version-block"><div class="version-block-head"><span class="section-label">版本 / VERSIONS</span>${canUpdate ? '<label class="panel-link" for="replaceFile">替换文件 <svg class="icon sm"><use href="#i-plus"/></svg></label><input id="replaceFile" type="file" hidden>' : ''}</div>
            ${(asset.versions || []).slice().reverse().map(v => `<div class="version-row"><div class="v">v${v.version_no} ${v.id === asset.current_version_id ? '<small>当前</small>' : ''}</div><div><b>${esc(v.original_filename)}</b><div class="mono">${esc(dateFmt(v.created_at))} · ${bytes(v.byte_size)} · ${esc(v.mime)}</div></div><span class="mono">${v.id === asset.published_version_id ? '<span class="pill published" style="height:18px;font-size:7px"><i></i>线上</span>' : '保留'}</span><span class="mono">${esc(v.extract_status || '')}</span></div>`).join('') || '<div class="inline-note">还没有版本。</div>'}
          </div>
        </div>
        <div class="panel info-panel">
          <div class="info-panel-head"><div class="panel-title" style="font-size:20px">内容信息 <small>EDITING FIELDS · 不影响线上</small></div><button class="icon-btn bordered" style="width:30px;height:30px" data-action="copy" data-value="${attr(asset.id)}" aria-label="复制 ID"><svg class="icon sm"><use href="#i-copy"/></svg></button></div>
          <div class="tabs" id="assetTabs"><button class="tab active" data-action="asset-tab" data-tab="basic">基本信息</button><button class="tab" data-action="asset-tab" data-tab="publish">发布与生命周期</button></div>
          <div class="tab-panel active" data-asset-panel="basic">
            <div class="publish-status"><div class="left"><svg class="icon"><use href="#i-spark"/></svg><div><strong>${asset.status === 'published' ? `线上运行中 · v${publishedVersion?.version_no ?? '—'}` : asset.status === 'trashed' ? '在回收站中' : '尚未发布'}</strong><small>${asset.published_version_id ? '当前线上快照存在；编辑字段不会改变访客看到的内容。' : '发布后才会生成公开快照与公开地址。'}</small></div></div>${starPill(asset.status, asset.visibility)}</div>
            <form id="assetForm" data-form="asset-update"><div class="form-error"></div>
              <div class="field"><label>标题 <span class="mini">TITLE</span></label><input class="input" name="title" maxlength="255" value="${attr(asset.title || '')}"></div>
              <div class="form-row"><div class="field"><label>别名 <span class="mini">SLUG</span></label><input class="input" name="slug" maxlength="128" value="${attr(asset.slug || '')}"></div><div class="field"><label>目录 <span class="mini">FOLDER</span></label><select class="select" name="folder_id" id="assetFolder"><option value="">未归档</option>${state.library.folders.map(f => `<option value="${attr(f.id)}" ${asset.folder_id === f.id ? 'selected' : ''}>${esc(f.path)}</option>`).join('')}</select></div></div>
              <div class="field"><label>摘要 <span class="mini">SUMMARY</span></label><textarea class="textarea" name="summary" maxlength="1024">${esc(asset.summary || '')}</textarea></div>
              <div class="form-row"><div class="field"><label>语言 <span class="mini">LANGUAGE</span></label><input class="input" name="language" maxlength="16" value="${attr(asset.language || 'zh-CN')}"></div><div class="field"><label>可见性 <span class="mini">VISIBILITY</span></label><select class="select" name="visibility" ${visibilityLocked ? 'disabled' : ''}>${Object.entries(VISIBILITY).map(([key, label]) => `<option value="${key}" ${asset.visibility === key ? 'selected' : ''}>${label}</option>`).join('')}</select><div class="help">${visibilityLocked ? '已发布内容改可见性需要发布权限。' : '可见性修改属于发布动作，立即影响公开区。'}</div></div></div>
              <div class="field"><label>标签 <span class="mini">TAGS</span></label><div class="tag-editor" id="assetTagEditor">${state.assetTags.map(tag => `<span class="tag">${esc(tag)}<button type="button" data-action="tag-remove" data-tag="${attr(tag)}" aria-label="移除"><svg class="icon" style="width:10px;height:10px"><use href="#i-close"/></svg></button></span>`).join('')}<input id="tagInput" placeholder="输入标签后回车" autocomplete="off"></div></div>
              ${asset.kind === 'markdown' || asset.kind === 'text' ? `<div class="field"><label>正文 <span class="mini">CURRENT BODY</span></label><div class="inline-note">${canUpdate ? `<button type="button" class="btn secondary sm" data-route="editor/${attr(asset.id)}"><svg class="icon sm"><use href="#i-text"/></svg>编辑正文</button>` : '有修改权限的账号可以编辑正文。'}</div></div>` : ''}
              <div class="form-actions"><span class="hint">保存会增加 1 个 revision；发布另算一次动作。</span><button class="btn primary sm" type="submit" ${canUpdate ? '' : 'disabled'}>保存修改</button></div>
            </form>
          </div>
          <div class="tab-panel" data-asset-panel="publish">
            <div class="publish-status"><div class="left"><svg class="icon"><use href="#i-globe"/></svg><div><strong>${asset.status === 'published' ? '已发布 · 公开快照' : '未发布'}</strong><small>${asset.published_slug ? `已发布别名：${esc(asset.published_slug)}` : '还没有发布别名。'}</small></div></div>${starPill(asset.status, asset.visibility)}</div>
            <div class="field"><label>已发布别名 <span class="mini">PUBLISHED SLUG</span></label><input class="input mono" value="${attr(asset.published_slug || '—')}" readonly></div>
            <div class="form-row"><div class="field"><label>修订号 <span class="mini">REVISION</span></label><input class="input mono" value="${asset.revision}" readonly></div><div class="field"><label>可见性 <span class="mini">VISIBILITY</span></label><input class="input" value="${esc(VISIBILITY[asset.visibility] || asset.visibility)}" readonly></div></div>
            <div class="field"><label>生命周期操作 <span class="mini">ACTIONS</span></label><div class="wide-actions">${canPublish && asset.status !== 'trashed' ? '<button class="btn primary sm" data-action="publish-asset">发布 / 定时发布</button>' : ''}${canPublish && asset.status === 'published' ? '<button class="btn secondary sm" data-action="unpublish-asset">撤回</button>' : ''}${canPublish && ['draft','published'].includes(asset.status) ? '<button class="btn secondary sm" data-action="archive-asset">归档</button>' : ''}${canTrash && asset.status !== 'trashed' ? '<button class="btn secondary sm" style="color:#8E4650;border-color:#E5CFD1" data-action="trash-asset">移入回收站</button>' : ''}${canTrash && asset.status === 'trashed' ? '<button class="btn primary sm" data-action="restore-asset">恢复</button>' : ''}${isOwner() && asset.status === 'trashed' ? '<button class="btn secondary sm" style="color:#8E4650;border-color:#E5CFD1" data-action="purge-asset">彻底删除</button>' : ''}</div></div>
            <div class="upload-note" style="margin-top:15px">公开地址由已发布版本生成。发布失败时状态落在“更不可访问”一侧，可用 <span class="inline-code">fsck</span> 修复公开链接。</div>
          </div>
        </div>
      </div>`;
  }

  function bindAssetInputs() {
    const tagInput = $('#tagInput');
    if (tagInput) {
      tagInput.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ',') return;
        event.preventDefault();
        const value = tagInput.value.trim().replace(/,$/, '');
        if (!value) return;
        if (!state.assetTags.includes(value)) state.assetTags.push(value);
        renderTagEditor();
        $('#tagInput')?.focus();
      });
    }
  }

  function renderTagEditor() {
    const box = $('#assetTagEditor');
    if (!box) return;
    const input = $('#tagInput');
    box.innerHTML = `${state.assetTags.map(tag => `<span class="tag">${esc(tag)}<button type="button" data-action="tag-remove" data-tag="${attr(tag)}" aria-label="移除"><svg class="icon" style="width:10px;height:10px"><use href="#i-close"/></svg></button></span>`).join('')}<input id="tagInput" placeholder="输入标签后回车" autocomplete="off">`;
    box.querySelector('input')?.focus();
    bindAssetInputs();
  }

  async function saveAsset(form) {
    const asset = state.asset;
    if (!asset) return;
    const data = formDataObject(form);
    const changes = {
      revision: asset.revision,
      title: data.title,
      summary: data.summary,
      language: data.language || 'zh-CN',
      slug: data.slug || null,
      folder_id: data.folder_id || null,
      tags: state.assetTags,
    };
    const visibilitySelect = form.querySelector('select[name=visibility]');
    if (visibilitySelect && !visibilitySelect.disabled && visibilitySelect.value !== asset.visibility) {
      changes.visibility = visibilitySelect.value;
    }
    const updated = await api(`/assets/${encodeURIComponent(asset.id)}`, { method: 'PATCH', body: changes });
    state.asset = updated;
    state.assetTags = (updated.tags || []).map(t => t.name);
    toast('内容信息已保存');
    await renderAsset(updated.id);
  }

  function openPublishModal() {
    const asset = state.asset;
    if (!asset) return;
    openModal({
      kicker: 'Publish / 发布', title: '把这一版送到线上',
      body: `<form id="publishForm" data-form="publish-asset"><div class="form-error"></div><div class="publish-status"><div class="left"><svg class="icon"><use href="#i-spark"/></svg><div><strong>当前待发布版本 · v${asset.current_version?.version_no ?? '—'}</strong><small>发布后线上快照切换至这一版，并复制标题、摘要与标签。</small></div></div>${starPill(asset.status, asset.visibility)}</div><div class="field"><label>可见性 <span class="mini">VISIBILITY</span></label><input class="input" value="${esc(VISIBILITY[asset.visibility] || asset.visibility)}" readonly><div class="help">先在上方基本信息里修改并保存可见性，再发布。</div></div><div class="field"><label>定时发布 <span class="mini">OPTIONAL · ASIA/SHANGHAI</span></label><input class="input" type="datetime-local" name="publish_at"><div class="help">留空立即发布；未来时间只登记计划，到点由 worker 执行。</div></div></form>`,
      footer: `<button class="btn secondary sm" data-close-modal>取消</button><button class="btn primary sm" data-submit-form="publishForm">确认发布</button>`,
    });
  }

  async function submitPublish(form) {
    const asset = state.asset;
    if (!asset) return;
    const value = form.elements.publish_at?.value;
    const publishAt = value ? new Date(value).toISOString() : null;
    const updated = await api(`/assets/${encodeURIComponent(asset.id)}/publish`, { method: 'POST', body: { revision: asset.revision, publish_at: publishAt } });
    closeModal();
    toast(publishAt ? '已登记定时发布' : '已发布');
    state.asset = updated;
    await renderAsset(updated.id);
  }

  function confirmAssetAction(action) {
    const asset = state.asset;
    if (!asset) return;
    const map = {
      unpublish: { title: '撤回这次发布？', body: `公开地址会立即 404；管理端仍保留原件与全部版本。`, label: '撤回发布', path: 'unpublish' },
      archive: { title: '归档这条内容？', body: `归档会撤销公开链接，并从常规内容列表中移出。`, label: '归档', path: 'archive' },
      trash: { title: '移入回收站？', body: `公开链接会先撤下，内容进入回收站保留 30 天。`, label: '移入回收站', path: 'trash' },
      restore: { title: '从回收站恢复？', body: `恢复后不会自动重新公开，需要再次发布。`, label: '恢复', path: 'restore' },
    };
    const item = map[action];
    if (!item) return;
    confirmDialog({
      kicker: `Lifecycle / ${item.label}`, title: item.title, body: item.body, confirmLabel: item.label,
      onConfirm: async () => {
        const updated = await api(`/assets/${encodeURIComponent(asset.id)}/${item.path}`, { method: 'POST', body: { revision: asset.revision } });
        toast(item.label + '完成');
        state.asset = updated;
        await renderAsset(updated.id);
      },
    });
  }

  function confirmPurge() {
    const asset = state.asset;
    if (!asset) return;
    confirmDialog({
      kicker: 'Permanent delete / 彻底删除', title: '彻底删除这条内容？',
      body: `原件、全部版本、派生文件与公开链接都会永久删除，且不可撤销。仍被集合或封面引用时服务端会拒绝。`,
      confirmLabel: '彻底删除',
      onConfirm: async () => {
        await api(`/assets/${encodeURIComponent(asset.id)}`, { method: 'DELETE' });
        toast('内容已彻底删除');
        navigate('trash');
      },
    });
  }

  /* ------------------------------------------------------------
     Markdown editor
     ------------------------------------------------------------ */
  async function renderEditor(id) {
    if (!id) throw new ApiFailure('缺少内容 ID。', 400);
    const asset = await api(`/assets/${encodeURIComponent(id)}`);
    if (!['markdown', 'text'].includes(asset.kind)) throw new ApiFailure('只有 Markdown 与纯文本可以在线编辑正文。', 400);
    const body = await api(`/assets/${encodeURIComponent(id)}/body`);
    state.asset = asset;
    state.editor = { asset, body: body.body, revision: body.revision, dirty: false };
    state.assetTags = (asset.tags || []).map(t => t.name);
    $('#view').innerHTML = editorHTML(asset, body.body);
    updateEditorPreview(false);
  }

  function editorHTML(asset, body) {
    return `<div class="editor-head"><div><button class="back-link" data-route="asset/${attr(asset.id)}"><svg class="icon sm"><use href="#i-arrow-left"/></svg>返回内容详情</button><h1>${esc(asset.title || '未命名文章')}</h1><div class="detail-sub"><span>${esc(KINDS[asset.kind]?.label || asset.kind)}</span><span class="sep">/</span><span>${esc(asset.slug || '无别名')}</span><span class="sep">/</span>${starPill(asset.status, asset.visibility)}</div></div><div class="actions"><button class="btn secondary sm" data-action="editor-preview"><svg class="icon sm"><use href="#i-eye"/></svg>预览</button><button class="btn secondary sm" data-action="editor-save"><svg class="icon sm"><use href="#i-check"/></svg>保存草稿</button>${can('asset.publish') ? '<button class="btn primary sm" data-action="publish-asset"><svg class="icon sm"><use href="#i-spark"/></svg>发布</button>' : ''}</div></div>
      <div class="editor-meta-bar"><span class="item"><svg class="icon" style="width:12px;height:12px"><use href="#i-clock"/></svg><b id="editorState">最后保存 v${asset.current_version?.version_no ?? '—'} · ${esc(dateFmt(asset.updated_at))}</b></span><span class="item">字数 <b id="mdCount">—</b></span><span class="item">当前 revision <b id="editorRevision">${asset.revision}</b></span><span class="item" style="margin-left:auto;color:#7B6889"><svg class="icon" style="width:12px;height:12px"><use href="#i-info"/></svg>${esc(KINDS[asset.kind]?.label || '')} · 保存会生成新版本</span></div>
      <div class="editor-layout"><div class="editor-pane"><div class="pane-head"><span class="pane-label">SOURCE / 源文</span><button class="tool" data-action="insert-md" data-before="## " data-after="" title="标题">H</button><button class="tool serif" data-action="insert-md" data-before="**" data-after="**" title="加粗"><b>B</b></button><button class="tool italic" data-action="insert-md" data-before="*" data-after="*" title="斜体">I</button><button class="tool" data-action="insert-md" data-before="\`" data-after="\`" title="行内代码"><svg class="icon sm"><use href="#i-command"/></svg></button><button class="tool" data-action="insert-md" data-before="![说明](asset:" data-after=")" title="图片引用"><svg class="icon sm"><use href="#i-image"/></svg></button></div><textarea class="editor-textarea" id="mdEditor" spellcheck="false" aria-label="正文">${esc(body)}</textarea></div><div class="preview-pane"><div class="pane-head"><span class="pane-label">PREVIEW / 预览</span><span class="pill published" style="height:20px;font-size:7.5px"><i></i>安全预览</span></div><div class="preview-body" id="mdPreview"></div></div></div>`;
  }

  function updateEditorPreview(markDirty = true) {
    if (!state.editor) return;
    const editor = $('#mdEditor');
    if (!editor) return;
    const body = editor.value;
    state.editor.body = body;
    if (markDirty) state.editor.dirty = true;
    const count = body.replace(/\s/g, '').length;
    const countEl = $('#mdCount');
    if (countEl) countEl.textContent = `${num(count)} 字`;
    const stateEl = $('#editorState');
    if (stateEl && markDirty) { stateEl.textContent = '有未保存的修改'; stateEl.style.color = '#9A6B34'; }
    clearTimeout(state.editor.previewTimer);
    state.editor.previewController?.abort();
    const snapshot = state.editor;
    const requestNo = snapshot.previewRequest = (snapshot.previewRequest || 0) + 1;
    const preview = $('#mdPreview');
    if (preview) preview.textContent = '正在渲染预览…';
    snapshot.previewTimer = setTimeout(async () => {
      const controller = new AbortController();
      snapshot.previewController = controller;
      try {
        const rendered = await api(`/assets/${encodeURIComponent(snapshot.asset.id)}/render`, {
          method: 'POST', body: { body }, signal: controller.signal,
        });
        if (state.editor !== snapshot || snapshot.previewRequest !== requestNo || !preview?.isConnected) return;
        preview.innerHTML = rendered.html || '<div class="inline-note">左侧输入正文后，这里会显示预览。</div>';
      } catch (error) {
        if (!controller.signal.aborted && state.editor === snapshot && snapshot.previewRequest === requestNo && preview?.isConnected) {
          preview.textContent = `预览失败：${error.message}`;
        }
      }
    }, markDirty ? 250 : 0);
  }

  async function saveEditor() {
    const snapshot = state.editor;
    const editor = $('#mdEditor');
    if (!snapshot || !editor || snapshot.saving) return;
    const body = editor.value;
    const stateEl = $('#editorState');
    snapshot.saving = true;
    if (stateEl) { stateEl.textContent = '保存中…'; stateEl.style.color = ''; }
    try {
      const updated = await api(`/assets/${encodeURIComponent(snapshot.asset.id)}`, { method: 'PATCH', body: { revision: snapshot.revision, body } });
      if (state.editor !== snapshot || !editor.isConnected) return;
      state.asset = updated;
      snapshot.asset = updated;
      snapshot.revision = updated.revision;
      snapshot.body = editor.value;
      snapshot.dirty = editor.value !== body;
      $('#editorRevision').textContent = updated.revision;
      if (stateEl) stateEl.textContent = snapshot.dirty ? '有未保存的修改' : `已保存 · v${updated.current_version?.version_no ?? '—'} · ${timeOnly(updated.updated_at)}`;
      toast('正文已保存为新版本');
    } catch (error) {
      if (state.editor !== snapshot || !editor.isConnected) return;
      if (error.status === 409) {
        try {
          await renderEditor(snapshot.asset.id);
          toast('内容已被其他人修改，已重新加载最新正文和修订号。', 'error');
        } catch (reloadError) {
          if (stateEl) stateEl.textContent = '修订冲突，重新加载失败，请重试';
          if (reloadError.status !== 401) toast(reloadError.message, 'error');
        }
      } else {
        if (stateEl) { stateEl.textContent = '保存失败'; stateEl.style.color = '#8E4650'; }
        if (error.status !== 401) toast(error.message, 'error');
      }
    } finally {
      snapshot.saving = false;
    }
  }

  function insertMarkdown(button) {
    const editor = $('#mdEditor');
    if (!editor) return;
    const before = button.dataset.before || '';
    const after = button.dataset.after || '';
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    const selected = editor.value.slice(start, end);
    const text = `${before}${selected}${after}`;
    editor.setRangeText(text, start, end, 'end');
    editor.focus();
    updateEditorPreview();
  }


  async function copyText(text) {
    if (!text) return;
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else {
        const area = document.createElement('textarea');
        area.value = text; document.body.appendChild(area); area.select();
        document.execCommand('copy'); area.remove();
      }
    } catch { toast('复制失败，请手动选择文本', 'error'); }
  }


  /* ------------------------------------------------------------
     Upload
     ------------------------------------------------------------ */
  function renderUploadPage() {
    $('#view').innerHTML = `<div class="page-head"><div><div class="eyebrow"><span class="dot"></span>Ingest<span class="slash">/</span><span class="en">上传</span></div><h1>把文件交给内容库</h1><p class="desc">按文件头识别类型；分片上传，服务端合并后再登记草稿。上传完成不等于发布。</p></div><div class="actions"><button class="btn primary sm" data-action="upload-pick"><svg class="icon sm"><use href="#i-plus"/></svg>选择文件</button></div></div>
      <div class="upload-layout"><div><div class="dropzone" id="dropzone" role="button" tabindex="0" aria-label="上传文件"><div class="upload-glyph"><svg class="icon lg"><use href="#i-upload"/></svg></div><h3>拖入文件，或从电脑选择</h3><p>支持分片续传；类型、大小与哈希由服务端重新校验。</p><span class="btn secondary sm">选择文件</span><div class="allowed"><span>JPG</span><span>PNG</span><span>GIF</span><span>WEBP</span><span>MP4</span><span>WEBM</span><span>MD</span><span>PDF</span><span>DOCX</span><span>XLSX</span><span>TXT</span></div></div><input id="fileInput" type="file" hidden multiple>
        <div class="upload-queue"><div class="queue-head"><div class="queue-title">上传队列 <span id="queueCount">${state.uploadQueue.length} 项</span></div><div class="queue-actions"><button class="btn ghost xs" data-action="upload-clear">清理已完成</button></div></div><div class="queue-list" id="uploadQueue">${uploadQueueHTML()}</div></div>
      </div><aside class="upload-side"><div class="panel"><div class="panel-title" style="font-size:18px">服务端校验 <small>SERVER-SIDE CHECKS</small></div><div class="rules-list"><div class="rule-row"><span><svg class="icon"><use href="#i-image"/></svg>图片</span><b>类型头 · 像素上限</b></div><div class="rule-row"><span><svg class="icon"><use href="#i-video"/></svg>视频</span><b>分片 · 容器探测</b></div><div class="rule-row"><span><svg class="icon"><use href="#i-text"/></svg>Markdown / 文本</span><b>UTF-8</b></div><div class="rule-row"><span><svg class="icon"><use href="#i-doc"/></svg>PDF / Word / Excel</span><b>文件头 · 配额</b></div></div><div class="upload-note"><b>不接受</b> HTML、SVG、ZIP、可执行文件与旧版 .doc / .xls。分片以 4 MiB 为单位，临时失败自动重试两次；仍失败可在队列中重试，已上传分片会保留。</div></div><div class="panel" style="margin-top:14px"><div class="panel-title" style="font-size:17px">入库之后 <small>WHAT HAPPENS NEXT</small></div><div class="definition-grid" style="grid-template-columns:1fr"><div class="definition-row"><span>版本</span><b>自动成为 v1 草稿</b></div><div class="definition-row"><span>派生</span><b>缩略图 / 抽取入队</b></div><div class="definition-row"><span>公开</span><b>发布后才可见</b></div></div></div></aside></div>
      <div class="process-strip"><div class="process-item"><div class="process-num">01</div><div><b>识别与校验</b><span>只读文件头判断真实类型，扩展名、声明类型与大小上限三者一致才继续。</span><em>INSPECTION · SHA-256</em></div></div><div class="process-item"><div class="process-num">02</div><div><b>落盘并登记</b><span>原件按内容 ID 分片写入，SQLite 登记版本；此时内容仍是草稿、私有。</span><em>ORIGINAL · VERSION ROW</em></div></div><div class="process-item"><div class="process-num">03</div><div><b>派生与发布</b><span>缩略图、抽取任务进入队列。发布不会等派生完成，原图地址始终可用。</span><em>QUEUE · PUBLISH SNAPSHOT</em></div></div></div>`;
    bindUploadDropzone();
  }

  function uploadQueueHTML() {
    if (!state.uploadQueue.length) {
      return '<div class="inline-note" style="border:0;background:transparent;padding:12px 0">还没有上传任务。选择文件后会在这里显示分片进度。</div>';
    }
    return state.uploadQueue.map(item => {
      const kind = KINDS[item.kind] || { label: '文件', code: 'FILE' };
      const percent = Math.min(100, Math.round(item.progress || 0));
      const stateLabel = item.retrying ? '<span class="pill pending"><i></i>重试分片中</span>' : item.status === 'done' ? '<span class="pill published"><i></i>已登记</span>' : item.status === 'error' ? '<span class="pill failed"><i></i>失败</span>' : item.status === 'queued' ? '<span class="pill pending"><i></i>等待中</span>' : '<span class="pill pending"><i></i>上传中</span>';
      return `<div class="queue-row"><div class="queue-thumb type-art" data-kind="${attr(item.kind)}"><span class="type-mark">${esc(kind.code)}</span></div><div><div class="queue-name">${esc(item.name)}</div><div class="queue-sub">${esc(kind.label)} · ${bytes(item.size)} · ${item.status === 'error' ? esc(item.error || '失败') : item.status === 'done' ? '内容已登记' : `${percent}%`}</div><div class="queue-progress"><span style="width:${percent}%"></span></div></div><div class="queue-size">${item.assetId ? `<button class="btn secondary xs" data-route="asset/${attr(item.assetId)}">查看</button>` : bytes(item.size)}</div><div class="queue-state">${stateLabel}${item.status === 'error' && item.retryable ? `<button class="btn secondary xs" data-action="upload-retry" data-id="${attr(item.id)}">重试失败分片</button>` : ''}${item.status === 'done' || item.status === 'error' ? `<button class="icon-btn queue-remove" data-action="upload-remove" data-id="${attr(item.id)}" aria-label="移除"><svg class="icon sm"><use href="#i-close"/></svg></button>` : ''}</div></div>`;
    }).join('');
  }

  function bindUploadDropzone() {
    const zone = $('#dropzone');
    if (!zone) return;
    zone.addEventListener('click', () => $('#fileInput')?.click());
    zone.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); $('#fileInput')?.click(); } });
    ['dragenter', 'dragover'].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); zone.classList.add('drag'); }));
    ['dragleave', 'drop'].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); zone.classList.remove('drag'); }));
    zone.addEventListener('drop', event => { const files = [...(event.dataTransfer?.files || [])]; if (files.length) addUploadFiles(files); });
  }

  function addUploadFiles(files) {
    for (const file of files) {
      state.uploadQueue.push({ id: uuid(), file, name: file.name, size: file.size, kind: guessKind(file.name), status: 'queued', progress: 0, error: null, assetId: null });
    }
    renderUploadQueue();
    processUploadQueue();
  }

  function guessKind(filename) {
    const ext = String(filename).toLowerCase().split('.').pop();
    return ({ jpg: 'image', jpeg: 'image', png: 'image', gif: 'image', webp: 'image', mp4: 'video', webm: 'video', md: 'markdown', markdown: 'markdown', pdf: 'pdf', docx: 'docx', xlsx: 'xlsx', txt: 'text' })[ext] || 'text';
  }

  function renderUploadQueue() {
    const list = $('#uploadQueue');
    if (list) list.innerHTML = uploadQueueHTML();
    const count = $('#queueCount');
    if (count) count.textContent = `${state.uploadQueue.length} 项`;
  }

  async function processUploadQueue() {
    if (state.uploadBusy) return;
    const item = state.uploadQueue.find(row => row.status === 'queued');
    if (!item) return;
    state.uploadBusy = true;
    try {
      await uploadOne(item, null, false);
    } catch (error) {
      if (error.status !== 401) {
        item.status = 'error';
        item.error = error.message;
        renderUploadQueue();
        toast(error.message, 'error');
      }
    } finally {
      state.uploadBusy = false;
      renderUploadQueue();
      if (state.uploadQueue.some(row => row.status === 'queued')) processUploadQueue();
    }
  }

  function retryableUploadError(error) {
    return error.status === 0 || error.status === 408 || error.status === 429 || error.status >= 500;
  }

  async function retryUploadPart(item, send, fromAssetPage) {
    for (let attempt = 0; ; attempt++) {
      try {
        await send();
        item.retrying = false;
        return;
      } catch (error) {
        if (!retryableUploadError(error) || attempt >= 2) throw error;
        item.retrying = true;
        if (!fromAssetPage) renderUploadQueue();
        await new Promise(resolve => setTimeout(resolve, 500 * (2 ** attempt)));
      }
    }
  }

  async function uploadOne(item, assetId = null, fromAssetPage = false) {
    const file = item.file;
    if (!file) throw new ApiFailure('没有可上传的文件。', 400);
    item.status = 'uploading';
    item.progress = item.progress || 1;
    item.retrying = false;
    if (!fromAssetPage) renderUploadQueue();
    let session = item.uploadSession || null;
    try {
      session = session || await api('/uploads', {
        method: 'POST',
        body: { filename: file.name, content_type: file.type || null, asset_id: assetId || null },
      });
      item.uploadSession = session;
      if (session.byte_limit && file.size > session.byte_limit) {
        throw new ApiFailure(`文件超过该类型上限（${bytes(session.byte_limit)}）。`, 413);
      }
      const chunkSize = 4 * 1024 * 1024;
      const parts = Math.max(1, Math.ceil(file.size / chunkSize));
      for (let i = item.nextPart || 0; i < parts; i++) {
        const start = i * chunkSize;
        const blob = file.slice(start, Math.min(file.size, start + chunkSize));
        await retryUploadPart(item, async () => api(`/uploads/${encodeURIComponent(session.id)}/parts/${i + 1}`, {
          method: 'PUT', body: blob, raw: true, headers: { 'Content-Type': 'application/octet-stream' },
        }), fromAssetPage);
        item.nextPart = i + 1;
        item.progress = Math.round(((i + 1) / parts) * 92);
        if (!fromAssetPage) renderUploadQueue();
      }
      item.completeKey = item.completeKey || uuid();
      const completed = await api(`/uploads/${encodeURIComponent(session.id)}/complete`, {
        method: 'POST', headers: { 'Idempotency-Key': item.completeKey },
      });
      item.uploadSession = null;
      item.retryable = false;
      item.status = 'done';
      item.progress = 100;
      item.assetId = completed.asset_id;
      if (!fromAssetPage) {
        renderUploadQueue();
        toast(`上传完成 · ${file.name}`);
      } else if (assetId) {
        toast('新版本已上传，线上版本尚未改变');
        await renderAsset(assetId);
      }
      return completed;
    } catch (error) {
      item.status = 'error';
      item.error = error.message;
      item.retryable = retryableUploadError(error);
      item.retrying = false;
      if (!item.retryable && session?.id) {
        item.uploadSession = null;
        item.nextPart = 0;
        try { await api(`/uploads/${encodeURIComponent(session.id)}`, { method: 'DELETE' }); } catch { /* best effort */ }
      }
      if (!fromAssetPage) renderUploadQueue();
      throw error;
    }
  }

  async function uploadReplacement(file, assetId) {
    const item = { id: uuid(), file, name: file.name, size: file.size, kind: guessKind(file.name), status: 'uploading', progress: 0, error: null, assetId: null };
    return uploadOne(item, assetId, true);
  }


  /* ------------------------------------------------------------
     Collections
     ------------------------------------------------------------ */
  async function renderCollections() {
    const [collections, assets] = await Promise.all([api('/collections'), api('/assets?limit=200')]);
    state.collections = collections;
    state.collectionAssets = assets.items;
    if (isOwner() && !state.sites.length) {
      try { state.sites = await api('/sites'); } catch { state.sites = []; }
    }
    if (!state.collection || !collections.some(item => item.id === state.collection.id)) {
      state.collection = collections[0] || null;
    }
    if (state.collection) await loadCollectionItems();
    else { state.collectionItems = []; }
    $('#view').innerHTML = collectionsHTML();
    bindImages($('#view'));
  }

  async function loadCollectionItems() {
    if (!state.collection) return;
    const items = await api(`/collections/${encodeURIComponent(state.collection.id)}/items`);
    const details = await Promise.all(items.map(async item => {
      try { return { item, asset: await api(`/assets/${encodeURIComponent(item.asset_id)}`) }; }
      catch { return { item, asset: null }; }
    }));
    state.collectionItems = details;
    state.collectionDirty = false;
  }

  function collectionsHTML() {
    const collection = state.collection;
    const sites = Object.fromEntries(state.sites.map(site => [site.id, site]));
    return `<div class="page-head"><div><div class="eyebrow"><span class="dot"></span>Collections<span class="slash">/</span><span class="en">集合</span></div><h1>给内容排一个顺序</h1><p class="desc">集合是网站读取内容的稳定契约；条目顺序一次事务提交，别名全局唯一。</p></div><div class="actions"><button class="btn secondary sm" data-action="collection-preview"><svg class="icon sm"><use href="#i-command"/></svg>API 预览</button>${can('collection.manage') ? '<button class="btn primary sm" data-action="new-collection"><svg class="icon sm"><use href="#i-plus"/></svg>新建集合</button>' : ''}</div></div>
      <div class="collections-layout"><aside class="collection-list"><div class="collection-list-head"><span>${state.collections.length} COLLECTIONS</span>${can('collection.manage') ? '<button class="add-mini" data-action="new-collection" aria-label="新建集合"><svg class="icon sm"><use href="#i-plus"/></svg></button>' : ''}</div>${state.collections.map(item => `<button class="collection-row ${collection?.id === item.id ? 'active' : ''}" data-action="collection-select" data-id="${attr(item.id)}"><div class="alias">${esc(item.slug)}</div><div class="name"><b>${esc(item.title || item.slug)}</b><span>${item.id === collection?.id ? state.collectionItems.length : ''}</span></div><div class="meta">${esc(sites[item.site_id]?.name || '库级集合')} · ${item.allowed_kinds?.length ? esc(item.allowed_kinds.join(' / ')) : '不限类型'}</div></button>`).join('') || '<div class="inline-note" style="margin:8px">还没有集合。</div>'}<div class="filter-rule" style="margin-top:12px">集合的 updated_at 只由可见条目的发布时间决定。改一条匿名看不见的内容，不会改变公开响应。</div></aside>
      ${collection ? collectionMainHTML(collection, sites) : '<div class="collection-main"><div class="empty-state"><div class="empty-icon"><svg class="icon"><use href="#i-layers"/></svg></div><h2>还没有集合</h2><p>集合让网站按别名读取一组有顺序的内容。先建一个，例如 <span class="mono">home.hero</span>。</p><div class="empty-actions"><button class="btn primary sm" data-action="new-collection">新建集合</button></div></div></div>'}
      </div>`;
  }

  function collectionMainHTML(collection, sites) {
    return `<div class="collection-main"><div class="collection-header"><div class="collection-header-top"><div><div class="collection-kicker">Collection / ${esc(sites[collection.site_id]?.key || 'library-level')}</div><h2>${esc(collection.title || collection.slug)}</h2><div class="collection-meta"><span class="tag mono" style="font-family:var(--mono);font-size:8px">${esc(collection.slug)}</span><span class="chip">${esc(sites[collection.site_id]?.name || '库级')}</span>${(collection.allowed_kinds || []).map(kind => `<span class="chip">${esc(KINDS[kind]?.label || kind)}</span>`).join('') || '<span class="chip">不限类型</span>'}</div></div><div class="collection-actions"><button class="btn secondary sm" data-action="collection-preview"><svg class="icon sm"><use href="#i-external"/></svg>查看结构</button>${can('collection.manage') ? `<button class="btn primary sm" data-action="collection-save-order" ${state.collectionDirty ? '' : 'disabled'}><svg class="icon sm"><use href="#i-check"/></svg>保存顺序</button>` : ''}</div></div></div>
      <div class="order-list"><div class="order-head"><span></span><span>预览</span><span>标题 / 条目说明</span><span>类型</span><span>状态</span><span style="text-align:right">调整</span></div>
      ${state.collectionItems.length ? state.collectionItems.map(({item, asset}, index) => {
        const version = asset?.current_version;
        return `<div class="order-row" data-asset-id="${attr(item.asset_id)}"><span class="drag-handle"><svg class="icon sm"><use href="#i-drag"/></svg></span><span class="order-thumb type-art" data-kind="${attr(asset?.kind || 'text')}"><span class="type-mark">${esc(KINDS[asset?.kind]?.code || '—')}</span>${asset ? thumbImg(asset) : ''}</span><div><div class="order-title">${esc(asset?.title || item.asset_id)}</div><div class="order-caption">${esc(item.caption || '无 caption')} · ${esc(version?.original_filename || '')}</div></div><span class="order-kind">${esc(KINDS[asset?.kind]?.code || '—')}</span><span>${asset ? starPill(asset.status, asset.visibility) : '<span class="pill private"><i></i>不可读</span>'}</span>${can('collection.manage') ? `<div class="order-move"><button data-action="collection-move" data-dir="up" data-index="${index}" aria-label="上移"><svg class="icon sm"><use href="#i-chevron-up"/></svg></button><button data-action="collection-move" data-dir="down" data-index="${index}" aria-label="下移"><svg class="icon sm"><use href="#i-chevron-down"/></svg></button><button data-action="collection-remove" data-id="${attr(item.asset_id)}" aria-label="移除"><svg class="icon sm"><use href="#i-close"/></svg></button></div>` : '<div class="order-move"></div>'}</div>`;
      }).join('') : `<div class="empty-state" style="border:0;padding:30px 10px"><div class="empty-icon"><svg class="icon"><use href="#i-layers"/></svg></div><h2>集合还是空的</h2><p>从内容库添加条目，顺序就是网站读取到的顺序。</p>${can('collection.manage') ? '<div class="empty-actions"><button class="btn primary sm" data-action="collection-add-item">添加条目</button></div>' : ''}</div>`}
      <div class="order-foot"><span class="left"><svg class="icon" style="width:13px;height:13px"><use href="#i-info"/></svg>${state.collectionDirty ? '顺序有未保存修改。' : '顺序会整体提交；同一集合内同一内容只出现一次。'}</span>${can('collection.manage') ? '<button class="btn secondary xs" data-action="collection-add-item"><svg class="icon sm"><use href="#i-plus"/></svg>添加条目</button>' : ''}</div></div>
      <div class="detail-lower"><section class="panel"><div class="panel-head" style="margin-bottom:7px"><div class="panel-title" style="font-size:18px">读取契约 <small>PUBLIC SHAPE</small></div><button class="panel-link" data-action="collection-preview">打开完整响应 <svg class="icon sm"><use href="#i-external"/></svg></button></div><p style="font-size:9px;line-height:1.75;color:#8B7C95">网站只读 <span class="mono" style="font-size:8px;color:#624B76">GET /api/public/v1/collections/${esc(collection.slug)}</span>。标题、顺序与摘要来自线上快照。</p><div class="mini-contract">${esc(`{
  "slug": ${JSON.stringify(collection.slug)},
  "title": ${JSON.stringify(collection.title)},
  "items": [
    { "position": 0, "caption": ${JSON.stringify(state.collectionItems[0]?.item?.caption || '')} },
    { "position": 1, "caption": ${JSON.stringify(state.collectionItems[1]?.item?.caption || '')} }
  ]
}`)}</div></section><section class="panel"><div class="panel-head" style="margin-bottom:9px"><div class="panel-title" style="font-size:18px">引用这个集合 <small>CONSUMERS</small></div><span class="pill ${collection.site_id ? 'site' : 'published'}" style="height:21px;font-size:8px"><i></i>${collection.site_id ? '站点集合' : '库级集合'}</span></div><div class="ref-site-row"><span class="mark">${esc((sites[collection.site_id]?.name || '库').slice(0, 1))}</span><span class="grow"><b>${esc(sites[collection.site_id]?.name || '所有站点可读')}</b><span>${collection.site_id ? '仅在归属站点可打开' : '库级集合对任意站点开放'}</span></span><span class="mono">${state.collectionItems.length} items</span></div><div class="upload-note" style="margin-top:12px"><b>改顺序不会立刻影响缓存。</b> 公开响应最多缓存 60 秒；带站点令牌的响应不缓存。</div></section></div></div>`;
  }

  function openNewCollectionModal() {
    const sites = state.sites.map(site => `<option value="${attr(site.id)}">${esc(site.name || site.key)}</option>`).join('');
    openModal({
      kicker: 'Collection / 新建', title: '创建一个新的集合',
      body: `<form id="newCollectionForm" data-form="new-collection"><div class="form-error"></div><div class="field"><label>集合别名 <span class="mini">SLUG</span></label><input class="input mono" name="slug" required maxlength="128" placeholder="home.hero"><div class="field-help">全局唯一，只能使用小写字母、数字、点与连字符。</div></div><div class="field"><label>显示标题 <span class="mini">TITLE</span></label><input class="input" name="title" maxlength="255" placeholder="首页主视觉"></div><div class="field"><label>允许类型 <span class="mini">OPTIONAL</span></label><div class="check-grid">${Object.entries(KINDS).map(([key, meta]) => `<label class="check-row"><input type="checkbox" name="kinds" value="${key}">${esc(meta.label)}</label>`).join('')}</div></div>${isOwner() ? `<div class="field"><label>归属站点 <span class="mini">OPTIONAL</span></label><select class="select" name="site_id"><option value="">库级集合（任意站点）</option>${sites}</select></div>` : ''}</form>`,
      footer: `<span class="hint">集合本身能不能打开看归属；授权前缀只影响内容可见性。</span><button class="btn secondary sm" data-close-modal>取消</button><button class="btn primary sm" data-submit-form="newCollectionForm">创建集合</button>`,
    });
  }

  function openAddCollectionItemModal() {
    if (!state.collection) return;
    const options = state.collectionAssets.map(asset => `<option value="${attr(asset.id)}">${esc(asset.title || '未命名')} · ${esc(KINDS[asset.kind]?.label || asset.kind)}</option>`).join('');
    openModal({
      kicker: 'Collection / 添加条目', title: `添加到 ${state.collection.slug}`,
      body: `<form id="addCollectionItemForm" data-form="collection-add-item"><div class="form-error"></div><div class="field"><label>内容 <span class="mini">ASSET</span></label><select class="select" name="asset_id" required>${options || '<option value="">没有可添加的内容</option>'}</select></div><div class="field"><label>条目说明 <span class="mini">CAPTION</span></label><input class="input" name="caption" maxlength="255" placeholder="例如：秋季主图"></div></form>`,
      footer: `<span class="hint">同一资产在同一集合只出现一次。</span><button class="btn secondary sm" data-close-modal>取消</button><button class="btn primary sm" data-submit-form="addCollectionItemForm" ${options ? '' : 'disabled'}>添加条目</button>`,
    });
  }

  function collectionPreviewModal() {
    if (!state.collection) return;
    const data = {
      slug: state.collection.slug,
      title: state.collection.title,
      items: state.collectionItems.map(({ item, asset }, index) => ({ position: index, caption: item.caption, asset_id: item.asset_id, asset_title: asset?.title || null })),
    };
    openModal({
      kicker: 'Public contract / 公开接口', title: `${state.collection.slug} · 读取结构`, wide: true,
      body: `<p style="font-size:9.5px;color:#8B7C95;margin-bottom:12px;line-height:1.75">网站通过公开 API 读取这个集合。下面的示例来自当前管理视图；真正响应只包含已发布且对该站点可见的条目。</p><pre class="api-preview">${esc(JSON.stringify(data, null, 2))}</pre>`,
      footer: `<span class="hint">GET /api/public/v1/collections/${esc(state.collection.slug)}</span><button class="btn secondary sm" data-close-modal>关闭</button>`,
    });
  }

  async function createCollection(form) {
    const data = new FormData(form);
    const payload = {
      slug: String(data.get('slug') || '').trim(),
      title: String(data.get('title') || '').trim(),
      site_id: data.get('site_id') || null,
      allowed_kinds: data.getAll('kinds'),
    };
    const created = await api('/collections', { method: 'POST', body: payload });
    closeModal();
    toast('集合已创建');
    state.collection = created;
    state.collectionDirty = false;
    await renderCollections();
  }

  async function addCollectionItem(form) {
    if (!state.collection) return;
    const data = formDataObject(form);
    if (!data.asset_id) throw new ApiFailure('没有可添加的内容。', 400);
    await api(`/collections/${encodeURIComponent(state.collection.id)}/items`, { method: 'POST', body: { asset_id: data.asset_id, caption: data.caption || '' } });
    closeModal();
    toast('条目已添加');
    await loadCollectionItems();
    await renderCollections();
  }

  async function moveCollectionItem(index, direction) {
    const target = direction === 'up' ? index - 1 : index + 1;
    if (target < 0 || target >= state.collectionItems.length) return;
    const copy = [...state.collectionItems];
    [copy[index], copy[target]] = [copy[target], copy[index]];
    state.collectionItems = copy;
    state.collectionDirty = true;
    $('#view').innerHTML = collectionsHTML();
    bindImages($('#view'));
  }

  async function saveCollectionOrder() {
    if (!state.collection) return;
    const order = state.collectionItems.map(row => row.item.asset_id);
    await api(`/collections/${encodeURIComponent(state.collection.id)}/order`, { method: 'PUT', body: { asset_ids: order } });
    state.collectionDirty = false;
    toast('集合顺序已保存');
    $('#view').innerHTML = collectionsHTML();
    bindImages($('#view'));
  }


  /* ------------------------------------------------------------
     Sites, grants and tokens
     ------------------------------------------------------------ */
  async function renderSites() {
    const [sites, tokens, users] = await Promise.all([api('/sites'), api('/tokens'), api('/users')]);
    state.sites = sites;
    state.tokens = tokens;
    state.users = users;
    const grantPairs = await Promise.all(sites.map(async site => {
      try { return [site.id, await api(`/sites/${encodeURIComponent(site.id)}/grants`)]; }
      catch { return [site.id, []]; }
    }));
    state.grants = Object.fromEntries(grantPairs);
    $('#view').innerHTML = sitesHTML();
  }

  function sitesHTML() {
    const active = state.sitesTab;
    return `<div class="page-head"><div><div class="eyebrow"><span class="dot"></span>Sites &amp; Tokens<span class="slash">/</span><span class="en">站点与令牌</span></div><h1>一个库，服务多个站点</h1><p class="desc">站点按集合前缀读取内容；令牌只出现一次，库中只保存哈希。</p></div><div class="actions"><button class="btn secondary sm" data-action="new-site"><svg class="icon sm"><use href="#i-plus"/></svg>新建站点</button><button class="btn primary sm" data-action="issue-token"><svg class="icon sm"><use href="#i-shield"/></svg>签发令牌</button></div></div>
      <div class="tabs-line"><button class="${active === 'sites' ? 'active' : ''}" data-action="sites-tab" data-tab="sites">站点 <span class="count">${state.sites.length}</span></button><button class="${active === 'tokens' ? 'active' : ''}" data-action="sites-tab" data-tab="tokens">访问令牌 <span class="count">${state.tokens.length}</span></button><button class="${active === 'grants' ? 'active' : ''}" data-action="sites-tab" data-tab="grants">授权说明 <span class="count">GUIDE</span></button></div>
      ${active === 'sites' ? sitesTabHTML() : active === 'tokens' ? tokensTabHTML() : grantsGuideHTML()}`;
  }

  function sitesTabHTML() {
    return `<div class="site-layout"><div class="site-list">${state.sites.map(site => {
      const grants = state.grants[site.id] || [];
      const count = state.tokens.filter(token => token.subject_type === 'site' && token.subject_id === site.id && !token.revoked).length;
      return `<article class="site-card"><div class="site-mark">${esc((site.name || site.key).slice(0, 1).toUpperCase())}</div><div><div class="site-name">${esc(site.name || site.key)}</div><div class="site-key">${esc(site.key)} · created ${esc(dateFmt(site.created_at, false))}</div><div class="site-desc">${grants.length ? `读取 ${grants.length} 个前缀授权，未列出内容${grants.some(g => g.can_read_unlisted) ? '可' : '不可'}出现在集合里。` : '尚未配置集合授权。'}</div><div class="site-scopes">${grants.map(g => `<span>${esc(g.prefix)}${g.can_read_unlisted ? ' · unlisted' : ''}</span>`).join('') || '<span>NO GRANTS</span>'}</div></div><div class="site-side"><div class="tokens"><b>${count}</b>tokens</div><div class="site-card-actions"><button class="icon-btn bordered" style="width:28px;height:28px" data-action="manage-grants" data-id="${attr(site.id)}" title="管理授权"><svg class="icon sm"><use href="#i-shield"/></svg></button><button class="icon-btn bordered" style="width:28px;height:28px" data-action="issue-token" data-site="${attr(site.key)}" title="签发令牌"><svg class="icon sm"><use href="#i-plus"/></svg></button></div></div></article>`;
    }).join('') || '<div class="empty-state"><div class="empty-icon"><svg class="icon"><use href="#i-globe"/></svg></div><h2>还没有站点</h2><p>网站通过站点令牌和集合授权读取已发布内容。先建一个站点，再签发令牌。</p><div class="empty-actions"><button class="btn primary sm" data-action="new-site">新建站点</button></div></div>'}<div class="upload-note"><b>前缀按点号分段匹配。</b> <span class="mono">home</span> 覆盖 <span class="mono">home.hero</span>，但不覆盖 <span class="mono">homepage</span>。集合能不能打开看归属；授权只决定内容可见性。</div></div><aside><div class="panel aside-panel"><div class="aside-title">令牌健康</div><p>站点令牌只能读取已发布内容。撤销后立即失效；已发布的公开文件不受影响。</p><div class="aside-rule"></div>${state.tokens.filter(t => !t.revoked).slice(0, 5).map(t => `<div class="token-health"><svg class="icon"><use href="#i-shield"/></svg>${esc(t.name || t.subject_type)}<span class="mono">${t.expires_at ? dateFmt(t.expires_at, false) : 'NO EXPIRY'}</span></div>`).join('') || '<div class="inline-note">还没有有效令牌。</div>'}<div class="aside-rule"></div><button class="btn secondary sm" style="width:100%;border-radius:8px" data-action="issue-token"><svg class="icon sm"><use href="#i-plus"/></svg>签发新令牌</button></div><div class="panel" style="margin-top:13px;padding:16px"><div class="section-label" style="margin-bottom:9px">安全边界</div><div class="inline-note">站点令牌只在网站服务器使用，不放进浏览器；管理端会话使用 HttpOnly Cookie 与 CSRF 头。</div></div></aside></div>`;
  }

  function tokensTabHTML() {
    return `<div class="panel" style="padding:19px 20px"><div class="panel-head"><div class="panel-title">已签发令牌 <small>TOKENS · ONLY HASHES ARE STORED</small></div><div class="aside" style="gap:8px"><button class="btn primary xs" data-action="issue-token"><svg class="icon sm"><use href="#i-plus"/></svg>签发令牌</button></div></div><div class="table-wrap"><table class="data-table"><thead><tr><th>名称</th><th>主体</th><th>创建</th><th>过期</th><th>最近使用</th><th>状态</th><th style="text-align:right">操作</th></tr></thead><tbody>${state.tokens.map(token => {
      const subject = token.subject_type === 'site' ? state.sites.find(s => s.id === token.subject_id)?.key || token.subject_id : state.users.find(u => u.id === token.subject_id)?.username || token.subject_id;
      return `<tr><td><div class="strong">${esc(token.name || '未命名令牌')}</div><div class="mono">${esc(token.id.slice(0, 8))}…</div></td><td>${esc(token.subject_type === 'site' ? '站点' : '用户')} · ${esc(subject)}</td><td class="mono">${esc(dateFmt(token.created_at, false))}</td><td class="mono">${token.expires_at ? esc(dateFmt(token.expires_at, false)) : '不自动过期'}</td><td class="mono">${token.last_used_at ? esc(dateFmt(token.last_used_at)) : '尚未使用'}</td><td>${token.revoked ? '<span class="pill private"><i></i>已撤销</span>' : '<span class="pill published"><i></i>有效</span>'}</td><td class="actions">${token.revoked ? '' : `<button class="btn ghost xs" style="color:#8E4650" data-action="revoke-token" data-id="${attr(token.id)}">撤销</button>`}</td></tr>`;
    }).join('') || '<tr><td colspan="7"><div class="inline-note" style="border:0;background:transparent">还没有令牌。</div></td></tr>'}</tbody></table></div><div class="token-empty-note"><b>为什么只出现一次？</b> 明文只在签发响应里返回，数据库只保存 SHA-256。令牌泄露时，撤销并重签是最快的处理方式。</div></div>`;
  }

  function grantsGuideHTML() {
    return `<div class="panel" style="padding:21px 23px;max-width:900px"><div class="panel-title">授权语义速查 <small>GRANTS EXPLAINED</small></div><div class="field-grid" style="gap:20px;margin-top:15px"><div><div class="section-label" style="margin-bottom:7px">COLLECTION ACCESS</div><p style="font-size:9.5px;line-height:1.85;color:#786A82">集合能不能打开，只看 <b>site_id</b>。库级集合对所有站点公开；属于某个站点的集合，只对那个站点开放。前缀授权不参与这个判断。</p><div class="aside-rule"></div><div class="section-label" style="margin-bottom:7px">CONTENT VISIBILITY</div><p style="font-size:9.5px;line-height:1.85;color:#786A82">前缀授权决定两件事：仅站点内容必须出现在已授权集合里；未列出内容要显式允许，才会出现在集合列表中。</p></div><div class="upload-note" style="margin-top:0;background:#F2ECF6;line-height:1.9"><b>示例：</b><br><span class="mono">home</span> → 覆盖 <span class="mono">home.hero</span>、<span class="mono">home.secondary</span><br><span class="mono">home</span> → 不覆盖 <span class="mono">homepage</span><br><br>带站点令牌但令牌无效：返回 <b>401</b>，不降级为匿名；否则「令牌用错」会显示成「内容不存在」。</div></div></div>`;
  }

  function openCreateSiteModal() {
    openModal({
      kicker: 'Site / 新建', title: '创建一个站点',
      body: `<form id="newSiteForm" data-form="new-site"><div class="form-error"></div><div class="field"><label>站点标识 <span class="mini">KEY</span></label><input class="input mono" name="key" required maxlength="64" placeholder="main-site"><div class="field-help">全局唯一，例如 main-site、journal。</div></div><div class="field"><label>显示名称 <span class="mini">NAME</span></label><input class="input" name="name" maxlength="128" placeholder="主站"></div></form>`,
      footer: `<button class="btn secondary sm" data-close-modal>取消</button><button class="btn primary sm" data-submit-form="newSiteForm">创建站点</button>`,
    });
  }

  async function createSite(form) {
    const data = formDataObject(form);
    await api('/sites', { method: 'POST', body: { key: data.key, name: data.name || '' } });
    closeModal();
    toast('站点已创建');
    await renderSites();
  }

  function openManageGrants(siteId) {
    const site = state.sites.find(item => item.id === siteId);
    if (!site) return;
    api(`/sites/${encodeURIComponent(siteId)}/grants`).then(grants => {
      state.grants[site.id] = grants;
      openModal({
        kicker: 'Grants / 站点授权', title: `${site.name || site.key} · 集合前缀`, wide: true,
        body: `<div class="section-label" style="margin-bottom:8px">CURRENT GRANTS</div>${grants.map(grant => `<div class="reference-row" style="border-top:0"><svg class="icon"><use href="#i-shield"/></svg><div><b style="font-size:9.5px;color:#554560">${esc(grant.prefix)}</b><div class="mono">${grant.can_read_unlisted ? 'can_read_unlisted = true' : 'only listed content'}</div></div><span class="mono">${esc(grant.id.slice(0, 8))}</span><button class="btn ghost xs" style="color:#8E4650;margin-left:8px" data-action="grant-remove" data-site="${attr(site.id)}" data-id="${attr(grant.id)}">移除</button></div>`).join('') || '<div class="inline-note">还没有授权。这个站点只能打开库级集合。</div>'}<div class="aside-rule"></div><form id="addGrantForm" data-form="add-grant" data-site="${attr(site.id)}"><div class="form-error"></div><div class="field-grid"><div class="field"><label>前缀 <span class="mini">PREFIX</span></label><input class="input mono" name="prefix" required maxlength="128" placeholder="home"></div><div class="field"><label>未列出内容 <span class="mini">CAN READ UNLISTED</span></label><label class="check-row" style="height:36px"><input type="checkbox" name="can_read_unlisted">允许出现在集合里</label></div></div></form>`,
        footer: `<span class="hint">前缀按点号分段匹配，添加后立即生效。</span><button class="btn secondary sm" data-close-modal>关闭</button><button class="btn primary sm" data-submit-form="addGrantForm">添加授权</button>`,
      });
    }).catch(error => toast(error.message, 'error'));
  }

  async function addGrant(form) {
    const siteId = form.dataset.site;
    const data = new FormData(form);
    await api(`/sites/${encodeURIComponent(siteId)}/grants`, { method: 'POST', body: { prefix: String(data.get('prefix') || '').trim(), can_read_unlisted: data.get('can_read_unlisted') === 'on' } });
    toast('授权已添加');
    state.grants[siteId] = await api(`/sites/${encodeURIComponent(siteId)}/grants`);
    openManageGrants(siteId);
  }

  function openIssueTokenModal(siteKey = '') {
    const siteOptions = state.sites.map(site => `<option value="site:${attr(site.key)}" ${siteKey && site.key === siteKey ? 'selected' : ''}>站点 · ${esc(site.name || site.key)}</option>`).join('');
    const userOptions = state.users.map(user => `<option value="user:${attr(user.username)}">用户 · ${esc(user.username)}</option>`).join('');
    openModal({
      kicker: 'Token / 签发', title: '签发访问令牌',
      body: `<form id="issueTokenForm" data-form="issue-token"><div class="form-error"></div><div class="field"><label>签发对象 <span class="mini">SUBJECT</span></label><select class="select" name="subject">${siteOptions}${userOptions}</select></div><div class="field-grid"><div class="field"><label>令牌名称 <span class="mini">NAME</span></label><input class="input" name="name" maxlength="64" placeholder="production"></div><div class="field"><label>有效期 <span class="mini">EXPIRES</span></label><select class="select" name="expires_days"><option value="30">30 天</option><option value="90" selected>90 天</option><option value="365">1 年</option><option value="0">不自动过期</option></select></div></div><div class="field"><label>允许来源 IP <span class="mini">OPTIONAL</span></label><input class="input mono" name="allowed_ip" maxlength="128" placeholder="10.0.0.12，留空不限制"></div><div class="upload-note"><b>令牌明文只出现一次。</b> 库中只保存 SHA-256；站点令牌只能读取已授权内容，不能上传。</div></form>`,
      footer: `<span class="hint">签发动作会写入审计记录。</span><button class="btn secondary sm" data-close-modal>取消</button><button class="btn primary sm" data-submit-form="issueTokenForm">签发令牌</button>`,
    });
  }

  async function issueToken(form) {
    const data = formDataObject(form);
    const [subjectType, ...rest] = String(data.subject || '').split(':');
    const subjectKey = rest.join(':');
    if (!subjectType || !subjectKey) throw new ApiFailure('请选择签发对象。', 400);
    const days = Number(data.expires_days || 0);
    const expires = days > 0 ? new Date(Date.now() + days * 86400000).toISOString() : null;
    const issued = await api('/tokens', { method: 'POST', body: { name: data.name || '', subject_type: subjectType, subject_key: subjectKey, expires_at: expires, allowed_ip: data.allowed_ip || null } });
    state.tokens = await api('/tokens');
    openModal({
      kicker: 'Token / 仅显示一次', title: '令牌已签发',
      body: `<div class="token-once"><div class="token-label">请立即复制并放入网站服务器的环境变量；关闭后不可再次查看。</div><code id="issuedToken">${esc(issued.token)}</code><div class="copy-line"><button class="btn lav xs" data-action="copy" data-value="${attr(issued.token)}">复制完整令牌</button><span class="field-help">${esc(issued.subject_type === 'site' ? '站点' : '用户')} · ${esc(issued.name || '未命名')}</span></div></div><div class="upload-note" style="margin-top:13px"><b>只读边界：</b>站点令牌不能上传、修改或发布内容。撤销后立即失效。</div>`,
      footer: `<button class="btn primary sm" data-close-modal>我已保存</button>`,
    });
  }

  function confirmRevokeToken(id) {
    const token = state.tokens.find(item => item.id === id);
    if (!token) return;
    confirmDialog({
      kicker: 'Revoke / 撤销令牌', title: '撤销这个令牌？', body: `<b>${esc(token.name || token.id)}</b> 会立即失效。站点正在读取的公开内容会在下一次请求时收到 401；已发布公开文件不受影响。`, confirmLabel: '确认撤销',
      onConfirm: async () => { await api(`/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' }); toast('令牌已撤销'); await renderSites(); },
    });
  }

  /* ------------------------------------------------------------
     Users and permissions
     ------------------------------------------------------------ */
  async function renderUsers() {
    state.users = await api('/users');
    if (!state.selectedUser || !state.users.some(user => user.id === state.selectedUser.id)) {
      state.selectedUser = state.users.find(user => user.role !== 'owner') || state.users[0] || null;
    }
    if (state.selectedUser) state.selectedUser = state.users.find(user => user.id === state.selectedUser.id) || state.selectedUser;
    $('#view').innerHTML = usersHTML();
  }

  function usersHTML() {
    const selected = state.selectedUser;
    return `<div class="page-head"><div><div class="eyebrow"><span class="dot"></span>Accounts<span class="slash">/</span><span class="en">账号与权限</span></div><h1>谁可以碰这些内容</h1><p class="desc">唯一所有者不读权限表；编辑的八项能力由所有者逐项授予或收回。</p></div><div class="actions"><button class="btn primary sm" data-action="new-editor"><svg class="icon sm"><use href="#i-plus"/></svg>新建编辑</button></div></div>
      <div class="users-layout"><div class="user-list-panel"><div class="user-list-head"><span>${state.users.length} ACCOUNTS</span><span>ROLE / STATUS</span></div>${state.users.map(user => `<button class="user-row ${selected?.id === user.id ? 'active' : ''}" data-action="select-user" data-id="${attr(user.id)}"><div class="avatar">${esc(user.username.slice(0, 1).toUpperCase())}</div><div class="info"><div class="name">${esc(user.username)} <span class="role-label">${user.role === 'owner' ? 'OWNER' : 'EDITOR'}</span></div><div class="role">${user.role === 'owner' ? '全部权限' : `${user.permissions.length} / 8 项能力`}</div></div><div class="last">${user.disabled ? '已停用' : '正常'}</div></button>`).join('')}<div class="filter-rule" style="margin:0;padding:12px 14px">编辑可以看见全库草稿；第一期不按目录拆开可见性。</div></div>
      ${selected ? userDetailHTML(selected) : '<div class="panel"><div class="empty-state" style="border:0"><h2>还没有账号</h2></div></div>'}
      </div>`;
  }

  function userDetailHTML(user) {
    if (user.role === 'owner') {
      return `<div class="permission-panel"><div class="permission-head"><div><div class="eyebrow"><span class="dot"></span>Owner profile</div><h2>${esc(user.username)}</h2><div class="sub">唯一所有者 · 拥有全部能力</div></div><span class="pill published"><i></i>账号正常</span></div><div class="permission-body"><div class="permission-banner"><svg class="icon"><use href="#i-shield"/></svg><div><b>所有者不受权限表限制</b><span>不能创建第二个所有者，也不能删除或降级这一个账号。</span></div></div><div class="owner-note">账号管理、站点令牌、彻底删除、配额、备份与 fsck 只属于所有者。所有者也不接受整体替换权限。</div></div></div>`;
    }
    return `<div class="permission-panel"><div class="permission-head"><div><div class="eyebrow"><span class="dot"></span>Editor profile</div><h2>${esc(user.username)}</h2><div class="sub">${user.disabled ? '账号已停用' : '编辑账号'} · 创建于 ${esc(dateFmt(user.created_at, false))}</div></div><span class="pill ${user.disabled ? 'private' : 'published'}"><i></i>${user.disabled ? '已停用' : '账号正常'}</span></div><div class="permission-body"><div class="permission-banner"><svg class="icon"><use href="#i-shield"/></svg><div><b>权限只影响管理操作</b><span>没有发布权限的编辑可以替换文件，但不会改变线上那一版。</span></div></div><div class="permission-grid" id="permissionGrid">${PERMISSIONS.map(([key, label]) => `<div class="perm-row"><div class="perm-text"><b>${esc(label)}</b><span>${esc(key)}</span></div><button class="switch ${user.permissions.includes(key) ? 'on' : ''}" role="switch" aria-checked="${user.permissions.includes(key)}" data-action="perm-toggle" data-perm="${key}" aria-label="${esc(label)}"></button></div>`).join('')}</div><div class="permission-foot"><span class="count">已授予 <b id="permCount">${user.permissions.length}</b> / 8 项能力 · 变更立即生效</span><div style="display:flex;gap:6px"><button class="btn secondary sm" data-action="reset-password">重置密码</button><button class="btn secondary sm" data-action="disable-user" ${user.disabled ? 'disabled' : ''}>${user.disabled ? '已停用' : '停用账号'}</button><button class="btn primary sm" data-action="save-permissions">保存权限</button></div></div><div class="owner-note" style="margin-top:14px"><b>账号恢复：</b>停用后会话与令牌立即失效；第一期不支持重新启用，需要重建账号。</div></div></div>`;
  }

  function openNewEditorModal() {
    openModal({
      kicker: 'Account / 新建编辑', title: '创建一个编辑账号',
      body: `<form id="newEditorForm" data-form="new-editor"><div class="form-error"></div><div class="field-help" style="margin-bottom:9px">密码至少 12 个字符；登录失败按账号 + IP 限速。</div><div class="field-grid"><div class="field"><label>用户名 <span class="mini">USERNAME</span></label><input class="input" name="username" required maxlength="64" autocomplete="off"></div><div class="field"><label>初始密码 <span class="mini">PASSWORD</span></label><input class="input" name="password" type="password" required minlength="12" autocomplete="new-password"></div></div><div class="field"><label>预设权限 <span class="mini">PERMISSIONS</span></label><div class="check-grid">${PERMISSIONS.map(([key, label]) => `<label class="check-row"><input type="checkbox" name="permissions" value="${key}">${esc(label)}</label>`).join('')}</div><div class="field-help">不勾选也能登录，但看不到管理内容。所有者可随时收回单项能力。</div></div></form>`,
      footer: `<button class="btn secondary sm" data-close-modal>取消</button><button class="btn primary sm" data-submit-form="newEditorForm">创建账号</button>`,
    });
  }

  async function createEditor(form) {
    const data = new FormData(form);
    await api('/users', { method: 'POST', body: { username: String(data.get('username') || '').trim(), password: String(data.get('password') || ''), permissions: data.getAll('permissions') } });
    closeModal();
    toast('编辑账号已创建');
    await renderUsers();
  }

  function togglePermission(button) {
    const on = button.classList.toggle('on');
    button.setAttribute('aria-checked', on ? 'true' : 'false');
    const count = $$('#permissionGrid .switch.on').length;
    const el = $('#permCount'); if (el) el.textContent = count;
  }

  async function savePermissions() {
    if (!state.selectedUser) return;
    const permissions = $$('#permissionGrid .switch.on').map(el => el.dataset.perm);
    const updated = await api(`/users/${encodeURIComponent(state.selectedUser.id)}/permissions`, { method: 'PUT', body: { permissions } });
    state.selectedUser = updated;
    toast('权限已保存');
    await renderUsers();
  }

  function openResetPasswordModal() {
    if (!state.selectedUser) return;
    openModal({
      kicker: 'Password / 重置', title: `重置 ${state.selectedUser.username} 的密码`,
      body: `<form id="resetPasswordForm" data-form="reset-password"><div class="form-error"></div><div class="field"><label>新密码 <span class="mini">NEW PASSWORD</span></label><input class="input" type="password" name="password" required minlength="12" autocomplete="new-password"></div><div class="upload-note">重置密码会让这个账号已有的会话与令牌立即失效。</div></form>`,
      footer: `<button class="btn secondary sm" data-close-modal>取消</button><button class="btn primary sm" data-submit-form="resetPasswordForm">重置密码</button>`,
    });
  }

  async function resetPassword(form) {
    if (!state.selectedUser) return;
    const data = formDataObject(form);
    await api(`/users/${encodeURIComponent(state.selectedUser.id)}`, { method: 'PATCH', body: { password: data.password } });
    closeModal();
    toast('密码已重置 · 旧会话已失效');
  }

  function disableUser() {
    if (!state.selectedUser) return;
    const user = state.selectedUser;
    confirmDialog({
      kicker: 'Disable / 停用账号', title: `停用 ${user.username}？`, body: '该账号的会话与令牌会立即失效；内容仍归该账号所有。第一期不支持重新启用。', confirmLabel: '停用账号',
      onConfirm: async () => { await api(`/users/${encodeURIComponent(user.id)}`, { method: 'PATCH', body: { disabled: true } }); toast('账号已停用'); await renderUsers(); },
    });
  }


  /* ------------------------------------------------------------
     Trash
     ------------------------------------------------------------ */
  async function renderTrash() {
    const data = await api('/assets?limit=100&status=trashed');
    state.trash = data.items;
    $('#view').innerHTML = trashHTML();
    bindImages($('#view'));
    const badge = $('#trashBadge'); badge.textContent = data.items.length; badge.hidden = !data.items.length;
  }

  function trashHTML() {
    return `<div class="page-head"><div><div class="eyebrow"><span class="dot"></span>Trash<span class="slash">/</span><span class="en">回收站</span></div><h1>暂存，但不遗忘</h1><p class="desc">删除先进入回收站，默认保留 30 天；恢复后不会自动重新公开。</p></div><div class="actions">${isOwner() ? '<button class="btn secondary sm" data-action="trash-clear" style="color:#8E4650;border-color:#E5CFD1"><svg class="icon sm"><use href="#i-trash"/></svg>清空回收站</button>' : ''}</div></div>
      <div class="trash-notice"><svg class="icon"><use href="#i-clock"/></svg><span class="grow">回收站中的内容仍占用磁盘；到期由 worker 自动彻底删除。仍被封面或集合引用的内容会被跳过。</span><span class="mono">RETENTION 30 DAYS</span></div>
      ${state.trash.length ? `<div class="panel" style="padding:7px 19px 0"><div class="table-wrap" style="border:0"><table class="data-table"><thead><tr><th>内容</th><th>进入时间</th><th>类型</th><th>大小</th><th>状态</th><th style="text-align:right">操作</th></tr></thead><tbody>${state.trash.map(asset => {
        const version = asset.current_version;
        return `<tr><td><div class="trash-item"><span class="trash-thumb type-art" data-kind="${attr(asset.kind)}"><span class="type-mark">${esc(KINDS[asset.kind]?.code || '—')}</span></span><div><div class="trash-name">${esc(asset.title || '未命名内容')}</div><div class="trash-sub">${esc(version?.original_filename || '无版本')}</div></div></div></td><td class="mono">${esc(dateFmt(asset.trashed_at || asset.updated_at))}</td><td>${esc(KINDS[asset.kind]?.label || asset.kind)}</td><td class="mono">${bytes(version?.byte_size)}</td><td>${starPill(asset.status, asset.visibility)}</td><td><div class="trash-actions"><button class="btn ghost xs" data-action="trash-restore" data-id="${attr(asset.id)}"><svg class="icon sm"><use href="#i-rotate"/></svg>恢复</button>${isOwner() ? `<button class="btn ghost xs" style="color:#8E5158" data-action="trash-purge" data-id="${attr(asset.id)}">彻底删除</button>` : ''}</div></td></tr>`;
      }).join('')}</tbody></table></div></div><div class="trash-lower"><section class="panel"><div class="panel-head" style="margin-bottom:9px"><div class="panel-title" style="font-size:18px">保留与清理 <small>RETENTION WINDOW</small></div><span class="mono" style="font-size:7px;color:#A093A8">30 DAYS</span></div><p style="font-size:9px;line-height:1.75;color:#8B7C95">内容在回收站中仍计入磁盘占用。到期后由维护任务彻底删除；仍被封面或集合引用时会跳过。</p><div class="retention-track"><span class="keep"></span><span class="expire"></span></div><div class="retention-labels"><span>今天</span><span>自动清理窗口</span></div><div class="free-space"><div class="big">${state.trash.length}<span style="font-family:var(--mono);font-size:8px;color:#9A8CA2"> 条</span></div><span>当前回收站内容<br>恢复不会自动重新公开</span></div></section><section class="panel"><div class="panel-head" style="margin-bottom:9px"><div class="panel-title" style="font-size:18px">删除前会检查 <small>REFERENCE GUARD</small></div><svg class="icon" style="color:#8D72A0"><use href="#i-shield"/></svg></div><div class="token-health"><svg class="icon"><use href="#i-check"/></svg>没有被用作封面<span class="mono">SERVER</span></div><div class="token-health"><svg class="icon"><use href="#i-check"/></svg>没有被集合包含<span class="mono">SERVER</span></div><div class="token-health"><svg class="icon"><use href="#i-check"/></svg>原件与派生文件可删除<span class="mono">SERVER</span></div><div class="upload-note" style="margin-top:11px"><b>彻底删除不可撤销。</b> 审计记录会保留动作、操作者与时间，但不保留文件字节。</div></section></div>` : `<div class="empty-state"><div class="empty-icon"><svg class="icon"><use href="#i-trash"/></svg></div><h2>回收站是空的</h2><p>被删除的内容会先留在这里，30 天内可恢复或由所有者彻底删除。</p><div class="empty-actions"><button class="btn secondary sm" data-route="library">回到内容库</button></div></div>`}`;
  }

  function confirmRestore(id) {
    const asset = state.trash.find(item => item.id === id);
    if (!asset) return;
    confirmDialog({
      kicker: 'Restore / 恢复', title: '从回收站恢复？', body: '恢复后内容回到内容库，但不会自动重新公开；需要再次发布才会出现在公开区。', confirmLabel: '恢复',
      onConfirm: async () => { await api(`/assets/${encodeURIComponent(id)}/restore`, { method: 'POST', body: { revision: asset.revision } }); toast('已恢复'); await renderTrash(); },
    });
  }

  function confirmPurgeAsset(id) {
    const asset = state.trash.find(item => item.id === id);
    if (!asset) return;
    confirmDialog({
      kicker: 'Permanent delete / 彻底删除', title: '彻底删除这条内容？', body: `原件、全部版本、派生文件与公开链接都会永久删除。仍被封面或集合引用时服务端会拒绝。`, confirmLabel: '彻底删除',
      onConfirm: async () => { await api(`/assets/${encodeURIComponent(id)}`, { method: 'DELETE' }); toast('内容已彻底删除'); await renderTrash(); },
    });
  }

  function clearTrash() {
    if (!state.trash.length) return;
    confirmDialog({
      kicker: 'Destructive / 清空回收站', title: `彻底删除全部 ${state.trash.length} 条内容？`, body: '这会永久删除回收站中的原件与版本，只保留审计记录。仍被引用的内容会被跳过。', confirmLabel: '清空回收站',
      onConfirm: async () => {
        for (const asset of state.trash) {
          try { await api(`/assets/${encodeURIComponent(asset.id)}`, { method: 'DELETE' }); } catch { /* keep going */ }
        }
        toast('回收站已处理');
        await renderTrash();
      },
    });
  }

  /* ------------------------------------------------------------
     System status
     ------------------------------------------------------------ */
  async function renderSystem() {
    const [health, jobs, audit] = await Promise.all([api('/health'), api('/jobs?limit=12'), api('/audit?limit=60')]);
    state.health = health;
    state.jobs = jobs;
    state.audit = audit;
    $('#view').innerHTML = systemHTML();
  }

  function systemHTML() {
    const health = state.health || {};
    const disk = health.disk || {};
    const free = disk.free_bytes;
    const total = disk.total_bytes;
    const used = total && free !== null ? total - free : null;
    const percent = total ? Math.max(0, Math.min(100, Math.round((free / total) * 100))) : 0;
    const jobs = health.jobs || {};
    const queue = Number(jobs.pending || 0) + Number(jobs.running || 0);
    const backup = health.last_backup;
    return `<div class="page-head"><div><div class="eyebrow"><span class="dot"></span>System<span class="slash">/</span><span class="en">系统状态</span></div><h1>安静地运行</h1><p class="desc">磁盘、内存、队列与备份；发生降级时，这里会先于网站察觉。</p></div><div class="actions"><button class="btn secondary sm" data-action="system-refresh"><svg class="icon sm"><use href="#i-refresh"/></svg>刷新</button><button class="btn secondary sm" data-action="ops-command"><svg class="icon sm"><use href="#i-command"/></svg>运维命令</button></div></div>
      <div class="health-hero"><div class="health-orb" style="background:${health.status === 'ok' ? '#E8F9F1' : '#FBF0F2'};color:${health.status === 'ok' ? '#237452' : '#8E4650'};border-color:${health.status === 'ok' ? '#CEEEE0' : '#E7C8CE'}"><svg class="icon lg"><use href="#${health.status === 'ok' ? 'i-check' : 'i-alert'}"/></svg></div><div class="health-copy"><b>${health.status === 'ok' ? '所有核心服务运行正常' : '服务处于降级状态'}</b><span>${health.status === 'ok' ? 'API · WORKER · SQLITE · PUBLIC LINKS' : esc((health.reasons || []).join(' · '))}</span></div><div class="actions"><span class="pill ${health.status === 'ok' ? 'published' : 'private'}" style="height:26px"><i></i>${health.status === 'ok' ? '健康' : '降级'}</span></div></div>
      <div class="stat-grid"><div class="stat-card"><div class="label"><span>磁盘 / DISK</span><svg class="icon sm"><use href="#i-database"/></svg></div><div class="value">${bytes(free)} <small>可用</small></div><div class="foot">${used !== null ? `已用 ${bytes(used)} / ${bytes(total)}` : '无法读取磁盘'}</div><div class="spark-bar"><span style="width:${percent}%"></span></div></div><div class="stat-card"><div class="label"><span>队列 / QUEUE</span><svg class="icon sm"><use href="#i-activity"/></svg></div><div class="value">${queue} <small>待处理</small></div><div class="foot">失败 ${num(jobs.failed || 0)} · 运行中 ${num(jobs.running || 0)}</div><div class="spark-bar"><span style="width:${Math.min(100, queue * 10)}%"></span></div></div><div class="stat-card"><div class="label"><span>版本 / VERSION</span><svg class="icon sm"><use href="#i-server"/></svg></div><div class="value" style="font-size:18px">${esc(health.app_version || '—')} <small>schema ${esc(health.schema_version || '—')}</small></div><div class="foot">SQLite · local disk</div><div class="spark-bar"><span style="width:100%"></span></div></div><div class="stat-card"><div class="label"><span>备份 / BACKUP</span><svg class="icon sm"><use href="#i-clock"/></svg></div><div class="value" style="font-size:18px">${backup ? esc(backup.backup) : '尚无备份'}</div><div class="foot">${backup ? `${esc(backup.completed_at || '')}` : '按运维手册执行停写备份'}</div><div class="spark-bar"><span style="width:${backup ? 100 : 0}%"></span></div></div></div>
      <div class="tabs-line"><button class="${state.systemTab === 'health' ? 'active' : ''}" data-action="system-tab" data-tab="health">运行状态 <span class="count">LIVE</span></button><button class="${state.systemTab === 'audit' ? 'active' : ''}" data-action="system-tab" data-tab="audit">审计日志 <span class="count">${state.audit.length}</span></button></div>
      ${state.systemTab === 'audit' ? auditHTML() : healthTabHTML(health, disk, percent)}
    `;
  }

  function healthTabHTML(health, disk, percent) {
    return `<div class="system-grid"><div class="panel"><div class="panel-head"><div class="panel-title">后台任务 <small>JOB QUEUE · SINGLE WORKER</small></div><button class="panel-link" data-action="system-refresh"><svg class="icon sm"><use href="#i-refresh"/></svg>刷新</button></div><div class="table-wrap" style="border:0"><table class="data-table" style="min-width:560px"><thead><tr><th>任务</th><th>状态</th><th>尝试</th><th>创建</th><th>最近错误</th></tr></thead><tbody>${state.jobs.length ? state.jobs.map(job => `<tr><td class="mono">${esc(job.kind)}<div class="mono">${esc(job.id.slice(0, 8))}…</div></td><td>${(() => { const [label, cls] = JOB_STATUS[job.status] || [job.status, 'draft']; return `<span class="pill ${cls}" style="height:19px;font-size:7.5px"><i></i>${esc(label)}</span>`; })()}</td><td class="mono">${job.attempts} / 3</td><td class="mono">${esc(dateFmt(job.created_at))}</td><td class="mono" title="${attr(job.last_error || '')}">${esc(job.last_error || '—')}</td></tr>`).join('') : '<tr><td colspan="5"><div class="inline-note" style="border:0;background:transparent">队列是空的。</div></td></tr>'}</tbody></table></div><div class="upload-note" style="margin-top:12px"><b>低内存保护。</b> 可用内存低于阈值时，解码类任务保持 pending，定时发布与清理继续执行。</div></div><div><div class="panel"><div class="panel-head"><div class="panel-title">磁盘 <small>DATA FILESYSTEM</small></div><span class="panel-link">${esc(disk.data_dir || '—')}</span></div><div class="disk-detail"><div class="disk-number">${bytes(disk.free_bytes)} <small>可用 / ${bytes(disk.total_bytes)}</small></div><div class="disk-legend"><span><i></i>可用 ${percent}%</span></div></div><div class="disk-bar"><span class="used" style="width:${100 - percent}%"></span><span class="other" style="width:${percent}%"></span></div><div class="disk-caption"><span>内容目录所在文件系统</span><span>低于阈值时拒绝新上传</span></div><div class="aside-rule"></div><div class="token-health"><svg class="icon"><use href="#i-check"/></svg>健康状态<span class="mono">${esc(health.status)}</span></div><div class="token-health"><svg class="icon"><use href="#i-check"/></svg>迁移版本<span class="mono">${esc(health.schema_version || '—')}</span></div><div class="token-health"><svg class="icon"><use href="#i-check"/></svg>上次备份<span class="mono">${health.last_backup ? esc(health.last_backup.backup) : '—'}</span></div></div><div class="panel" style="margin-top:14px"><div class="panel-head" style="margin-bottom:8px"><div class="panel-title" style="font-size:17px">维护动作 <small>SERVER-SIDE</small></div><span class="pill draft">CLI</span></div><p style="font-size:9px;line-height:1.8;color:#897A94">fsck、停写备份与 restore 都需要在服务器维护窗口执行；管理端只读展示状态，避免误触。</p><div class="wide-actions"><button class="btn secondary xs" data-action="ops-command">查看命令</button></div></div></div></div>`;
  }

  function auditHTML() {
    return `<div class="panel" style="padding:19px 20px"><div class="panel-head"><div class="panel-title">审计日志 <small>AUDIT LOG · RECENT ${state.audit.length}</small></div><span class="mono" style="font-size:7.5px;color:#9A90A5">READ ONLY</span></div><div class="table-wrap" style="border:0"><table class="data-table" style="min-width:680px"><thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>目标</th><th>来源</th></tr></thead><tbody>${state.audit.length ? state.audit.map(entry => `<tr><td class="mono">${esc(dateFmt(entry.created_at))}</td><td><div class="actor"><span class="audit-dot upload"></span>${esc(entry.actor_name || entry.actor_type)}</div></td><td>${esc(AUDIT_LABELS[entry.action] || entry.action)}</td><td class="mono">${esc(entry.target_type || '—')} ${entry.target_id ? esc(entry.target_id.slice(0, 8)) + '…' : ''}</td><td class="mono">${esc(entry.detail?.ip || entry.detail?.username || '—')}</td></tr>`).join('') : '<tr><td colspan="5"><div class="inline-note" style="border:0;background:transparent">还没有审计记录。</div></td></tr>'}</tbody></table></div><div class="token-empty-note">审计只保留操作者、动作、目标与时间；访问上报不进入审计日志。</div></div>`;
  }

  function openOpsCommands() {
    openModal({
      kicker: 'Operations / 运维命令', title: '在服务器维护窗口执行',
      body: `<div class="section-label" style="margin-bottom:8px">CONSISTENCY</div><code class="command">ctrlstore fsck
ctrlstore fsck --fix-public-links</code><div class="section-label" style="margin:15px 0 8px">STOP-THE-WORLD BACKUP</div><code class="command">sudo CTRLSTORE_DATA_DIR=/var/lib/ctrlstore \\
  CTRLSTORE_BACKUP_DIR=/var/backups/ctrlstore \\
  scripts/backup.sh</code><div class="upload-note" style="margin-top:13px"><b>管理端不执行这两类动作。</b> 备份会暂停 API 与 worker；fsck 的一致性修复需要明确判断，仍由服务器命令行完成。</div>`,
      footer: `<span class="hint">命令与 README 运维章节一致。</span><button class="btn primary sm" data-close-modal>关闭</button>`,
    });
  }

  /* ------------------------------------------------------------
     Event delegation
     ------------------------------------------------------------ */
  document.addEventListener('click', async (event) => {
    const routeEl = event.target.closest('[data-route]');
    if (routeEl && !routeEl.disabled) {
      event.preventDefault();
      closeSearch();
      navigate(routeEl.dataset.route);
      return;
    }
    if (event.target.closest('[data-close-modal]')) { closeModal(); return; }
    const actionEl = event.target.closest('[data-action]');
    if (!actionEl || actionEl.disabled) return;
    try {
      switch (actionEl.dataset.action) {
        case 'open-search': openSearch(); break;
        case 'new-text': openNewTextModal(); break;
        case 'new-folder': openNewFolderModal(); break;
        case 'lib-kind': state.library.kind = actionEl.dataset.kind || ''; await reloadLibrary(); break;
        case 'lib-folder': state.library.folder_id = actionEl.dataset.id || ''; await reloadLibrary(); break;
        case 'lib-tag': state.library.tag = state.library.tag === actionEl.dataset.slug ? '' : actionEl.dataset.slug; await reloadLibrary(); break;
        case 'lib-clear': Object.assign(state.library, { kind: '', folder_id: '', status: '', tag: '', q: '' }); await reloadLibrary(); break;
        case 'lib-more': await loadLibrary(false); $('#view').innerHTML = libraryHTML(); bindImages($('#view')); break;
        case 'dash-prev': await dashboardDay('', -1); break;
        case 'dash-next': await dashboardDay('', 1); break;
        case 'dash-day': await dashboardDay(actionEl.dataset.day); break;
        case 'dash-today': {
          const today = todayShanghai();
          if (today.startsWith(state.dashboardMonth)) await dashboardDay(today);
          else { state.dashboardMonth = today.slice(0, 7); await renderDashboard(); }
          break;
        }
        case 'asset-tab': {
          const tab = actionEl.dataset.tab;
          $$('#assetTabs .tab').forEach(el => el.classList.toggle('active', el.dataset.tab === tab));
          $$('[data-asset-panel]').forEach(el => el.classList.toggle('active', el.dataset.assetPanel === tab));
          break;
        }
        case 'publish-asset': openPublishModal(); break;
        case 'unpublish-asset': confirmAssetAction('unpublish'); break;
        case 'archive-asset': confirmAssetAction('archive'); break;
        case 'trash-asset': confirmAssetAction('trash'); break;
        case 'restore-asset': confirmAssetAction('restore'); break;
        case 'purge-asset': confirmPurge(); break;
        case 'copy': copyText(actionEl.dataset.value || ''); toast('已复制'); break;
        case 'tag-remove': state.assetTags = state.assetTags.filter(tag => tag !== actionEl.dataset.tag); renderTagEditor(); break;
        case 'editor-save': await saveEditor(); break;
        case 'editor-preview': $('#mdPreview')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); toast('预览位于右侧'); break;
        case 'insert-md': insertMarkdown(actionEl); break;
        case 'upload-pick': $('#fileInput')?.click(); break;
        case 'upload-retry': {
          const item = state.uploadQueue.find(row => row.id === actionEl.dataset.id);
          if (item?.status === 'error' && item.retryable) { item.status = 'queued'; item.error = null; renderUploadQueue(); processUploadQueue(); }
          break;
        }
        case 'upload-remove': {
          const item = state.uploadQueue.find(row => row.id === actionEl.dataset.id);
          if (!item || !['done', 'error'].includes(item.status)) break;
          if (item.uploadSession?.id) await api(`/uploads/${encodeURIComponent(item.uploadSession.id)}`, { method: 'DELETE' });
          state.uploadQueue = state.uploadQueue.filter(row => row !== item);
          renderUploadQueue();
          break;
        }
        case 'upload-clear': state.uploadQueue = state.uploadQueue.filter(row => row.status !== 'done'); renderUploadQueue(); break;
        case 'new-collection': openNewCollectionModal(); break;
        case 'collection-select': state.collection = state.collections.find(item => item.id === actionEl.dataset.id) || state.collection; await loadCollectionItems(); $('#view').innerHTML = collectionsHTML(); bindImages($('#view')); break;
        case 'collection-add-item': openAddCollectionItemModal(); break;
        case 'collection-preview': collectionPreviewModal(); break;
        case 'collection-move': await moveCollectionItem(Number(actionEl.dataset.index), actionEl.dataset.dir); break;
        case 'collection-save-order': await saveCollectionOrder(); break;
        case 'collection-remove': if (state.collection) { const assetId = actionEl.dataset.id; confirmDialog({ kicker: 'Collection / 移除', title: '从集合移除此条目？', body: '只解除集合引用，不删除内容本身。', confirmLabel: '移除', onConfirm: async () => { await api(`/collections/${encodeURIComponent(state.collection.id)}/items/${encodeURIComponent(assetId)}`, { method: 'DELETE' }); toast('条目已移除'); await loadCollectionItems(); await renderCollections(); } }); } break;
        case 'new-site': openCreateSiteModal(); break;
        case 'sites-tab': state.sitesTab = actionEl.dataset.tab; $('#view').innerHTML = sitesHTML(); break;
        case 'manage-grants': openManageGrants(actionEl.dataset.id); break;
        case 'grant-remove': { const siteId = actionEl.dataset.site; await api(`/sites/${encodeURIComponent(siteId)}/grants/${encodeURIComponent(actionEl.dataset.id)}`, { method: 'DELETE' }); toast('授权已移除'); state.grants[siteId] = await api(`/sites/${encodeURIComponent(siteId)}/grants`); openManageGrants(siteId); break; }
        case 'issue-token': openIssueTokenModal(actionEl.dataset.site || ''); break;
        case 'revoke-token': confirmRevokeToken(actionEl.dataset.id); break;
        case 'new-editor': openNewEditorModal(); break;
        case 'select-user': state.selectedUser = state.users.find(user => user.id === actionEl.dataset.id) || state.selectedUser; $('#view').innerHTML = usersHTML(); break;
        case 'perm-toggle': togglePermission(actionEl); break;
        case 'save-permissions': await savePermissions(); break;
        case 'reset-password': openResetPasswordModal(); break;
        case 'disable-user': disableUser(); break;
        case 'trash-restore': confirmRestore(actionEl.dataset.id); break;
        case 'trash-purge': confirmPurgeAsset(actionEl.dataset.id); break;
        case 'trash-clear': clearTrash(); break;
        case 'system-tab': state.systemTab = actionEl.dataset.tab; $('#view').innerHTML = systemHTML(); break;
        case 'system-refresh': await renderSystem(); break;
        case 'ops-command': openOpsCommands(); break;
        default: break;
      }
    } catch (error) {
      if (error.status !== 401) toast(error.message, 'error');
    }
  });

  document.addEventListener('change', async (event) => {
    const target = event.target;
    if (target.matches('[data-onchange="lib-status"]')) { state.library.status = target.value; await reloadLibrary(); }
    if (target.id === 'fileInput') {
      const files = [...target.files];
      if (files.length) addUploadFiles(files);
      target.value = '';
    }
    if (target.id === 'replaceFile' && state.asset) {
      const file = target.files?.[0];
      target.value = '';
      if (file) {
        try { await uploadReplacement(file, state.asset.id); }
        catch (error) { if (error.status !== 401) toast(error.message, 'error'); }
      }
    }
  });

  document.addEventListener('submit', async (event) => {
    const form = event.target.closest('form[data-form]');
    if (!form) return;
    event.preventDefault();
    const kind = form.dataset.form;
    try {
      if (kind === 'library-search') { state.library.q = new FormData(form).get('q')?.trim() || ''; await reloadLibrary(); }
      else if (kind === 'new-text') await createTextFromForm(form);
      else if (kind === 'asset-update') await saveAsset(form);
      else if (kind === 'publish-asset') await submitPublish(form);
      else if (kind === 'new-collection') await createCollection(form);
      else if (kind === 'collection-add-item') await addCollectionItem(form);
      else if (kind === 'new-site') await createSite(form);
      else if (kind === 'add-grant') await addGrant(form);
      else if (kind === 'issue-token') await issueToken(form);
      else if (kind === 'new-editor') await createEditor(form);
      else if (kind === 'reset-password') await resetPassword(form);
      else if (kind === 'new-folder') {
        const data = formDataObject(form);
        await api('/folders', { method: 'POST', body: { name: data.name, parent_id: data.parent_id || null } });
        closeModal(); toast('目录已创建'); state.library.folders = await api('/folders'); await reloadLibrary();
      }
    } catch (error) {
      if (error.status === 401) return;
      const box = form.querySelector('.form-error');
      if (box) { box.textContent = error.message; box.classList.add('show'); }
      else toast(error.message, 'error');
    }
  });

  document.addEventListener('input', (event) => {
    if (event.target.id === 'searchInput') {
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(() => updateSearchResults(event.target.value), 220);
    }
    if (event.target.id === 'mdEditor') updateEditorPreview();
  });

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); openSearch(); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && state.route === 'editor') { event.preventDefault(); saveEditor(); }
    if (event.key === 'Escape') { closeSearch(); closeModal(); }
  });

  /* ------------------------------------------------------------
     Boot bindings
     ------------------------------------------------------------ */
  $('#loginForm').addEventListener('submit', login);
  $('#revealPass').addEventListener('click', () => { const input = $('#loginPass'); input.type = input.type === 'password' ? 'text' : 'password'; });
  $('#logoutButton').addEventListener('click', logout);
  $('#mobileMenu').addEventListener('click', () => { $('#sidebar').classList.add('open'); $('#sidebarScrim').classList.add('open'); });
  $('#sidebarScrim').addEventListener('click', closeSidebar);
  $('#openSearch').addEventListener('click', openSearch);
  $('#searchOverlay').addEventListener('click', event => { if (event.target === $('#searchOverlay')) closeSearch(); });
  $('#modalLayer').addEventListener('click', event => { if (event.target === $('#modalLayer')) closeModal(); });
  $('#modalLayer').addEventListener('click', event => {
    const submit = event.target.closest('[data-submit-form]');
    if (submit) document.getElementById(submit.dataset.submitForm)?.requestSubmit();
  });
  $('#topAvatar').addEventListener('click', () => { if (isOwner()) navigate('users'); });
  $('#searchInput').addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      const first = $('#searchResults [data-route]');
      if (first) { closeSearch(); navigate(first.dataset.route); }
    }
  });
  window.addEventListener('hashchange', () => { if (state.me) renderRoute(); });
  boot();
})();
