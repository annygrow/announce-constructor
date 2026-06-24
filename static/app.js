/**
 * Верстальщик рассылок — ZeroCoder
 * Frontend JavaScript
 */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let parsedData = {};
let generatedOutputs = {};
let emailBlocks = {};       // { channelKey: [{type, paragraphs_html, ...}] }
let tgVariants = [];
let _dragSrcKey = null;
let _dragSrcIdx = -1;
let _dragMouseY  = 0;
let _dragScrollRaf = null;
let metaFieldsPopulated = false;
let emailVariants = [];
let activeChannelKey = 'utm';

const EMAIL_CHANNELS = ['email', 'email_unisender'];
const TG_BOTS_CHANNELS = ['tg_bots'];

const TG_HTML_CHANNELS = [
  'tg_gc', 'tg_voronki', 'pomoshnik',
  'max', 'push', 'tg_channel', 'instagram',
  'neuro_pokoi', 'youtube', 'tests'
];

const TG_MARKDOWN_CHANNELS = ['neurocat'];
const GC_PUSH_TG_CHANNELS  = ['tg_gc', 'max'];

const CHANNEL_ORDER = [
  'email', 'email_unisender',
  'tg_gc', 'tg_voronki', 'pomoshnik', 'neurocat',
  'max', 'push', 'tg_channel', 'instagram',
  'neuro_pokoi', 'youtube', 'tests',
  'tg_bots'
];

const CHANNEL_NAMES = {
  email:            'Почта ГК',
  email_unisender:  'Почта Unisender',
  tg_gc:            'ТГ бот (ГК)',
  tg_voronki:       'ТГ бот (Воронки)',
  pomoshnik:        'Помощник',
  neurocat:         'Нейрокот',
  max:              'Max',
  push:             'Пуш',
  tg_channel:       'ТГ канал',
  instagram:        'Инстаграм',
  neuro_pokoi:      'Нейросетевые покои',
  youtube:          'YouTube',
  tests:            'Тесты',
  tg_bots:          'ТГ Боты',
};

const BLOCK_LABELS = {
  block_white:          'Белый',
  block_grey:           'Серый',
  block_dotted:         'Пунктирный',
  block_blue_cta:       'Синий CTA',
  block_blue_text:      'Синий текст',
  block_button:         'Кнопка',
  block_spacer:         'Отступ',
  block_image:          'Картинка',
  block_2col_img_text:  '2 колонки (фото+текст)',
  block_2col_text_img:  '2 колонки (текст+фото)',
  block_2col_text_text: '2 колонки (текст+текст)',
  block_3col_text:      '3 колонки',
};

// ---------------------------------------------------------------------------
// Generation
// ---------------------------------------------------------------------------

async function generateAll() {
  const url = document.getElementById('docUrl').value.trim();

  if (!url && !parsedData.email_html && !parsedData.tg_html) {
    showError('Введите ссылку на Google Doc');
    return;
  }

  const btn = document.querySelector('.generate-btn');
  setButtonLoading(btn, '⏳ Генерация...');
  setLoadingState(true);

  try {
    if (url) {
      const parseResp = await fetch('/api/parse', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      const parseData = await parseResp.json();

      if (parseData.error) {
        showError('Ошибка разбора: ' + parseData.error);
        return;
      }

      parsedData = parseData;
      setupTgVariants(parseData.tg_variants);
      emailVariants = parseData.email_variants || [];

      if (parseData.subject || parseData.preview) {
        metaFieldsPopulated = true;
        document.getElementById('subjectField').value = parseData.subject || '';
        document.getElementById('previewField').value = parseData.preview || '';
      }

      // Auto-fill campaign and date from document if fields are currently empty
      const campField = document.getElementById('utmCampaign');
      const dateField = document.getElementById('utmDate');
      if (parseData.doc_campaign && !campField.value.trim()) {
        campField.value = parseData.doc_campaign;
      }
      if (parseData.doc_date && !dateField.value.trim()) {
        dateField.value = parseData.doc_date;
      }
    }

    await _doGenerate();

  } catch (e) {
    showError('Ошибка: ' + e.message);
  } finally {
    resetButton(btn, '⚡ Сгенерировать');
    setLoadingState(false);
  }
}

async function _doGenerate() {
  const channels = getCheckedChannels();
  const campaign = document.getElementById('utmCampaign').value.trim();
  const date     = document.getElementById('utmDate').value.trim();
  const segment  = parsedData.segment || '';
  const images   = getImageUrls();

  // Warn if campaign or date are missing
  const missing = [];
  if (!campaign) missing.push('тег активности (utm_campaign)');
  if (!date)     missing.push('дата отправки');
  if (missing.length > 0) {
    const ok = confirm(`Не удалось определить: ${missing.join(' и ')}.\n\nUTM-метки будут без этих параметров. Продолжить?`);
    if (!ok) return;
  }

  const resp = await fetch('/api/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: parsedData, channels, campaign, date, segment, images }),
  });

  const data = await resp.json();
  if (data.error) throw new Error(data.error);

  generatedOutputs = data;

  // Collect email blocks
  for (const key of EMAIL_CHANNELS) {
    if (data[`${key}_blocks`]) emailBlocks[key] = data[`${key}_blocks`];
  }

  rebuildTabBar(data);

  autoFillUtmFromDocLinks(data.doc_urls || []);

  // Auto-fill image URL fields with URLs uploaded to YC Storage during generation
  if (data.uploaded_image_urls && data.uploaded_image_urls.length > 0) {
    fillUploadedImageUrls(data.uploaded_image_urls);
  }
}

function setLoadingState(on) {
  ['tg_gc', 'tg_voronki', 'pomoshnik', 'neurocat', 'max', 'push',
   'tg_channel', 'instagram', 'neuro_pokoi', 'youtube', 'tests', 'tg_bots'
  ].forEach(key => {
    const ta = document.getElementById(`code-${key}`);
    if (ta) {
      if (on) { ta.classList.add('loading'); ta.placeholder = 'Генерация...'; }
      else    { ta.classList.remove('loading'); ta.placeholder = ''; }
    }
  });
}

// ---------------------------------------------------------------------------
// Dynamic tab bar
// ---------------------------------------------------------------------------

function getChannelIcon(key) {
  if (EMAIL_CHANNELS.includes(key))   return '📧';
  if (TG_BOTS_CHANNELS.includes(key)) return '🤖';
  return '💬';
}

