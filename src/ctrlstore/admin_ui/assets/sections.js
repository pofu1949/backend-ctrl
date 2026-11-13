/* 区块页只负责把模板填进表单并预览。发布、可见性和配图是否公开都由接口决定。 */
window.CtrlStoreSections = {
  create({ api, esc, can, isOwner, openModal, closeModal, confirmDialog, toast, isActive }) {
    const state = {
      rows: [], sites: [], templates: [], cursor: null, filters: {}, request: 0,
      section: null, doc: null, assets: {}, template: null, picker: null,
    };
    const view = () => document.querySelector('#view');
    const canEdit = () => can('section.update');
    const siteLabel = id => state.sites.find(site => site.id === id)?.name || (id ? id : '库级');
    const templateLabel = id => state.templates.find(item => item.id === id)?.label || id || '';

    function crumb(text) {
      const node = document.querySelector('#crumb');
      if (node) node.textContent = text;
    }

    async function loadTemplates() {
      if (state.templates.length) return state.templates;
      const payload = await api('/section-templates');
      state.templates = payload.templates || [];
      return state.templates;
    }

    async function loadSites() {
      if (state.sites.length) return state.sites;
      state.sites = await api('/section-sites');
      return state.sites;
    }

    async function render(id) {
      if (id) await renderEditor(id);
      else await renderList();
    }

    async function renderList() {
      const request = ++state.request;
      const params = new URLSearchParams({ limit: '50' });
      for (const [key, value] of Object.entries(state.filters)) if (value) params.set(key, value);
      if (state.cursor && state.append) params.set('cursor', state.cursor);
      const [page, , templates] = await Promise.all([
        api(`/sections?${params}`),
        loadSites(),
        loadTemplates(),
      ]);
      if (request !== state.request || !isActive()) return;
      state.rows = state.append ? [...state.rows, ...page.items] : page.items;
      state.cursor = page.next_cursor;
      state.append = false;
      state.templates = templates;
      crumb('区块');
      view().innerHTML = listHTML();
    }

    function listHTML() {
      const filters = state.filters;
      const actions = canEdit()
        ? '<button class="btn secondary sm" type="button" data-section-action="example">用费用说明示例</button><button class="btn primary sm" type="button" data-section-action="new">新建区块</button>'
        : '';
      const rows = state.rows.length ? state.rows.map(row => `<tr>
        <td><b class="mono">${esc(row.slug)}</b><div>${esc(row.language)} · ${esc(templateLabel(row.template))}</div></td>
        <td>${esc(row.title || '未命名')}<div>${esc(row.body?.title || '')}</div></td>
        <td>${esc(siteLabel(row.site_id))}<div>${row.visibility === 'site' ? '仅站点' : '公开'}</div></td>
        <td><span class="pill ${row.status === 'published' ? 'published' : 'draft'}">${row.status === 'published' ? '已发布' : '草稿'}</span></td>
        <td><button class="btn secondary xs" type="button" data-section-action="open" data-id="${esc(row.id)}">${canEdit() ? '编辑' : '查看'}</button></td>
      </tr>`).join('') : '<tr><td colspan="5"><div class="inline-note">还没有区块。可以用费用说明示例看一层字段怎么落到页面上。</div></td></tr>';
      return `<div class="page-head"><div><div class="eyebrow">SECTIONS / 区块</div><h1>一页一层字段</h1><p class="desc">每个区块是一棵固定形状的字段树。文字和配图先存成草稿，发布后网站一次读取整棵树。</p></div><div class="actions">${actions}</div></div>
        <form class="entry-filters" data-section-form="filter"><label>语言<input class="input" name="language" value="${esc(filters.language || '')}" placeholder="全部语言，例如 zh-CN"></label><label>别名前缀<input class="input" name="prefix" value="${esc(filters.prefix || '')}" placeholder="例如 pricing"></label><label>状态<select class="select" name="status"><option value="">全部状态</option><option value="draft" ${filters.status === 'draft' ? 'selected' : ''}>草稿</option><option value="published" ${filters.status === 'published' ? 'selected' : ''}>已发布</option></select></label><button class="btn secondary sm" type="submit">筛选</button></form>
        <div class="table-wrap"><table class="data-table entry-table"><thead><tr><th>别名</th><th>名称 / 主标题</th><th>归属</th><th>状态</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
        ${state.cursor ? '<div class="entry-more"><button class="btn secondary sm" type="button" data-section-action="more">加载更多</button></div>' : ''}`;
    }

    async function renderEditor(id) {
      const request = ++state.request;
      const [section, , templates] = await Promise.all([
        api(`/sections/${encodeURIComponent(id)}`),
        loadSites(),
        loadTemplates(),
      ]);
      if (request !== state.request || !isActive()) return;
      state.section = section;
      state.doc = structuredClone(section.body);
      state.assets = section.assets || {};
      state.template = templates.find(item => item.id === section.template) || null;
      state.templates = templates;
      crumb('区块 / 编辑');
      view().innerHTML = editorHTML();
      paintFields();
      paintPreview();
    }

    function editorHTML() {
      const row = state.section;
      const readOnly = !canEdit();
      const disabled = readOnly ? 'disabled' : '';
      const sites = state.sites.map(site => `<option value="${esc(site.id)}" ${row.site_id === site.id ? 'selected' : ''}>${esc(site.name || site.key)}</option>`).join('');
      const publishButtons = can('section.publish') && row
        ? `${row.status === 'published' ? '<button class="btn secondary sm" type="button" data-section-action="unpublish">撤回</button>' : ''}<button class="btn secondary sm" type="button" data-section-action="publish">发布已保存草稿</button>`
        : '';
      return `<div class="page-head"><div><div class="eyebrow">SECTION / ${esc(templateLabel(row.template))}</div><h1>${esc(row.title || row.slug)}</h1><p class="desc">修订号 ${esc(row.revision)}。保存只改草稿；网站读取 <span class="mono">GET /api/public/v1/sections/${esc(row.slug)}</span></p></div>
        <div class="actions"><button class="btn ghost sm" type="button" data-section-action="back">返回列表</button>${canEdit() ? '<button class="btn secondary sm" type="button" data-section-action="fill">填入费用说明示例</button><button class="btn primary sm" type="submit" form="sectionForm">保存草稿</button>' : ''}${publishButtons}${isOwner() && row.status === 'draft' ? `<button class="btn ghost sm danger-link" type="button" data-section-action="delete">删除</button>` : ''}</div></div>
        <form id="sectionForm" class="section-editor" data-section-form="save">
            <div class="form-error" role="alert"></div>
            <div class="field-grid">
              <div class="field"><label for="sectionSlug">别名</label><input id="sectionSlug" class="input mono" name="slug" value="${esc(row.slug)}" required maxlength="128" ${disabled}></div>
              <div class="field"><label for="sectionLanguage">语言</label><input id="sectionLanguage" class="input" name="language" value="${esc(row.language)}" required maxlength="32" ${disabled}></div>
            </div>
            <div class="field-grid">
              <div class="field"><label for="sectionTitle">管理名称</label><input id="sectionTitle" class="input" name="title" value="${esc(row.title)}" maxlength="255" ${disabled}></div>
              <div class="field"><label for="sectionVisibility">可见性</label><select id="sectionVisibility" class="select" name="visibility" ${disabled}><option value="public" ${row.visibility !== 'site' ? 'selected' : ''}>公开</option><option value="site" ${row.visibility === 'site' ? 'selected' : ''}>仅站点</option></select></div>
            </div>
            <div class="field"><label for="sectionSite">所属站点</label><select id="sectionSite" class="select" name="site_id" ${disabled}><option value="">库级</option>${sites}</select><div class="field-help">库级公开区块可匿名读取。指定站点后只有该站点能读。同别名时本站覆盖库级。</div></div>
            <div id="sectionFields" class="section-fields"></div>
          </form>
          <aside class="section-preview" id="sectionPreview"></aside>`;
    }

    function paintFields() {
      const root = document.querySelector('#sectionFields');
      if (!root || !state.template) return;
      const readOnly = !canEdit();
      root.innerHTML = state.template.fields.map(field => fieldHTML(field, state.doc[field.key], field.key, readOnly)).join('');
      bindPreviews(root);
    }

    function fieldHTML(field, value, path, readOnly) {
      const disabled = readOnly ? 'disabled' : '';
      if (field.type === 'text' || field.type === 'link') {
        return `<div class="field"><label>${esc(field.label)}${field.required ? ' *' : ''}</label><input class="input" data-section-path="${esc(path)}" value="${esc(value || '')}" maxlength="${field.max_length || 200}" ${disabled}></div>`;
      }
      if (field.type === 'longtext') {
        return `<div class="field"><label>${esc(field.label)}${field.required ? ' *' : ''}</label><textarea class="textarea" data-section-path="${esc(path)}" rows="4" maxlength="${field.max_length || 2000}" ${disabled}>${esc(value || '')}</textarea></div>`;
      }
      if (field.type === 'assets') return assetsHTML(field, value || [], path, readOnly);
      if (field.type === 'repeat') return repeatHTML(field, value || [], path, readOnly);
      return '';
    }

    function assetsHTML(field, items, path, readOnly) {
      const cards = items.map((id, index) => assetCard(id, path, index, items.length, readOnly)).join('')
        || '<div class="inline-note">还没有配图。这里引用内容库里的图片或视频，不在区块里重新上传。</div>';
      const add = readOnly ? '' : `<button class="btn secondary xs" type="button" data-section-action="add-asset" data-path="${esc(path)}" data-max="${field.max_items || 8}">从内容库选择</button>`;
      return `<div class="field"><label>${esc(field.label)}</label><div class="section-fields">${cards}</div>${add}</div>`;
    }

    function assetCard(id, path, index, total, readOnly) {
      const asset = state.assets[id];
      const title = asset?.title || id;
      const status = !asset ? '内容已不存在' : (asset.status === 'published' ? '已发布' : '尚未发布，区块发布时会被拒绝');
      const media = asset && asset.kind === 'image'
        ? `<img alt="" src="/api/admin/v1/assets/${esc(id)}/preview">`
        : `<div class="ph">${asset?.kind === 'video' ? '视频' : '缺失'}</div>`;
      const controls = readOnly ? '' : `<button class="btn ghost xs" type="button" data-section-action="move" data-path="${esc(path)}" data-index="${index}" data-delta="-1" ${index === 0 ? 'disabled' : ''}>上移</button><button class="btn ghost xs" type="button" data-section-action="move" data-path="${esc(path)}" data-index="${index}" data-delta="1" ${index === total - 1 ? 'disabled' : ''}>下移</button><button class="btn ghost xs danger-link" type="button" data-section-action="remove" data-path="${esc(path)}" data-index="${index}">移除</button>`;
      return `<div class="section-asset">${media}<div class="grow"><b>${esc(title)}</b><span>${esc(status)}</span></div>${controls}</div>`;
    }

    function repeatHTML(field, items, path, readOnly) {
      const cards = items.map((item, index) => {
        const prefix = `${path}.${index}`;
        const controls = readOnly ? '' : `<button class="btn ghost xs" type="button" data-section-action="move" data-path="${esc(path)}" data-index="${index}" data-delta="-1" ${index === 0 ? 'disabled' : ''}>上移</button><button class="btn ghost xs" type="button" data-section-action="move" data-path="${esc(path)}" data-index="${index}" data-delta="1" ${index === items.length - 1 ? 'disabled' : ''}>下移</button><button class="btn ghost xs danger-link" type="button" data-section-action="remove" data-path="${esc(path)}" data-index="${index}">删除卡片</button>`;
        const inner = field.fields.map(child => fieldHTML(child, item[child.key], `${prefix}.${child.key}`, readOnly)).join('');
        return `<article class="section-card"><div class="section-card-head"><b>卡片 ${index + 1}</b>${controls}</div>${inner}</article>`;
      }).join('');
      const add = readOnly ? '' : `<button class="btn secondary sm" type="button" data-section-action="add-point">加一张下级卡片</button>`;
      return `<div class="field"><label>${esc(field.label)}</label>${cards || '<div class="inline-note">还没有下级卡片。</div>'}${add}</div>`;
    }

    function paintPreview() {
      const root = document.querySelector('#sectionPreview');
      if (!root || !state.doc) return;
      const doc = state.doc;
      const gallery = (doc.gallery || []).map(id => previewMedia(id)).join('')
        || '<div class="ph">配图会出现在这里</div><div class="ph">从内容库选择图片或视频</div>';
      const points = doc.points || [];
      const first = points[0] ? pointHTML(points[0]) : '<div class="inline-note">第一张下级卡片会出现在左侧。</div>';
      const rest = points.slice(1).map(point => pointHTML(point)).join('')
        || '<div class="inline-note">其余下级卡片会出现在右侧。</div>';
      const endpoint = `${location.origin}/api/public/v1/sections/${encodeURIComponent(state.section?.slug || '')}?language=${encodeURIComponent(state.section?.language || 'zh-CN')}`;
      root.innerHTML = `<div class="fee-sheet">
        <div class="fee-legend"><span class="is-title">主标题</span><span class="is-lead">导语</span><span class="is-point">下级卡片</span><span class="is-body">卡片正文</span></div>
        <div class="fee-kicker">${esc(doc.eyebrow || '眉题')}</div>
        <div class="fee-top">
          <h2 class="fee-title">${esc(doc.title || '主标题')}</h2>
          <p class="fee-lead">${esc(doc.lead || '导语')}</p>
        </div>
        <div class="fee-stage">
          <div>${first}</div>
          <div class="fee-gallery">${gallery}</div>
          <div>${rest}</div>
        </div>
        <p class="fee-note">这是编辑预览。网站只在发布后读取已保存的草稿；未发布或已撤回的图片不会出现在公开结果里。</p>
        <p class="fee-note"><a href="${esc(endpoint)}" target="_blank" rel="noopener">${esc(endpoint)}</a></p>
        <pre class="section-json">${esc(JSON.stringify({ slug: state.section?.slug, template: state.section?.template, fields: doc }, null, 2))}</pre>
      </div>`;
      bindPreviews(root);
    }

    function pointHTML(point) {
      const action = actionHTML(point.action_label, point.action_href);
      const media = (point.media || []).length
        ? `<div class="fee-gallery">${point.media.map(id => previewMedia(id)).join('')}</div>`
        : '';
      return `<article class="fee-point"><div class="num">${esc(point.number || '')}</div><h3>${esc(point.title || '未命名卡片')}</h3><p>${esc(point.body || '')}</p>${media}${action}</article>`;
    }

    function previewMedia(id) {
      const asset = state.assets[id];
      if (asset?.kind === 'image') return `<img alt="${esc(asset.title || '')}" src="/api/admin/v1/assets/${esc(id)}/preview">`;
      return `<div class="ph">${esc(asset?.title || (asset?.kind === 'video' ? '视频' : '配图'))}</div>`;
    }

    function actionHTML(label, href) {
      if (!label && !href) return '';
      const safe = typeof href === 'string' && (href.startsWith('/') && !href.startsWith('//') || href.startsWith('https://') || href.startsWith('http://'));
      if (safe) return `<a class="fee-action" href="${esc(href)}" target="_blank" rel="noopener">${esc(label || href)}</a>`;
      return `<span class="fee-action">${esc(label || href || '')}</span>`;
    }

    function bindPreviews(root) {
      root.querySelectorAll('img').forEach(img => {
        img.addEventListener('error', () => {
          const fallback = document.createElement('div');
          fallback.className = 'ph';
          fallback.textContent = '无预览';
          img.replaceWith(fallback);
        }, { once: true });
      });
    }

    function pullFields() {
      document.querySelectorAll('[data-section-path]').forEach(input => {
        assign(state.doc, input.dataset.sectionPath, input.value);
      });
    }

    function assign(root, path, value) {
      const parts = path.split('.');
      let node = root;
      for (let index = 0; index < parts.length - 1; index += 1) node = node[parts[index]];
      node[parts[parts.length - 1]] = value;
    }

    function lookup(root, path) {
      return path.split('.').reduce((node, key) => node[key], root);
    }

    function blankItem(fields) {
      const item = {};
      fields.forEach(field => {
        item[field.key] = field.type === 'assets' || field.type === 'repeat' ? [] : '';
      });
      return item;
    }

    function readMeta(form) {
      const data = Object.fromEntries(new FormData(form));
      return {
        slug: String(data.slug || '').trim(),
        language: String(data.language || '').trim(),
        title: String(data.title || ''),
        visibility: data.visibility || 'public',
        site_id: data.site_id || null,
      };
    }

    async function openCreate() {
      await Promise.all([loadTemplates(), loadSites()]);
      if (!isActive()) return;
      const sites = state.sites.map(site => `<option value="${esc(site.id)}">${esc(site.name || site.key)}</option>`).join('');
      openModal({
        kicker: 'Section / 区块', title: '新建区块', wide: true,
        body: `<form id="sectionCreateForm" data-section-form="create"><div class="form-error" role="alert"></div><div class="field"><label>别名</label><input class="input mono" name="slug" required maxlength="128" placeholder="pricing.fee" pattern="[a-z0-9]+(([.]|-)[a-z0-9]+)*"></div><div class="field"><label>管理名称</label><input class="input" name="title" maxlength="255" placeholder="费用如何确定"></div><div class="field-grid"><div class="field"><label>语言</label><input class="input" name="language" value="zh-CN" required maxlength="32"></div><div class="field"><label>可见性</label><select class="select" name="visibility"><option value="public">公开</option><option value="site">仅站点</option></select></div></div><div class="field"><label>所属站点</label><select class="select" name="site_id"><option value="">库级</option>${sites}</select></div><input type="hidden" name="template" value="stack"></form>`,
        footer: '<button class="btn secondary sm" type="button" data-close-modal>取消</button><button class="btn primary sm" type="submit" form="sectionCreateForm">创建草稿</button>',
      });
    }

    async function openPicker(path, max) {
      if (!can('asset.read')) {
        toast('选择配图需要「查看内容」权限。', 'error');
        return;
      }
      const [images, videos] = await Promise.all([
        api('/assets?kind=image&limit=40'),
        api('/assets?kind=video&limit=20'),
      ]);
      if (!isActive()) return;
      const items = [...(images.items || []), ...(videos.items || [])];
      items.forEach(item => { state.assets[item.id] = item; });
      state.picker = { path, max, items, imgCursor: images.next_cursor || null, vidCursor: videos.next_cursor || null };
      openModal({
        kicker: 'Library / 配图', title: '选择图片或视频', wide: true,
        body: pickerGridHTML(),
        footer: '<button class="btn secondary sm" type="button" data-close-modal>关闭</button>',
      });
      bindPreviews(document.querySelector('#modalBody'));
    }

    function pickerGridHTML() {
      const picker = state.picker;
      if (!picker) return '';
      const { items, imgCursor, vidCursor } = picker;
      const hasMore = imgCursor || vidCursor;
      const grid = items.length ? `<div class="section-picker">${items.map(item => `<button class="section-pick" type="button" data-section-action="pick" data-id="${esc(item.id)}">${item.kind === 'image' ? `<img alt="" src="/api/admin/v1/assets/${esc(item.id)}/preview">` : '<div class="ph">视频</div>'}<b>${esc(item.title || '未命名')}</b><span>${item.status === 'published' ? '已发布' : '尚未发布'}</span></button>`).join('')}</div>` : '<div class="inline-note">内容库里还没有图片或视频。先在「上传」加入，再回到这里选择。</div>';
      return grid + (hasMore ? '<div class="entry-more"><button class="btn secondary sm" type="button" data-section-action="picker-more">加载更多</button></div>' : '');
    }

    async function loadMorePicker() {
      const picker = state.picker;
      if (!picker) return;
      const { imgCursor, vidCursor } = picker;
      const requests = [];
      if (imgCursor) requests.push(api(`/assets?kind=image&limit=40&cursor=${encodeURIComponent(imgCursor)}`));
      else requests.push(Promise.resolve({ items: [], next_cursor: null }));
      if (vidCursor) requests.push(api(`/assets?kind=video&limit=20&cursor=${encodeURIComponent(vidCursor)}`));
      else requests.push(Promise.resolve({ items: [], next_cursor: null }));
      const [images, videos] = await Promise.all(requests);
      if (!isActive() || !state.picker) return;
      const newItems = [...(images.items || []), ...(videos.items || [])];
      newItems.forEach(item => { state.assets[item.id] = item; });
      picker.items = [...picker.items, ...newItems];
      picker.imgCursor = images.next_cursor || null;
      picker.vidCursor = videos.next_cursor || null;
      const modalBody = document.querySelector('#modalBody');
      if (modalBody) {
        modalBody.innerHTML = pickerGridHTML();
        bindPreviews(modalBody);
      }
    }

    function showFormError(error) {
      const box = document.querySelector('#sectionForm .form-error, #sectionCreateForm .form-error');
      if (box) {
        box.textContent = error.message;
        box.classList.add('show');
      } else toast(error.message, 'error');
    }

    async function save(form) {
      pullFields();
      const id = state.section.id;
      const fields = readMeta(form);
      fields.revision = state.section.revision;
      fields.body = state.doc;
      const row = await api(`/sections/${encodeURIComponent(id)}`, { method: 'PATCH', body: fields });
      toast('区块草稿已保存。网站仍读取已发布的版本');
      state.section = row;
      state.doc = structuredClone(row.body);
      state.assets = { ...state.assets, ...(row.assets || {}) };
      const revisionNote = document.querySelector('.page-head .desc');
      if (revisionNote) revisionNote.innerHTML = `修订号 ${esc(row.revision)}。保存只改草稿；网站读取 <span class="mono">GET /api/public/v1/sections/${esc(row.slug)}</span>`;
      const slug = document.querySelector('#sectionSlug');
      if (slug) slug.value = row.slug;
      paintPreview();
    }

    async function lifecycle(action) {
      const id = state.section?.id;
      if (!id) return;
      pullFields();
      const row = await api(`/sections/${encodeURIComponent(id)}`);
      const form = document.querySelector('#sectionForm');
      const meta = form ? readMeta(form) : null;
      const dirty = meta && (
        meta.slug !== row.slug || meta.language !== row.language || meta.title !== row.title
        || meta.visibility !== row.visibility || (meta.site_id || null) !== (row.site_id || null)
        || JSON.stringify(state.doc) !== JSON.stringify(row.body)
      );
      const labels = { publish: '发布', unpublish: '撤回', delete: '删除' };
      const key = (crypto.randomUUID && crypto.randomUUID()) || `section-${Date.now()}`;
      confirmDialog({
        title: `${labels[action]}区块「${row.slug}」？`,
        kicker: `Section / ${labels[action]}`,
        confirmLabel: labels[action],
        body: action === 'publish'
          ? `网站会读取上次保存的草稿。${dirty ? '<p>编辑框里还有未保存的修改，这次不会发布。</p>' : ''}<p>配图必须已经发布，否则这次发布会被拒绝。</p>`
          : action === 'unpublish' ? '撤回后公开接口不再返回这个区块，草稿会保留。' : '永久删除这条已撤回的区块。',
        onConfirm: async () => {
          if (action === 'delete') await api(`/sections/${encodeURIComponent(id)}?revision=${row.revision}`, { method: 'DELETE' });
          else await api(`/sections/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: { revision: row.revision }, headers: { 'Idempotency-Key': key } });
          toast(`区块已${labels[action]}`);
          if (action === 'delete') location.hash = '#/sections';
          else await renderEditor(id);
        },
      });
    }

    document.addEventListener('click', async event => {
      const button = event.target.closest('[data-section-action]');
      if (!button || !isActive()) return;
      const action = button.dataset.sectionAction;
      button.disabled = true;
      try {
        if (action === 'new') await openCreate();
        else if (action === 'open') location.hash = `#/sections/${button.dataset.id}`;
        else if (action === 'back') location.hash = '#/sections';
        else if (action === 'more') { state.append = true; await renderList(); }
        else if (action === 'example') await createExample();
        else if (action === 'fill') fillExample();
        else if (action === 'add-point') addPoint();
        else if (action === 'add-asset') await openPicker(button.dataset.path, Number(button.dataset.max || 8));
        else if (action === 'picker-more') await loadMorePicker();
        else if (action === 'pick') pickAsset(button.dataset.id);
        else if (action === 'remove' || action === 'move') moveOrRemove(action, button);
        else if (action === 'publish' || action === 'unpublish' || action === 'delete') await lifecycle(action);
      } catch (error) {
        if (error.status !== 401) showFormError(error);
      } finally {
        button.disabled = false;
      }
    });

    document.addEventListener('submit', async event => {
      const form = event.target;
      if (!isActive() || !form.matches('[data-section-form]')) return;
      event.preventDefault();
      if (form.dataset.busy) return;
      form.dataset.busy = '1';
      try {
        if (form.dataset.sectionForm === 'filter') {
          state.filters = Object.fromEntries(new FormData(form));
          state.cursor = null;
          await renderList();
        } else if (form.dataset.sectionForm === 'create') {
          const fields = Object.fromEntries(new FormData(form));
          fields.site_id = fields.site_id || null;
          fields.title = fields.title || fields.slug;
          const row = await api('/sections', { method: 'POST', body: fields });
          closeModal();
          toast('区块草稿已创建');
          location.hash = `#/sections/${row.id}`;
        } else {
          await save(form);
        }
      } catch (error) {
        if (error.status !== 401) showFormError(error);
      } finally {
        delete form.dataset.busy;
      }
    });

    document.addEventListener('input', event => {
      const input = event.target.closest('[data-section-path]');
      if (!input || !isActive() || !state.doc) return;
      assign(state.doc, input.dataset.sectionPath, input.value);
      paintPreview();
    });

    function fillExample() {
      const template = state.templates.find(item => item.id === 'stack');
      if (!template || !state.doc) return;
      state.doc = structuredClone(template.example);
      paintFields();
      paintPreview();
      toast('已填入费用说明示例。保存草稿后才会写入');
    }

    async function createExample() {
      await loadTemplates();
      const template = state.templates.find(item => item.id === 'stack');
      const page = await api('/sections?prefix=pricing.fee&limit=50');
      const existing = (page.items || []).find(row => row.slug === 'pricing.fee' && row.language === 'zh-CN' && !row.site_id);
      if (existing) {
        location.hash = `#/sections/${existing.id}`;
        return;
      }
      const row = await api('/sections', {
        method: 'POST',
        body: {
          slug: 'pricing.fee', title: '费用如何确定', template: 'stack',
          language: 'zh-CN', visibility: 'public', body: template.example,
        },
      });
      toast('已按费用说明示例建好草稿');
      location.hash = `#/sections/${row.id}`;
    }

    function addPoint() {
      pullFields();
      const field = state.template.fields.find(item => item.key === 'points');
      if (!field) return;
      if (state.doc.points.length >= (field.max_items || 12)) {
        toast('下级卡片已达上限', 'error');
        return;
      }
      state.doc.points.push(blankItem(field.fields));
      paintFields();
      paintPreview();
    }

    function pickAsset(id) {
      const picker = state.picker;
      if (!picker) return;
      const list = lookup(state.doc, picker.path);
      if (list.includes(id)) {
        toast('这张已经在当前字段里');
        return;
      }
      if (list.length >= picker.max) {
        toast('这个字段的配图已满', 'error');
        return;
      }
      list.push(id);
      closeModal();
      state.picker = null;
      paintFields();
      paintPreview();
    }

    function moveOrRemove(action, button) {
      pullFields();
      const list = lookup(state.doc, button.dataset.path);
      const index = Number(button.dataset.index);
      if (action === 'remove') list.splice(index, 1);
      else {
        const next = index + Number(button.dataset.delta);
        if (next < 0 || next >= list.length) return;
        const [item] = list.splice(index, 1);
        list.splice(next, 0, item);
      }
      paintFields();
      paintPreview();
    }

    return { render };
  },
};
