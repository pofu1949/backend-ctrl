/* 文案页沿用管理端的请求、会话与弹层，不在浏览器实现发布或可见性规则。 */
window.CtrlStoreEntries = {
  create({ api, esc, can, isOwner, openModal, closeModal, confirmDialog, toast, isActive }) {
    const state = { rows: [], sites: [], cursor: null, filters: {}, request: 0 };
    const view = () => document.querySelector('#view');
    const canEdit = () => can('entry.update');
    const siteLabel = id => state.sites.find(site => site.id === id)?.name || id || '库级';
    const summary = value => esc(value === null ? '尚未发布' : [...value].slice(0, 80).join('') || '（空文本）');

    async function render(append = false) {
      const request = ++state.request;
      const params = new URLSearchParams({ limit: '50' });
      for (const [key, value] of Object.entries(state.filters)) if (value) params.set(key, value);
      if (append && state.cursor) params.set('cursor', state.cursor);
      const [page, sites] = await Promise.all([api(`/entries?${params}`), api('/entry-sites')]);
      if (request !== state.request || !isActive()) return;
      state.rows = append ? [...state.rows, ...page.items] : page.items;
      state.cursor = page.next_cursor;
      state.sites = sites;
      view().innerHTML = pageHTML();
    }

    function pageHTML() {
      const filters = state.filters;
      return `<div class="page-head"><div><div class="eyebrow">COPY / 文案字典</div><h1>一句话，处处更新</h1><p class="desc">每个键保存一句纯文本。修改先存为草稿，发布后网站才会读到新文字。</p></div>${canEdit() ? '<button class="btn primary sm" data-entry-action="new">新建文案</button>' : ''}</div>
        <form class="entry-filters" data-entry-filter><label>语言<input class="input" name="language" value="${esc(filters.language || '')}" placeholder="全部语言，例如 zh-CN"></label><label>键前缀<input class="input" name="prefix" value="${esc(filters.prefix || '')}" placeholder="例如 home"></label><label>状态<select class="select" name="status"><option value="">全部状态</option><option value="draft" ${filters.status === 'draft' ? 'selected' : ''}>草稿</option><option value="published" ${filters.status === 'published' ? 'selected' : ''}>已发布</option></select></label><button class="btn secondary sm" type="submit">筛选</button></form>
        <div class="table-wrap"><table class="data-table entry-table"><thead><tr><th>键 / 语言</th><th>归属 / 可见性</th><th>状态</th><th>编辑中的文字</th><th>已发布文字</th><th>操作</th></tr></thead><tbody>${state.rows.length ? state.rows.map(row => `<tr><td><b class="mono">${esc(row.key)}</b><div>${esc(row.language)}</div></td><td>${esc(siteLabel(row.site_id))}<div>${row.visibility === 'site' ? '仅站点' : '公开'}</div></td><td><span class="pill ${row.status === 'published' ? 'published' : 'draft'}">${row.status === 'published' ? '已发布' : '草稿'}</span></td><td class="entry-value">${summary(row.value)}</td><td class="entry-value">${summary(row.published_value)}${row.published_key && (row.key !== row.published_key || row.language !== row.published_language) ? `<div class="mono">${esc(row.published_key)} / ${esc(row.published_language)}</div>` : ''}</td><td><button class="btn secondary xs" data-entry-action="open" data-id="${esc(row.id)}">${canEdit() ? '编辑' : '查看'}</button></td></tr>`).join('') : '<tr><td colspan="6"><div class="inline-note">没有符合条件的文案。</div></td></tr>'}</tbody></table></div>
        ${state.cursor ? '<div class="entry-more"><button class="btn secondary sm" data-entry-action="more">加载更多</button></div>' : ''}`;
    }

    async function open(id = null, notice = '') {
      const row = id ? await api(`/entries/${encodeURIComponent(id)}`) : null;
      if (!isActive()) return;
      const readOnly = !canEdit();
      const disabled = readOnly ? 'disabled' : '';
      const sites = state.sites.map(site => `<option value="${esc(site.id)}" ${row?.site_id === site.id ? 'selected' : ''}>${esc(site.name || site.key)}</option>`).join('');
      openModal({
        kicker: 'Copy / 文案', title: row ? (readOnly ? '查看文案' : '编辑文案草稿') : '新建文案', wide: true,
        body: `<form id="entryForm" data-entry-form data-id="${esc(id || '')}" data-revision="${row?.revision || ''}"><div class="form-error ${notice ? 'show' : ''}" role="alert">${esc(notice)}</div><div class="field-grid"><div class="field"><label for="entryKey">键</label><input id="entryKey" class="input mono" name="key" value="${esc(row?.key || '')}" required maxlength="128" pattern="[a-z0-9]+(([.]|-)[a-z0-9]+)*" placeholder="nav.about" ${disabled}></div><div class="field"><label for="entryLanguage">语言</label><input id="entryLanguage" class="input" name="language" value="${esc(row?.language || 'zh-CN')}" required maxlength="32" ${disabled}></div></div><div class="field-grid"><div class="field"><label for="entrySite">所属站点</label><select id="entrySite" name="site_id" class="select" ${disabled}><option value="">库级</option>${sites}</select></div><div class="field"><label for="entryVisibility">可见性</label><select id="entryVisibility" name="visibility" class="select" ${disabled}><option value="public" ${row?.visibility !== 'site' ? 'selected' : ''}>公开</option><option value="site" ${row?.visibility === 'site' ? 'selected' : ''}>仅站点</option></select></div></div><div class="field-help">库级公开文案可匿名读取；指定站点后仅该站点可读取。</div><div class="field"><label for="entryValue">编辑中的文字</label><textarea id="entryValue" class="textarea" name="value" rows="7" ${disabled}>${esc(row?.value || '')}</textarea><div class="field-help">最多 2000 个 Unicode 字符，支持换行与制表符。保存草稿不会改变网站当前显示的文字。</div></div>${row ? `<div class="field"><label>已发布文字</label><div class="entry-published">${esc(row.published_value ?? '尚未发布')}</div>${row.status === 'published' ? `<div class="field-help">${esc(row.published_key)} / ${esc(row.published_language)} · ${esc(siteLabel(row.published_site_id))} · ${row.published_visibility === 'site' ? '仅站点' : '公开'}</div>` : ''}</div><div class="field-help">当前修订号 ${row.revision}</div>` : ''}</form>`,
        footer: `${row && isOwner() && row.status === 'draft' ? `<button class="btn ghost sm danger-link" data-entry-action="delete" data-id="${esc(id)}">删除文案</button>` : ''}<button class="btn secondary sm" data-close-modal>关闭</button>${row && can('entry.publish') ? `${row.status === 'published' ? `<button class="btn secondary sm" data-entry-action="unpublish" data-id="${esc(id)}">撤回</button>` : ''}<button class="btn secondary sm" data-entry-action="publish" data-id="${esc(id)}">发布已保存草稿</button>` : ''}${canEdit() ? '<button class="btn primary sm" type="submit" form="entryForm">保存草稿</button>' : ''}`,
      });
    }

    async function showError(error, id = null) {
      if (error.status === 401) return;
      if (error.status === 409 && id) {
        const message = `${error.message}。已重新加载当前条目，请核对后重试。`;
        try {
          await render();
          await open(id, message);
        } catch (reloadError) {
          closeModal();
          toast(reloadError.status === 404 ? '这条文案已不存在，列表已刷新。' : reloadError.message, 'error');
        }
        return;
      }
      const box = document.querySelector('#entryForm .form-error');
      if (box) { box.textContent = error.message; box.classList.add('show'); }
      else toast(error.message, 'error');
    }

    async function lifecycle(action, id) {
      const row = await api(`/entries/${encodeURIComponent(id)}`);
      const labels = { publish: '发布', unpublish: '撤回', delete: '删除' };
      const label = labels[action];
      const key = crypto.randomUUID();
      confirmDialog({
        title: `${label}文案「${row.key}」？`, kicker: `Copy / ${label}`, confirmLabel: label,
        body: action === 'publish'
          ? `网站会读取这一份已保存的草稿：<div class="entry-published">${esc(row.value)}</div><p>编辑框中尚未保存的修改不会发布。</p>`
          : action === 'unpublish' ? '撤回后网站无法继续读取这条发布快照，草稿文字会保留。' : '永久删除这条已撤回的文案，此操作不可撤销。',
        onConfirm: async () => {
          const path = `/entries/${encodeURIComponent(id)}`;
          if (action === 'delete') await api(`${path}?revision=${row.revision}`, { method: 'DELETE' });
          else await api(`${path}/${action}`, { method: 'POST', body: { revision: row.revision }, headers: { 'Idempotency-Key': key } });
          toast(`文案已${label}`);
          await render();
        },
        onError: error => showError(error, id),
      });
    }

    document.addEventListener('click', async event => {
      const button = event.target.closest('[data-entry-action]');
      if (!button || !isActive()) return;
      const { entryAction: action, id } = button.dataset;
      button.disabled = true;
      try {
        if (action === 'new') await open();
        else if (action === 'open') await open(id);
        else if (action === 'more') await render(true);
        else await lifecycle(action, id);
      } catch (error) { await showError(error, id); }
      finally { button.disabled = false; }
    });

    document.addEventListener('submit', async event => {
      const form = event.target;
      if (!isActive() || !form.matches('[data-entry-form], [data-entry-filter]')) return;
      event.preventDefault();
      if (form.dataset.busy) return;
      form.dataset.busy = '1';
      try {
        const fields = Object.fromEntries(new FormData(form));
        if (form.matches('[data-entry-filter]')) { state.filters = fields; await render(); return; }
        const id = form.dataset.id;
        fields.site_id = fields.site_id || null;
        if (id) fields.revision = Number(form.dataset.revision);
        const row = await api(id ? `/entries/${encodeURIComponent(id)}` : '/entries', {
          method: id ? 'PATCH' : 'POST', body: fields,
        });
        toast('文案草稿已保存，网站仍读取已发布版本');
        await render();
        await open(row.id);
      } catch (error) { await showError(error, form.dataset.id); }
      finally { delete form.dataset.busy; }
    });

    return { render };
  },
};