function rebuildTabBar(data) {
  const tabBar        = document.getElementById('outputTabs');
  const panelsWrapper = document.getElementById('channelPanels');

  // Clear channel tabs (leave UTM tab)
  [...tabBar.querySelectorAll('.channel-tab')].forEach(t => t.remove());
  panelsWrapper.innerHTML = '';

  let firstKey = null;

  for (const key of CHANNEL_ORDER) {
    if (!data[key]) continue;

    const icon = getChannelIcon(key);
    const name = CHANNEL_NAMES[key] || key;

    const btn = document.createElement('button');
    btn.className = 'tab-btn channel-tab';
    btn.dataset.panel = key;
    btn.innerHTML = `${icon} ${name}`;
    btn.onclick = () => switchTabPanel(key);
    tabBar.insertBefore(btn, tabBar.lastElementChild); // before UTM tab

    let panelEl;
    if (EMAIL_CHANNELS.includes(key)) {
      panelEl = createEmailPanelEl(key, data[key], data[`${key}_blocks`] || []);
    } else if (TG_BOTS_CHANNELS.includes(key)) {
      panelEl = createTgBotsPanelEl(key, data[key]);
    } else if (TG_MARKDOWN_CHANNELS.includes(key)) {
      panelEl = createTgMarkdownPanelEl(key, data[key], data[`${key}_links`] || []);
    } else if (TG_HTML_CHANNELS.includes(key)) {
      panelEl = createTgHtmlPanelEl(key, data[key]);
    }

    if (panelEl) {
      panelsWrapper.appendChild(panelEl);
      if (EMAIL_CHANNELS.includes(key)) renderBlockEditorForPanel(key);
    }
    if (!firstKey) firstKey = key;
  }

  if (firstKey) switchTabPanel(firstKey);
  else switchTabPanel('utm');
}

function switchTabPanel(key) {
  activeChannelKey = key;

  const isEmailTab = EMAIL_CHANNELS.includes(key);

  // Show Тема/прехедер only on email tabs
  const metaEl = document.getElementById('metaFields');
  if (metaEl) {
    metaEl.style.display = (metaFieldsPopulated && isEmailTab) ? 'flex' : 'none';
  }

  document.querySelectorAll('#outputTabs .tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.panel === key);
  });

  document.querySelectorAll('#channelPanels .tab-content').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-utm').classList.remove('active');

  const panel = document.getElementById(`panel-${key}`);
  if (panel) panel.classList.add('active');
}

// ---------------------------------------------------------------------------
// Panel builders
// ---------------------------------------------------------------------------

function createEmailPanelEl(key, html, blocks) {
  const div = document.createElement('div');
  div.className = 'tab-content';
  div.id = `panel-${key}`;

  const showGcBtn = (key === 'email');
  div.innerHTML = `
    <div class="tab-toolbar">
      <div class="toolbar-spacer"></div>
      <button class="toolbar-btn" id="copybtn-${key}" onclick="copyChannelHtml('${key}')">📋 Скопировать HTML</button>
      ${showGcBtn ? `<button class="toolbar-btn gc-push-btn" onclick="pushToMail('${key}')">🚀 В GetCourse</button>` : ''}
    </div>
    ${showGcBtn ? `<div class="gc-push-form" id="gcmailform-${key}" style="display:none">
      <input type="text" id="gcmailname-${key}" placeholder="Название рассылки в GetCourse">
      <button class="toolbar-btn primary-btn" onclick="confirmPushToMail('${key}')">Отправить</button>
      <button class="toolbar-btn" onclick="closeMailForm('${key}')">Отмена</button>
      <span class="gc-push-status" id="gcmailstatus-${key}"></span>
    </div>` : ''}
    <div class="email-editor-layout">
      <div class="email-preview-col">
        <div class="preview-col-label">Предпросмотр</div>
        <iframe id="preview-${key}" sandbox="allow-same-origin"></iframe>
      </div>
      <div class="email-right-col">
        <div class="block-editor-title">Блоки письма</div>
        <div class="block-list" id="blocklist-${key}"></div>
        <button class="add-block-btn" onclick="showAddBlockForm('${key}')">+ Добавить блок</button>
        <div id="addform-${key}" class="add-block-form" style="display:none">
          <select id="newtype-${key}" onchange="toggleNewBlockFields('${key}')">
            ${Object.entries(BLOCK_LABELS).map(([v,l]) => `<option value="${v}">${l}</option>`).join('')}
          </select>
          <textarea id="newtext-${key}" placeholder="Текст (каждый абзац — отдельная строка)..."></textarea>
          <div id="newcols-${key}" style="display:none;flex-direction:column;gap:6px">
            <textarea id="newcol2-${key}" placeholder="Текст второй колонки..."></textarea>
            <textarea id="newcol3-${key}" style="display:none" placeholder="Текст третьей колонки..."></textarea>
          </div>
          <div id="newbtn-${key}" style="display:none;flex-direction:column;gap:6px">
            <input type="text" id="newbtntext-${key}" placeholder="Текст кнопки">
            <input type="url"  id="newbtnurl-${key}"  placeholder="URL кнопки (https://...)">
          </div>
          <div id="newimg-${key}" style="display:none">
            <input type="url" id="newimgurl-${key}" placeholder="URL картинки">
          </div>
          <div class="add-block-actions">
            <button class="toolbar-btn primary-btn" onclick="addBlock('${key}')">Добавить</button>
            <button class="toolbar-btn" onclick="hideAddBlockForm('${key}')">Отмена</button>
          </div>
        </div>
      </div>
    </div>`;

  const iframe = div.querySelector(`#preview-${key}`);
  if (html) generatedOutputs[key] = html;
  setIframeSrc(iframe, html);

  emailBlocks[key] = blocks;
  // renderBlockEditorForPanel called after DOM insertion in rebuildTabBar

  return div;
}

function createTgHtmlPanelEl(key, content) {
  const div = document.createElement('div');
  div.className = 'tab-content';
  div.id = `panel-${key}`;

  const showGcBtnTg = GC_PUSH_TG_CHANNELS.includes(key);
  div.innerHTML = `
    <div class="tab-toolbar">
      <div class="toolbar-spacer"></div>
      <button class="toolbar-btn" onclick="copyChannelHtml('${key}')">📋 Скопировать</button>
      ${showGcBtnTg ? `<button class="toolbar-btn gc-push-btn" onclick="pushToMail('${key}')">🚀 В GetCourse</button>` : ''}
    </div>
    ${showGcBtnTg ? `<div class="gc-push-form" id="gcmailform-${key}" style="display:none">
      <input type="text" id="gcmailname-${key}" placeholder="Название рассылки в GetCourse">
      <button class="toolbar-btn primary-btn" onclick="confirmPushToMail('${key}')">Отправить</button>
      <button class="toolbar-btn" onclick="closeMailForm('${key}')">Отмена</button>
      <span class="gc-push-status" id="gcmailstatus-${key}"></span>
    </div>` : ''}
    <div class="code-preview-split">
      <textarea id="code-${key}" class="code-editor" placeholder="HTML для Telegram..."></textarea>
      <div class="preview-pane tg-preview-pane">
        <div class="tg-preview-header">Предпросмотр</div>
        <div class="tg-message-preview" id="tgpreview-${key}"></div>
      </div>
    </div>`;

  const ta = div.querySelector(`#code-${key}`);
  const preview = div.querySelector(`#tgpreview-${key}`);
  ta.value = content || '';
  preview.innerHTML = content || '';
  ta.addEventListener('input', () => { preview.innerHTML = ta.value; });

  return div;
}

function createTgBotsPanelEl(key, content) {
  const div = document.createElement('div');
  div.className = 'tab-content';
  div.id = `panel-${key}`;

  div.innerHTML = `
    <div class="tab-toolbar">
      <div class="toolbar-spacer"></div>
      <button class="toolbar-btn" onclick="copyChannelHtml('${key}')">📋 Скопировать</button>
    </div>
    <textarea id="code-${key}" class="code-editor" placeholder="Текст для ботов появится здесь..."></textarea>`;

  div.querySelector(`#code-${key}`).value = content || '';
  return div;
}

function createTgMarkdownPanelEl(key, text, links) {
  const div = document.createElement('div');
  div.className = 'tab-content';
  div.id = `panel-${key}`;

  let linksHtml = '';
  if (links && links.length) {
    linksHtml = links.map((url, i) => `
      <div class="md-link-row">
        <span class="md-link-label">Ссылка ${i + 1}</span>
        <input class="md-link-input" id="mdlink-${key}-${i}" value="${url.replace(/"/g, '&quot;')}" readonly>
        <button class="toolbar-btn" onclick="copyMdLink('mdlink-${key}-${i}')">📋</button>
      </div>`).join('');
  } else {
    linksHtml = '<div class="md-link-row" style="color:var(--text-muted)">Ссылок не найдено</div>';
  }

  div.innerHTML = `
    <div class="tab-toolbar">
      <div class="toolbar-spacer"></div>
      <button class="toolbar-btn" onclick="copyChannelHtml('${key}')">📋 Скопировать текст</button>
    </div>
    <textarea id="code-${key}" class="code-editor" placeholder="Markdown для Нейрокота..."></textarea>
    <div class="md-links-box">
      <div class="md-links-title">UTM ссылки — вставить вручную через 🔗 в редакторе Нейрокота:</div>
      ${linksHtml}
    </div>`;

  div.querySelector(`#code-${key}`).value = text || '';
  return div;
}

function copyMdLink(inputId) {
  const input = document.getElementById(inputId);
  if (!input) return;
  navigator.clipboard.writeText(input.value).catch(() => {
    input.select();
    document.execCommand('copy');
  });
}

// ---------------------------------------------------------------------------
// Copy
// ---------------------------------------------------------------------------

async function copyChannelHtml(key) {
  let text = '';
  if (EMAIL_CHANNELS.includes(key)) {
    text = generatedOutputs[key] || '';
  } else {
    const ta = document.getElementById(`code-${key}`);
    text = ta ? ta.value : '';
  }
  if (!text) { showError('Нечего копировать — сначала сгенерируйте'); return; }
  try {
    await navigator.clipboard.writeText(text);
    const btn = document.getElementById(`copybtn-${key}`) ||
      document.querySelector(`#panel-${key} .toolbar-btn`);
    flashButton(btn, '✓ Скопировано');
  } catch (_) {
    fallbackCopy(text);
  }
}

// Keep copyCode for backward-compat (used in some places)
async function copyCode(type) {
  await copyChannelHtml(type === 'tg_html' ? activeChannelKey : type);
}

// ---------------------------------------------------------------------------
// Email block editor
// ---------------------------------------------------------------------------

function renderBlockEditorForPanel(channelKey) {
  const listEl = document.getElementById(`blocklist-${channelKey}`);
  if (!listEl) return;
  const blocks = emailBlocks[channelKey] || [];
  listEl.innerHTML = '';

  if (!blocks.length) {
    listEl.innerHTML = '<div class="block-empty">Блоки появятся после генерации</div>';
    return;
  }

  blocks.forEach((block, idx) => {
    listEl.appendChild(createBlockCard(block, idx, channelKey, blocks.length));
  });
}

function createBlockCard(block, idx, channelKey, total) {
  const card = document.createElement('div');
  card.className = 'block-card';
  card.id = `blockcard-${channelKey}-${idx}`;
  const rawClass  = (block.type || 'block_white').replace('block_', '').replace(/_/g, '-');
  const typeClass = rawClass.replace(/^(\d)/, 'col-$1');
  const typeLabel = BLOCK_LABELS[block.type] || block.type;
  const preview   = escapeHtml((block.preview_text || '').substring(0, 50));
  const isFirst   = idx === 0;
  const isLast    = idx === total - 1;
  const isCta     = ['block_blue_cta', 'block_button'].includes(block.type);
  const isNoText  = ['block_spacer', 'block_button'].includes(block.type);
  const is3col    = block.type === 'block_3col_text';
  const is2colTT  = block.type === 'block_2col_text_text';

  const selectOpts = Object.entries(BLOCK_LABELS).map(([v, l]) =>
    `<option value="${v}"${block.type === v ? ' selected' : ''}>${l}</option>`
  ).join('');

  let textareasHtml;
  if (is3col) {
    textareasHtml = `
      <label class="col-label">Колонка 1</label>
      <textarea class="block-edit-textarea" placeholder="Текст первой колонки..."></textarea>
      <label class="col-label">Колонка 2</label>
      <textarea class="block-edit-textarea block-edit-col2" placeholder="Текст второй колонки..."></textarea>
      <label class="col-label">Колонка 3</label>
      <textarea class="block-edit-textarea block-edit-col3" placeholder="Текст третьей колонки..."></textarea>`;
  } else if (is2colTT) {
    textareasHtml = `
      <label class="col-label">Колонка 1</label>
      <textarea class="block-edit-textarea" placeholder="Текст первой колонки..."></textarea>
      <label class="col-label">Колонка 2</label>
      <textarea class="block-edit-textarea block-edit-col2" placeholder="Текст второй колонки..."></textarea>`;
  } else {
    textareasHtml = isNoText ? '' : `<textarea class="block-edit-textarea" placeholder="Редактируйте текст блока..."></textarea>`;
  }

  card.innerHTML = `
    <div class="block-card-header">
      <span class="drag-handle" draggable="true" title="Перетащить">⠿</span>
      <span class="block-type-badge ${typeClass}">${typeLabel}</span>
      <span class="block-preview-text">${preview}</span>
      <div class="block-card-controls">
        <button class="block-ctrl-btn" onclick="moveBlock('${channelKey}',${idx},-1)" ${isFirst ? 'disabled' : ''}>↑</button>
        <button class="block-ctrl-btn" onclick="moveBlock('${channelKey}',${idx},1)"  ${isLast  ? 'disabled' : ''}>↓</button>
        <button class="block-ctrl-btn block-edit" onclick="toggleBlockEdit('${channelKey}',${idx},this)" title="Свернуть">▲</button>
        <button class="block-ctrl-btn block-delete" onclick="deleteBlock('${channelKey}',${idx})">✕</button>
      </div>
    </div>
    <select class="block-type-select" onchange="changeBlockType('${channelKey}',${idx},this.value)">
      ${selectOpts}
    </select>
    <div class="block-edit-area" id="editarea-${channelKey}-${idx}" style="display:flex">
      <div class="format-toolbar"${isNoText ? ' style="display:none"' : ''}>
        <button type="button" class="fmt-btn" onclick="wrapFmt(this,'b')" title="Жирный"><b>Ж</b></button>
        <button type="button" class="fmt-btn" onclick="wrapFmt(this,'i')" title="Курсив"><i>К</i></button>
        <button type="button" class="fmt-btn" onclick="wrapFmt(this,'u')" title="Подчёркнутый"><u>Ч</u></button>
        <button type="button" class="fmt-btn fmt-link" onclick="wrapFmtLink(this)" title="Ссылка">🔗</button>
        <span class="fmt-sep"></span>
        <button type="button" class="fmt-btn" onclick="wrapFmtAlign(this,'left')" title="По левому краю"><svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="1" y1="3" x2="13" y2="3"/><line x1="1" y1="7" x2="9" y2="7"/><line x1="1" y1="11" x2="11" y2="11"/></svg></button>
        <button type="button" class="fmt-btn" onclick="wrapFmtAlign(this,'center')" title="По центру"><svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="1" y1="3" x2="13" y2="3"/><line x1="3" y1="7" x2="11" y2="7"/><line x1="2" y1="11" x2="12" y2="11"/></svg></button>
      </div>
      ${textareasHtml}
      ${isCta ? `<div class="btn-edit-fields">
        <input type="text" class="btn-text-field block-edit-input" placeholder="Текст кнопки">
        <input type="url" class="btn-url-field block-edit-input" placeholder="URL кнопки (https://...)">
      </div>` : ''}
      <div class="block-edit-actions">
        <button class="toolbar-btn primary-btn btn-sm" onclick="saveBlockEdit('${channelKey}',${idx})">Сохранить</button>
        <button class="toolbar-btn btn-sm" onclick="cancelBlockEdit('${channelKey}',${idx})">Закрыть</button>
      </div>
    </div>`;

  // Drag-and-drop listeners
  const handle = card.querySelector('.drag-handle');
  if (handle) {
    handle.addEventListener('dragstart', e => _dndStart(e, channelKey, idx));
    handle.addEventListener('dragend',   e => _dndEnd(e, channelKey, idx));
  }
  card.addEventListener('dragover', e => _dndOver(e, channelKey, idx));
  card.addEventListener('drop',    e => _dndDrop(e, channelKey, idx));

  // Pre-populate edit fields
  const ta = card.querySelector('.block-edit-textarea');
  if (ta) ta.value = paragraphsHtmlToText(block.paragraphs_html || '');
  const ta2 = card.querySelector('.block-edit-col2');
  if (ta2) ta2.value = paragraphsHtmlToText(block.col2_html || '');
  const ta3 = card.querySelector('.block-edit-col3');
  if (ta3) ta3.value = paragraphsHtmlToText(block.col3_html || '');
  if (isCta) {
    const btnTf = card.querySelector('.btn-text-field');
    const btnUf = card.querySelector('.btn-url-field');
    if (btnTf) btnTf.value = block.btn_text || '';
    if (btnUf) btnUf.value = block.btn_url_utm || '';
  }

  return card;
}

function _captureUiState(channelKey) {
  const panel = document.querySelector('.output-panel');
  const collapsed = new Set();
  const listEl = document.getElementById(`blocklist-${channelKey}`);
  if (listEl) {
    listEl.querySelectorAll('.block-card').forEach((card, i) => {
      const area = card.querySelector('.block-edit-area');
      if (area && area.style.display === 'none') collapsed.add(i);
    });
  }
  return { scrollTop: panel ? panel.scrollTop : 0, collapsed };
}

function _restoreUiState(channelKey, state, indexRemap) {
  const listEl = document.getElementById(`blocklist-${channelKey}`);
  if (listEl) {
    listEl.querySelectorAll('.block-card').forEach((card, i) => {
      const oldIdx = indexRemap ? (indexRemap(i) ?? i) : i;
      if (state.collapsed.has(oldIdx)) {
        const area = card.querySelector('.block-edit-area');
        const editBtn = card.querySelector('.block-edit');
        if (area) area.style.display = 'none';
        if (editBtn) editBtn.textContent = '▼';
      }
    });
  }
  const panel = document.querySelector('.output-panel');
  if (panel) requestAnimationFrame(() => { panel.scrollTop = state.scrollTop; });
}

function moveBlock(channelKey, idx, dir) {
  const blocks = emailBlocks[channelKey];
  if (!blocks) return;
  const to = idx + dir;
  if (to < 0 || to >= blocks.length) return;
  const state = _captureUiState(channelKey);
  [blocks[idx], blocks[to]] = [blocks[to], blocks[idx]];
  renderBlockEditorForPanel(channelKey);
  reassembleEmail(channelKey);
  _restoreUiState(channelKey, state, i => i === idx ? to : i === to ? idx : i);
}

function deleteBlock(channelKey, idx) {
  const blocks = emailBlocks[channelKey];
  if (!blocks) return;
  const state = _captureUiState(channelKey);
  blocks.splice(idx, 1);
  renderBlockEditorForPanel(channelKey);
  reassembleEmail(channelKey);
  _restoreUiState(channelKey, state, i => i < idx ? i : i + 1);
}

function changeBlockType(channelKey, idx, newType) {
  const blocks = emailBlocks[channelKey];
  if (!blocks || !blocks[idx]) return;
  const state = _captureUiState(channelKey);
  blocks[idx].type = newType;
  renderBlockEditorForPanel(channelKey);
  reassembleEmail(channelKey);
  _restoreUiState(channelKey, state);
}

// ---------------------------------------------------------------------------
// Drag-and-drop reordering  (placeholder approach — no flicker)
// ---------------------------------------------------------------------------

let _dragPlaceholder = null;

function _dndAutoScroll() {
  if (!_dragSrcKey) return;
  // The block list lives inside .email-right-col which has its own overflow-y: auto
  const listEl = document.getElementById(`blocklist-${_dragSrcKey}`);
  const panel = listEl ? listEl.closest('.email-right-col') : document.querySelector('.output-panel');
  if (panel && _dragMouseY > 0) {
    const r = panel.getBoundingClientRect();
    const zone = 80;
    if (_dragMouseY < r.top + zone) {
      panel.scrollTop -= 10 + 10 * (1 - (_dragMouseY - r.top) / zone);
    } else if (_dragMouseY > r.bottom - zone) {
      panel.scrollTop += 10 + 10 * (1 - (r.bottom - _dragMouseY) / zone);
    }
  }
  _dragScrollRaf = requestAnimationFrame(_dndAutoScroll);
}

function _dndStart(e, channelKey, idx) {
  _dragSrcKey = channelKey;
  _dragSrcIdx = idx;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', String(idx));

  const card = document.getElementById(`blockcard-${channelKey}-${idx}`);
  if (card) {
    e.dataTransfer.setDragImage(card, card.offsetWidth / 2, 20);

    _dragPlaceholder = document.createElement('div');
    _dragPlaceholder.className = 'drag-placeholder';
    _dragPlaceholder.style.height = card.offsetHeight + 'px';
    _dragPlaceholder.addEventListener('dragover', ev => {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
    });
    _dragPlaceholder.addEventListener('drop', ev => {
      ev.preventDefault();
      _dndCommit(channelKey);
    });

    requestAnimationFrame(() => card.classList.add('dragging'));
  }

  _dragScrollRaf = requestAnimationFrame(_dndAutoScroll);
}

function _dndOver(e, channelKey, idx) {
  if (_dragSrcKey !== channelKey || idx === _dragSrcIdx) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  if (!_dragPlaceholder) return;

  const card = document.getElementById(`blockcard-${channelKey}-${idx}`);
  if (!card || card === _dragPlaceholder) return;

  const rect = card.getBoundingClientRect();
  const insertBefore = e.clientY < rect.top + rect.height / 2;
  const list = card.parentNode;
  if (insertBefore) {
    list.insertBefore(_dragPlaceholder, card);
  } else {
    list.insertBefore(_dragPlaceholder, card.nextSibling);
  }
}

function _dndDrop(e, channelKey, idx) {
  e.preventDefault();
  _dndCommit(channelKey);
}

function _dndEnd(e, channelKey, idx) {
  _dndCleanup();
}

function _dndCommit(channelKey) {
  if (!_dragPlaceholder || _dragSrcKey !== channelKey) { _dndCleanup(); return; }

  const from = _dragSrcIdx;
  const listEl = document.getElementById(`blocklist-${channelKey}`);
  if (!listEl) { _dndCleanup(); return; }

  const allChildren = Array.from(listEl.children);
  const phIdx = allChildren.indexOf(_dragPlaceholder);
  if (phIdx < 0) { _dndCleanup(); return; }

  // Count real block-cards (not the dragging one) before the placeholder
  const to = allChildren.slice(0, phIdx).filter(
    el => el.classList.contains('block-card') && !el.classList.contains('dragging')
  ).length;

  _dndCleanup();
  if (from === to) return;

  const blocks = emailBlocks[channelKey];
  if (!blocks) return;

  const state = _captureUiState(channelKey);
  const [item] = blocks.splice(from, 1);
  blocks.splice(to, 0, item);
  renderBlockEditorForPanel(channelKey);
  reassembleEmail(channelKey);
  _restoreUiState(channelKey, state, i => {
    if (i === from) return to;
    if (from < to && i > from && i <= to) return i - 1;
    if (from > to && i >= to && i < from) return i + 1;
    return i;
  });
}

function _dndCleanup() {
  if (_dragScrollRaf) { cancelAnimationFrame(_dragScrollRaf); _dragScrollRaf = null; }
  _dragMouseY = 0;
  if (_dragPlaceholder && _dragPlaceholder.parentNode)
    _dragPlaceholder.parentNode.removeChild(_dragPlaceholder);
  _dragPlaceholder = null;
  if (_dragSrcKey && _dragSrcIdx >= 0) {
    const c = document.getElementById(`blockcard-${_dragSrcKey}-${_dragSrcIdx}`);
    if (c) c.classList.remove('dragging');
  }
  _dragSrcKey = null;
  _dragSrcIdx = -1;
}

// ---------------------------------------------------------------------------
// Block inline editing
// ---------------------------------------------------------------------------

function toggleBlockEdit(channelKey, idx, btnEl) {
  const area = document.getElementById(`editarea-${channelKey}-${idx}`);
  if (!area) return;
  const isOpen = area.style.display !== 'none';
  area.style.display = isOpen ? 'none' : 'flex';
  if (btnEl) btnEl.textContent = isOpen ? '▼' : '▲';
}

function cancelBlockEdit(channelKey, idx) {
  const area = document.getElementById(`editarea-${channelKey}-${idx}`);
  if (!area) return;
  const block = (emailBlocks[channelKey] || [])[idx];
  if (block) {
    const ta = area.querySelector('.block-edit-textarea');
    if (ta) ta.value = paragraphsHtmlToText(block.paragraphs_html || '');
    const ta2 = area.querySelector('.block-edit-col2');
    if (ta2) ta2.value = paragraphsHtmlToText(block.col2_html || '');
    const ta3 = area.querySelector('.block-edit-col3');
    if (ta3) ta3.value = paragraphsHtmlToText(block.col3_html || '');
    if (['block_blue_cta', 'block_button'].includes(block.type)) {
      const btnTf = area.querySelector('.btn-text-field');
      const btnUf = area.querySelector('.btn-url-field');
      if (btnTf) btnTf.value = block.btn_text || '';
      if (btnUf) btnUf.value = block.btn_url_utm || '';
    }
  }
  area.style.display = 'none';
  const editBtn = area.closest('.block-card')?.querySelector('.block-edit');
  if (editBtn) editBtn.textContent = '▼';
}

function saveBlockEdit(channelKey, idx) {
  const area = document.getElementById(`editarea-${channelKey}-${idx}`);
  if (!area) return;

  const block = (emailBlocks[channelKey] || [])[idx];
  if (!block) return;

  const isNoText = ['block_spacer', 'block_button'].includes(block.type);
  if (!isNoText) {
    const ta = area.querySelector('.block-edit-textarea');
    const text = ta ? ta.value : '';
    const isBlue = ['block_blue_cta', 'block_blue_text'].includes(block.type);
    block.paragraphs_html = makeParaHtml(text, isBlue ? '#ffffff' : '#333333');
    block.preview_text = text.replace(/<[^>]+>/g, '').trim().substring(0, 80);
  }

  if (['block_2col_text_text', 'block_3col_text'].includes(block.type)) {
    const ta2 = area.querySelector('.block-edit-col2');
    if (ta2) block.col2_html = makeParaHtml(ta2.value, '#333333');
  }
  if (block.type === 'block_3col_text') {
    const ta3 = area.querySelector('.block-edit-col3');
    if (ta3) block.col3_html = makeParaHtml(ta3.value, '#333333');
  }

  if (['block_blue_cta', 'block_button'].includes(block.type)) {
    const btnTf = area.querySelector('.btn-text-field');
    const btnUf = area.querySelector('.btn-url-field');
    if (btnTf) { block.btn_text = btnTf.value; block.preview_text = btnTf.value.substring(0, 50); }
    if (btnUf) block.btn_url_utm = btnUf.value;
  }

  // Update just the preview text in-place — no full DOM rebuild, no scroll jump
  const blockCard = document.getElementById(`blockcard-${channelKey}-${idx}`);
  if (blockCard) {
    const previewEl = blockCard.querySelector('.block-preview-text');
    if (previewEl) previewEl.textContent = (block.preview_text || '').substring(0, 50);
  }
  reassembleEmail(channelKey);
}

function paragraphsHtmlToText(html) {
  if (!html) return '';
  const div = document.createElement('div');
  div.innerHTML = html;
  return [...div.querySelectorAll('p')]
    .filter(p => {
      const style = p.getAttribute('style') || '';
      const txt = p.textContent.trim();
      if (style.includes('font-size:6px') || style.includes('font-size:4px')) return false;
      return true;
    })
    .map(p => {
      const style = p.getAttribute('style') || '';
      const txt = p.textContent.trim();
      if (style.includes('font-size:8px') && (!txt || txt === ' ')) return '';
      if (!txt || txt === ' ') return null;
      const inner = extractCleanInlineHtml(p);
      return style.includes('text-align:center') ? `<center>${inner}</center>` : inner;
    })
    .filter(l => l !== null)
    .join('\n');
}

// Extract innerHTML keeping only safe inline tags, stripping all attrs except <a href>
function extractCleanInlineHtml(el) {
  const KEEP = new Set(['b', 'i', 'u', 's', 'code', 'a', 'strong', 'em']);
  let result = '';
  for (const node of el.childNodes) {
    if (node.nodeType === Node.TEXT_NODE) {
      result += node.textContent;
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      const tag = node.tagName.toLowerCase();
      const inner = extractCleanInlineHtml(node);
      if (KEEP.has(tag)) {
        const t = tag === 'strong' ? 'b' : tag === 'em' ? 'i' : tag;
        if (t === 'a') {
          const href = node.getAttribute('href') || '#';
          result += `<a href="${href}">${inner}</a>`;
        } else {
          result += `<${t}>${inner}</${t}>`;
        }
      } else {
        result += inner;
      }
    }
  }
  return result;
}

// Sanitize user-typed inline HTML: keep safe tags only, apply link color to <a>
function sanitizeInlineHtml(html, linkColor) {
  const div = document.createElement('div');
  div.innerHTML = html;
  _sanitizeInlineNode(div, linkColor);
  return div.innerHTML;
}

function _sanitizeInlineNode(node, linkColor) {
  const KEEP = new Set(['b', 'strong', 'i', 'em', 'u', 's', 'code', 'a', 'br']);
  for (const child of [...node.childNodes]) {
    if (child.nodeType === Node.TEXT_NODE) continue;
    if (child.nodeType !== Node.ELEMENT_NODE) { child.remove(); continue; }
    const tag = child.tagName.toLowerCase();
    if (KEEP.has(tag)) {
      const href = tag === 'a' ? (child.getAttribute('href') || '#') : null;
      while (child.attributes.length) child.removeAttribute(child.attributes[0].name);
      if (tag === 'a') {
        child.setAttribute('href', href);
        child.setAttribute('target', '_blank');
        child.setAttribute('style', `color:${linkColor};text-decoration:underline`);
      }
      _sanitizeInlineNode(child, linkColor);
    } else {
      while (child.firstChild) node.insertBefore(child.firstChild, child);
      child.remove();
    }
  }
}

// Wrap selected text in textarea with open/close tags
function wrapSelectionWith(area, open, close) {
  const start = area.selectionStart;
  const end   = area.selectionEnd;
  if (start === end) return;
  area.setRangeText(open + area.value.substring(start, end) + close, start, end, 'end');
  area.focus();
}

function _getFocusedTextarea(btn) {
  const editArea = btn.closest('.block-edit-area');
  const active = document.activeElement;
  if (active && active.classList.contains('block-edit-textarea') && editArea.contains(active)) {
    return active;
  }
  return editArea.querySelector('.block-edit-textarea');
}

// Called by format toolbar buttons — find nearest textarea via DOM
function wrapFmt(btn, tag) {
  const area = _getFocusedTextarea(btn);
  if (!area) return;
  wrapSelectionWith(area, `<${tag}>`, `</${tag}>`);
}

function wrapFmtLink(btn) {
  const area = _getFocusedTextarea(btn);
  if (!area) return;
  const start    = area.selectionStart;
  const end      = area.selectionEnd;
  const selected = area.value.substring(start, end);
  const url = prompt('URL ссылки:', 'https://');
  if (!url) return;
  const wrapped = `<a href="${url}">${selected || url}</a>`;
  area.setRangeText(wrapped, start, end, 'end');
  area.focus();
}

function wrapFmtAlign(btn, align) {
  const area = _getFocusedTextarea(btn);
  if (!area) return;
  const val = area.value;
  const selStart = area.selectionStart;
  const selEnd   = area.selectionEnd;
  const lineStart = val.lastIndexOf('\n', selStart - 1) + 1;
  let lineEnd = val.indexOf('\n', selEnd);
  if (lineEnd === -1) lineEnd = val.length;
  const lines = val.slice(lineStart, lineEnd).split('\n').map(line => {
    const t = line.trim();
    if (!t) return line;
    const isCenter = t.startsWith('<center>') && t.endsWith('</center>');
    const content = isCenter ? t.slice(8, -9) : t;
    return align === 'center' ? `<center>${content}</center>` : content;
  }).join('\n');
  area.setRangeText(lines, lineStart, lineEnd, 'end');
  area.focus();
}

// ---------------------------------------------------------------------------
// Add block form
// ---------------------------------------------------------------------------

function showAddBlockForm(channelKey) {
  const form = document.getElementById(`addform-${channelKey}`);
  if (form) {
    form.style.display = 'flex';
    toggleNewBlockFields(channelKey);
  }
}

function hideAddBlockForm(channelKey) {
  const form = document.getElementById(`addform-${channelKey}`);
  if (!form) return;
  form.style.display = 'none';
  ['newtext', 'newcol2', 'newcol3', 'newbtntext', 'newbtnurl', 'newimgurl'].forEach(id => {
    const el = document.getElementById(`${id}-${channelKey}`);
    if (el) el.value = '';
  });
}

function toggleNewBlockFields(channelKey) {
  const type    = document.getElementById(`newtype-${channelKey}`)?.value || '';
  const cols    = document.getElementById(`newcols-${channelKey}`);
  const btn     = document.getElementById(`newbtn-${channelKey}`);
  const img     = document.getElementById(`newimg-${channelKey}`);
  const text    = document.getElementById(`newtext-${channelKey}`);
  const col3    = document.getElementById(`newcol3-${channelKey}`);

  if (cols) cols.style.display   = ['block_2col_text_text', 'block_3col_text'].includes(type) ? 'flex' : 'none';
  if (col3) col3.style.display   = type === 'block_3col_text' ? 'block' : 'none';
  if (btn)  btn.style.display    = ['block_blue_cta', 'block_button'].includes(type) ? 'flex' : 'none';
  if (img)  img.style.display    = ['block_image', 'block_2col_img_text', 'block_2col_text_img'].includes(type) ? 'block' : 'none';
  if (text) text.style.display   = ['block_spacer', 'block_button'].includes(type) ? 'none' : 'block';
  if (text) {
    const label2 = document.getElementById(`newcols-${channelKey}`)?.querySelector('textarea');
    if (label2) label2.placeholder = 'Текст второй колонки...';
  }
  if (text) text.placeholder = type === 'block_2col_text_text' || type === 'block_3col_text' ? 'Текст первой колонки...' :
                                type === 'block_2col_img_text' ? 'Текст (правая колонка)...' :
                                type === 'block_2col_text_img' ? 'Текст (левая колонка)...' :
                                'Текст (каждый абзац — отдельная строка)...';
}

function makeParaHtml(text, color) {
  const linkColor = color === '#ffffff' ? '#e1fb52' : '#1445ea';
  const pBase = `margin:0 0 10px 0;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;line-height:27px;color:${color};font-size:18px`;
  const spacer = `<p style="margin:0;font-size:8px;line-height:16px">&nbsp;</p>`;
  return text.split('\n')
    .map(l => {
      if (!l.trim()) return spacer;
      const t = l.trim();
      const isCenter = t.startsWith('<center>') && t.endsWith('</center>');
      const content = isCenter ? t.slice(8, -9) : t;
      const pStyle = isCenter ? pBase + ';text-align:center' : pBase;
      return `<p style="${pStyle}">${sanitizeInlineHtml(content, linkColor)}</p>`;
    })
    .join('\n');
}

function addBlock(channelKey) {
  const type     = document.getElementById(`newtype-${channelKey}`)?.value || 'block_white';
  const rawText  = document.getElementById(`newtext-${channelKey}`)?.value.trim() || '';
  const rawCol2  = document.getElementById(`newcol2-${channelKey}`)?.value.trim() || '';
  const rawCol3  = document.getElementById(`newcol3-${channelKey}`)?.value.trim() || '';
  const btnText  = document.getElementById(`newbtntext-${channelKey}`)?.value.trim() || '';
  const btnUrl   = document.getElementById(`newbtnurl-${channelKey}`)?.value.trim() || '#';
  const imgUrl   = document.getElementById(`newimgurl-${channelKey}`)?.value.trim() || '';

  if (!['block_spacer', 'block_button'].includes(type) && !rawText && !imgUrl) {
    showError('Введите текст или URL картинки');
    return;
  }
  if (type === 'block_button' && !btnText) {
    showError('Введите текст кнопки');
    return;
  }

  const isBlue = ['block_blue_cta', 'block_blue_text'].includes(type);
  const color  = isBlue ? '#ffffff' : '#333333';

  const newBlock = {
    type,
    paragraphs_html: makeParaHtml(rawText, color),
    col2_html:  rawCol2 ? makeParaHtml(rawCol2, '#333333') : '',
    col3_html:  rawCol3 ? makeParaHtml(rawCol3, '#333333') : '',
    btn_text:   btnText,
    btn_url_utm: btnUrl,
    image_url:  imgUrl,
    height:     20,
    preview_text: (rawText || imgUrl || 'Отступ').substring(0, 80),
  };

  if (!emailBlocks[channelKey]) emailBlocks[channelKey] = [];
  emailBlocks[channelKey].push(newBlock);

  const savedScrollY = window.scrollY;
  hideAddBlockForm(channelKey);
  renderBlockEditorForPanel(channelKey);
  reassembleEmail(channelKey);
  requestAnimationFrame(() => window.scrollTo(0, savedScrollY));
}

// ---------------------------------------------------------------------------
// Reassemble email
// ---------------------------------------------------------------------------

async function reassembleEmail(channelKey) {
  const blocks = emailBlocks[channelKey];
  if (!blocks || !blocks.length) return;

  const subject = document.getElementById('subjectField')?.value || '';
  const images  = getImageUrls();

  try {
    const resp = await fetch('/api/assemble-email', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ blocks, subject, images }),
    });
    const data = await resp.json();
    if (data.html) {
      generatedOutputs[channelKey] = data.html;
      const iframe = document.getElementById(`preview-${channelKey}`);
      if (iframe) {
        const savedScroll = iframe.contentWindow?.scrollY ?? 0;
        if (savedScroll > 0) {
          iframe.onload = () => {
            iframe.contentWindow?.scrollTo(0, savedScroll);
            iframe.onload = null;
          };
        }
        setIframeSrc(iframe, data.html);
      }
    }
  } catch (e) {
    showError('Ошибка сборки: ' + e.message);
  }
}

// ---------------------------------------------------------------------------
// Push to GetCourse
// ---------------------------------------------------------------------------

function _gcContent(channelKey) {
  if (EMAIL_CHANNELS.includes(channelKey)) return generatedOutputs[channelKey] || '';
  return document.getElementById(`code-${channelKey}`)?.value || '';
}

function pushToGC(channelKey) {
  const form = document.getElementById(`gcform-${channelKey}`);
  const nameInput = document.getElementById(`gcname-${channelKey}`);
  const subject = document.getElementById('subjectField')?.value || '';
  if (!_gcContent(channelKey)) {
    showError('Сначала сгенерируйте содержимое');
    return;
  }
  nameInput.value = subject;
  form.style.display = 'flex';
  nameInput.focus();
  nameInput.select();
}

async function confirmPushToGC(channelKey) {
  const name = document.getElementById(`gcname-${channelKey}`)?.value.trim();
  const subject = document.getElementById('subjectField')?.value || '';
  const content = _gcContent(channelKey);
  const statusEl = document.getElementById(`gcstatus-${channelKey}`);

  if (!name) { statusEl.textContent = 'Введите название'; return; }
  if (!content) { statusEl.textContent = 'Нет содержимого'; return; }

  statusEl.textContent = 'Сохраняю...';
  try {
    const resp = await fetch('/api/push-to-gc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, subject, html: content, channel_key: channelKey }),
    });
    const data = await resp.json();
    if (data.ok) {
      statusEl.textContent = `✓ Добавлено! В очереди: ${data.total}`;
      document.getElementById(`gcform-${channelKey}`).style.display = 'none';
    } else {
      statusEl.textContent = '⚠ ' + (data.error || 'Ошибка');
    }
  } catch (e) {
    statusEl.textContent = '⚠ ' + e.message;
  }
}

// ---------------------------------------------------------------------------
// Push to mail.zerocoder.info → GC drafts
// ---------------------------------------------------------------------------

const _CH_TAG = { tg_gc: 'tg', max: 'max' };

function buildMailingName(channelKey) {
  const campaign = document.getElementById('utmCampaign')?.value.trim() || '';
  const date     = document.getElementById('utmDate')?.value.trim() || '';
  const title    = (parsedData.doc_title || '').trim();
  const chTag    = _CH_TAG[channelKey];
  let name = '[announce]';
  if (chTag) name += `[${chTag}]`;
  if (campaign) name += `[${campaign}]`;
  if (date)     name += `[${date}]`;
  if (title)    name += ` ${title}`;
  return name.trim();
}

function pushToMail(channelKey) {
  if (!_gcContent(channelKey)) { showError('Сначала сгенерируйте содержимое'); return; }
  const nameInput = document.getElementById(`gcmailname-${channelKey}`);
  nameInput.value = buildMailingName(channelKey);
  document.getElementById(`gcmailstatus-${channelKey}`).textContent = '';
  document.getElementById(`gcmailform-${channelKey}`).style.display = 'flex';
  nameInput.focus();
  nameInput.select();
}

function closeMailForm(channelKey) {
  const form = document.getElementById(`gcmailform-${channelKey}`);
  if (form) form.style.display = 'none';
}

async function confirmPushToMail(channelKey) {
  const name = document.getElementById(`gcmailname-${channelKey}`)?.value.trim();
  const subject = document.getElementById('subjectField')?.value || '';
  const preheader = document.getElementById('previewField')?.value.trim() || '';
  const senderName = (parsedData.sender || '').trim() || 'Университет Зерокодер';
  const content = _gcContent(channelKey);
  const statusEl = document.getElementById(`gcmailstatus-${channelKey}`);
  const date = document.getElementById('utmDate')?.value.trim() || '';

  if (!name) { statusEl.textContent = 'Введите название'; return; }
  if (!content) { statusEl.textContent = 'Нет содержимого'; return; }

  statusEl.textContent = '⏳ Отправляю...';
  try {
    const resp = await fetch('/api/push-to-mail', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name, subject, html: content, channel_key: channelKey,
        date_tag: date ? `web-${date}` : '',
        campaign: document.getElementById('utmCampaign')?.value.trim() || '',
        preheader, sender_name: senderName,
      }),
    });
    const data = await resp.json();
    if (data.ok) {
      statusEl.textContent = '⏳ Ожидаю черновик...';
      pollJobForUrl(data.job_id, statusEl, channelKey);
    } else {
      statusEl.textContent = '⚠ ' + (data.error || 'Ошибка');
    }
  } catch (e) {
    statusEl.textContent = '⚠ ' + e.message;
  }
}

async function pollJobForUrl(jobId, statusEl, channelKey) {
  const maxAttempts = 30;
  for (let i = 0; i < maxAttempts; i++) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const r = await fetch(`/api/job-status/${jobId}`);
      const d = await r.json();
      if (d.gc_url) {
        statusEl.innerHTML = `✓ Создано! <a href="${d.gc_url}" target="_blank" style="color:#a78bfa;text-decoration:underline">Открыть черновик →</a>`;
        return;
      }
      if (d.status && !['pending', 'processing', 'creating'].includes(d.status)) {
        statusEl.textContent = '✓ Создано!';
        return;
      }
    } catch (_) {}
  }
  statusEl.textContent = '✓ Создано!';
}

// ---------------------------------------------------------------------------
// TG variant selector (global)
// ---------------------------------------------------------------------------

function setupTgVariants(variants) {
  tgVariants = variants || [];
  // Selector removed; last TG variant used by default
  if (tgVariants.length > 0) {
    parsedData.tg_html = tgVariants[tgVariants.length - 1].html;
  }
}

async function switchTgVariant() {
  const prevKey = activeChannelKey;
  const idx = parseInt(document.getElementById('tgVariantSelect').value, 10);
  if (!tgVariants[idx]) return;
  parsedData.tg_html = tgVariants[idx].html;
  try {
    await _doGenerate();
    switchTabPanel(prevKey);
  } catch (e) { showError('Ошибка: ' + e.message); }
}

// ---------------------------------------------------------------------------
// UTM generator
// ---------------------------------------------------------------------------

async function generateUTM() {
  const baseUrl  = document.getElementById('utmBaseUrl').value.trim();
  if (!baseUrl) { showError('Введите базовую ссылку'); return; }

  const campaign = document.getElementById('utmCampaign').value.trim();
  const date     = document.getElementById('utmDate').value.trim();
  const segment  = parsedData.segment || '';

  try {
    const resp = await fetch('/api/generate-utm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: baseUrl, campaign, date, segment }),
    });
    const data = await resp.json();
    if (data.error) { showError(data.error); return; }
    renderUtmResults(data);
  } catch (e) {
    showError('Ошибка: ' + e.message);
  }
}

function renderUtmResults(data, containerEl, append) {
  const container = containerEl || document.getElementById('utmResults');
  if (!append) container.innerHTML = '';
  const checked = new Set(getCheckedChannels());
  for (const [key, info] of Object.entries(data)) {
    if (!checked.has(key)) continue;
    const row = document.createElement('div');
    row.className = 'utm-row';
    const safeUrl = info.url.replace(/"/g, '&quot;');
    row.innerHTML = `
      <span class="utm-channel">${escapeHtml(info.name)}</span>
      <input type="text" value="${safeUrl}" readonly class="utm-url">
      <button onclick="copyUtm(this)" data-url="${safeUrl}" title="Скопировать">📋</button>`;
    container.appendChild(row);
  }
}

async function autoFillUtmFromDocLinks(docUrls) {
  const campaign = document.getElementById('utmCampaign').value.trim();
  const date     = document.getElementById('utmDate').value.trim();
  const segment  = parsedData.segment || '';
  const container = document.getElementById('utmResults');
  container.innerHTML = '';

  if (!docUrls || docUrls.length === 0) {
    container.innerHTML = '<div style="color:var(--text-muted);padding:8px 0">Внешних ссылок в документе не найдено — введите базовую ссылку вручную выше</div>';
    return;
  }

  for (const url of docUrls) {
    try {
      const resp = await fetch('/api/generate-utm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, campaign, date, segment }),
      });
      const data = await resp.json();
      if (data.error) continue;
      if (docUrls.length > 1) {
        const hdr = document.createElement('div');
        hdr.className = 'utm-source-label';
        hdr.textContent = url;
        container.appendChild(hdr);
      }
      renderUtmResults(data, container, true);
    } catch (_) { /* silent */ }
  }
  if (docUrls.length > 0) document.getElementById('utmBaseUrl').value = docUrls[0];
}

// ---------------------------------------------------------------------------
// Preview
// ---------------------------------------------------------------------------

function setIframeSrc(iframe, html) {
  if (iframe) iframe.srcdoc = html;
}

// ---------------------------------------------------------------------------
// Copy helpers
// ---------------------------------------------------------------------------

async function copyField(fieldId) {
  const val = document.getElementById(fieldId).value;
  if (!val) return;
  try {
    await navigator.clipboard.writeText(val);
    flashButton(
      document.querySelector(`button[onclick="copyField('${fieldId}')"]`),
      '✓'
    );
  } catch (_) { fallbackCopy(val); }
}

async function copyUtm(btn) {
  const url = btn.dataset.url;
  try {
    await navigator.clipboard.writeText(url);
    flashButton(btn, '✓');
  } catch (_) { fallbackCopy(url); }
}

function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;top:-9999px;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
}

function flashButton(btn, msg) {
  if (!btn) return;
  const orig = btn.textContent;
  btn.textContent = msg;
  setTimeout(() => { btn.textContent = orig; }, 2000);
}

// ---------------------------------------------------------------------------
// Image fields
// ---------------------------------------------------------------------------

function addImageField() {
  const container = document.getElementById('imageFields');
  const count = container.querySelectorAll('.image-field').length + 1;
  const div = document.createElement('div');
  div.className = 'image-field';
  div.style.marginTop = '6px';
  div.innerHTML = `
    <input type="url" placeholder="URL картинки ${count}">
    <button class="remove-btn" onclick="removeImageField(this)" title="Удалить">✕</button>`;
  container.appendChild(div);
}

function removeImageField(btn) {
  const field     = btn.closest('.image-field');
  const container = document.getElementById('imageFields');
  if (container.querySelectorAll('.image-field').length > 1) {
    field.remove();
  } else {
    field.querySelector('input').value = '';
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getCheckedChannels() {
  return [...document.querySelectorAll('input[name="channel"]:checked')]
    .map(el => el.value);
}

function getImageUrls() {
  return [...document.querySelectorAll('#imageFields input')]
    .map(el => el.value.trim())
    .filter(Boolean);
}

function fillUploadedImageUrls(urls) {
  const container = document.getElementById('imageFields');
  if (!container) return;

  const inputs = [...container.querySelectorAll('.image-field input')];

  urls.forEach((url, i) => {
    if (i < inputs.length) {
      // Fill existing field
      inputs[i].value = url;
    } else {
      // Add new field and fill it
      addImageField();
      const newInputs = [...container.querySelectorAll('.image-field input')];
      newInputs[newInputs.length - 1].value = url;
    }
  });
}

function setButtonLoading(btn, text) {
  btn.disabled = true;
  btn.dataset.origText = btn.textContent;
  btn.textContent = text;
}

function resetButton(btn, text) {
  btn.disabled = false;
  btn.textContent = text || btn.dataset.origText || text;
}

function showError(msg) {
  alert(msg);
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

document.addEventListener('DOMContentLoaded', () => {
  // TG preview update for any dynamically created TG panel is handled per-panel
});

// Track mouse Y during any drag via dragover on document — fires reliably unlike the 'drag' event
document.addEventListener('dragover', e => { _dragMouseY = e.clientY; });
