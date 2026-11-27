'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const BUSY_STATES = new Set(['connecting', 'scanning', 'running', 'paused', 'stopping']);
  const SELL_TIME_RE = /^(\d{4}-\d{2}-\d{2}[ T])?\d{1,2}:\d{2}(:\d{2}(\.\d{1,6})?)?$/;

  let snapshot = null;
  let viewApp = '';
  let workspace = '';
  let dirty = false;
  let clearStoredKeyRequested = false;
  let actionPending = false;
  let coordSignature = '';
  let inspectSignature = '';
  let modelsSignature = '';

  const notify = message => window.dispatchEvent(new CustomEvent('miaotask:error', {detail: message}));
  const pollNow = () => window.dispatchEvent(new Event('miaotask:poll-now'));

  function isDamaiVisible() {
    if (viewApp === 'damai') return true;
    if (!viewApp && snapshot?.app === 'damai') return true;
    return false;
  }

  function isBusy() {
    return Boolean(
      actionPending ||
      snapshot?.busy ||
      snapshot?.plugins_busy ||
      BUSY_STATES.has(snapshot?.status)
    );
  }

  function parseOptionalIndex(raw) {
    const trimmed = String(raw ?? '').trim();
    if (!trimmed) return null;
    const num = Number(trimmed);
    return Number.isInteger(num) ? num : NaN;
  }

  function parseViewerNames(raw) {
    const text = String(raw ?? '').trim();
    if (!text) return [];
    return [...new Set(text.split(/[,，;；\s]+/u).map(s => s.trim()).filter(Boolean))];
  }

  function collectOptions() {
    return {
      session_text: String($('damai-session-text').value || '').trim(),
      session_index: parseOptionalIndex($('damai-session-index').value),
      price_text: String($('damai-price-text').value || '').trim(),
      price_index: parseOptionalIndex($('damai-price-index').value),
      sell_time: String($('damai-sell-time').value || '').trim().replace('T', ' '),
      lead_ms: Number($('damai-lead-ms').value || 0),
      order_mode: $('damai-order-mode').value || 'quick_order',
      viewer_count: Number($('damai-viewer-count').value || 0),
      viewer_names: parseViewerNames($('damai-viewer-names').value),
      submit_order: Boolean($('damai-submit-order').checked),
      jitter_min_ms: Number($('damai-jitter-min').value || 0),
      jitter_max_ms: Number($('damai-jitter-max').value || 0),
      max_attempts: Number($('damai-max-attempts').value || 40),
      page_timeout_s: Number($('damai-page-timeout').value || 2.5),
      agree_tolerance_px: Number($('damai-agree-tolerance').value || 45),
      auto_resume_verify: Boolean($('damai-auto-resume-verify').checked),
      verify_wait_s: Number($('damai-verify-wait').value || 90),
      auto_select_session_on_calibrate: Boolean($('damai-auto-select-session').checked),
      llm_enabled: Boolean($('damai-llm-enabled').checked),
      llm_base_url: String($('damai-llm-base-url').value || '').trim(),
      llm_model: String($('damai-llm-model').value || 'claude-sonnet-4-6').trim(),
      llm_timeout_s: 15.0,
      ocr_min_conf: Number($('damai-ocr-min-conf').value || 0.3),
    };
  }

  function collectSecrets() {
    const typed = String($('damai-llm-api-key').value || '').trim();
    if (typed) return {llm_api_key: typed};
    if (clearStoredKeyRequested) return {llm_api_key: ''};
    return null;
  }

  function markSaved() {
    dirty = false;
    clearStoredKeyRequested = false;
    $('damai-llm-api-key').value = '';
  }

  function validate() {
    const opts = collectOptions();
    if (opts.session_index !== null && (!Number.isInteger(opts.session_index) || opts.session_index < 0 || opts.session_index > 50)) {
      notify('场次序号须为 0～50 的整数或留空');
      return false;
    }
    if (opts.price_index !== null && (!Number.isInteger(opts.price_index) || opts.price_index < 0 || opts.price_index > 50)) {
      notify('票档序号须为 0～50 的整数或留空');
      return false;
    }
    if (opts.sell_time && !SELL_TIME_RE.test(opts.sell_time)) {
      notify('开售时间格式须为 YYYY-MM-DD HH:MM:SS 或 HH:MM:SS，留空则自动读取详情页或立即开抢');
      return false;
    }
    if (opts.jitter_min_ms > opts.jitter_max_ms) {
      notify('最小点击抖动不能大于最大点击抖动');
      return false;
    }
    if (opts.llm_enabled && opts.llm_model.toLowerCase().startsWith('gemini-')) {
      notify('Gemini 系列视觉模型坐标偏差超过 300px，请改用 claude-sonnet-4-6 或关闭视觉大模型校准');
      return false;
    }
    return true;
  }

  function applyOptions(opts) {
    if (!opts || typeof opts !== 'object') return;
    $('damai-session-text').value = opts.session_text ?? '';
    $('damai-session-index').value = opts.session_index === null || opts.session_index === undefined ? '' : String(opts.session_index);
    $('damai-price-text').value = opts.price_text ?? '';
    $('damai-price-index').value = opts.price_index === null || opts.price_index === undefined ? '' : String(opts.price_index);
    $('damai-sell-time').value = opts.sell_time ?? '';
    $('damai-lead-ms').value = opts.lead_ms ?? 80;
    $('damai-order-mode').value = opts.order_mode || 'quick_order';
    $('damai-viewer-count').value = opts.viewer_count ?? 1;
    $('damai-viewer-names').value = Array.isArray(opts.viewer_names) ? opts.viewer_names.join('；') : '';
    $('damai-submit-order').checked = Boolean(opts.submit_order);
    $('damai-jitter-min').value = opts.jitter_min_ms ?? 120;
    $('damai-jitter-max').value = opts.jitter_max_ms ?? 280;
    $('damai-max-attempts').value = opts.max_attempts ?? 40;
    $('damai-page-timeout').value = opts.page_timeout_s ?? 2.5;
    $('damai-agree-tolerance').value = opts.agree_tolerance_px ?? 45;
    $('damai-auto-resume-verify').checked = opts.auto_resume_verify !== false;
    $('damai-verify-wait').value = opts.verify_wait_s ?? 90;
    $('damai-auto-select-session').checked = opts.auto_select_session_on_calibrate !== false;
    $('damai-llm-enabled').checked = Boolean(opts.llm_enabled);
    $('damai-llm-base-url').value = opts.llm_base_url ?? '';
    $('damai-llm-model').value = opts.llm_model || 'claude-sonnet-4-6';
    $('damai-ocr-min-conf').value = opts.ocr_min_conf ?? 0.3;
  }

  async function ensureDamaiSelected(device, token) {
    if (snapshot?.app === 'damai' && snapshot?.activity === 'ticket_grab') return;
    await MiaoAPI.request('/api/select_pipeline', {
      method: 'POST',
      token,
      device,
      payload: {activities: [{app: 'damai', activity: 'ticket_grab'}]},
    });
    for (let i = 0; i < 12; i++) {
      const latest = await MiaoAPI.request('/api/state', {device});
      snapshot = latest;
      if (latest.app === 'damai' && !latest.busy && latest.status !== 'scanning') return;
      await new Promise(resolve => setTimeout(resolve, 80));
    }
  }

  async function runDamaiRequest(kind) {
    if (!snapshot || isBusy()) return;
    if (['save', 'calibrate', 'models'].includes(kind) && !validate()) return;
    if (['inspect', 'calibrate'].includes(kind) && !snapshot.connected) {
      notify('请先通过 USB 连接手机，并在手机大麦打开目标页面');
      return;
    }
    const device = MiaoAPI.device;
    const token = snapshot.token;
    actionPending = true;
    renderControls();
    try {
      await ensureDamaiSelected(device, token);
      const secrets = collectSecrets();
      const configPayload = {activity_options: collectOptions()};
      if (kind === 'save') {
        await MiaoAPI.request('/api/configure', {
          method: 'POST',
          token,
          device,
          payload: {
            app: 'damai',
            activity: 'ticket_grab',
            config: configPayload,
            ...(secrets ? {activity_secrets: secrets} : {}),
          },
        });
        markSaved();
        notify('大麦抢票参数已保存');
      } else if (kind === 'inspect') {
        await MiaoAPI.request('/api/scan', {
          method: 'POST',
          token,
          device,
          payload: {app: 'damai', activity: 'ticket_grab', auto_open: false},
        });
      } else if (kind === 'calibrate') {
        await MiaoAPI.request('/api/activity_action', {
          method: 'POST',
          token,
          device,
          payload: {
            app: 'damai',
            activity: 'ticket_grab',
            name: 'calibrate',
            config: configPayload,
            ...(secrets ? {activity_secrets: secrets} : {}),
          },
        });
        markSaved();
      } else if (kind === 'clear_cache') {
        await MiaoAPI.request('/api/activity_action', {
          method: 'POST',
          token,
          device,
          payload: {app: 'damai', activity: 'ticket_grab', name: 'clear_cache'},
        });
      } else if (kind === 'models') {
        await MiaoAPI.request('/api/activity_action', {
          method: 'POST',
          token,
          device,
          payload: {
            app: 'damai',
            activity: 'ticket_grab',
            name: 'models',
            config: configPayload,
            ...(secrets ? {activity_secrets: secrets} : {}),
          },
        });
        if (secrets) markSaved();
      }
      pollNow();
    } catch (err) {
      notify(err.message || '大麦操作失败');
    } finally {
      actionPending = false;
      renderControls();
    }
  }

  function ruleSummary(target) {
    if (target.index !== null && target.index !== undefined) return `按序号 #${target.index}`;
    const kws = (target.keywords || []).filter(Boolean);
    return kws.length ? kws.join(' / ') : '默认首项';
  }

  function statusLabel(target) {
    if (target.stale) return {cls: 'warn', text: '参数已改 · 待重校'};
    if (!target.cached || !target.point) return {cls: 'idle', text: '未缓存 · 实时 XML'};
    const srcMap = {
      agree: '多源一致',
      single: '单源定位',
      conflict: '冲突优选',
      native_live: '实时核定',
      native_relocated: '开售重定位',
    };
    return {cls: 'ok', text: `已就绪 · ${srcMap[target.source] || target.source || '缓存'}`};
  }

  function renderCoords(actState) {
    const list = actState?.targets || [];
    const sig = JSON.stringify([list, actState?.last_calibration]);
    if (sig === coordSignature) return;
    coordSignature = sig;

    const tbody = $('damai-coord-rows');
    tbody.replaceChildren();
    if (!list.length) {
      const tr = el('tr');
      const td = el('td', 'damai-coord-empty', '切换到大麦抢票活动后显示各环节目标坐标状态');
      td.setAttribute('colspan', '4');
      tr.append(td);
      tbody.append(tr);
      return;
    }
    const pageNames = {detail: '详情页', sku: 'SKU 票档页', order: '订单页'};
    for (const item of list) {
      const tr = el('tr');
      const nameCell = el('td');
      nameCell.append(el('strong', '', item.label || item.name), el('small', 'damai-cell-sub', pageNames[item.page] || item.page));
      const ruleCell = el('td', 'damai-cell-rule', ruleSummary(item));
      const ptCell = el('td', 'damai-cell-pt', item.point ? `(${item.point[0]}, ${item.point[1]})` : '自动读取 XML');
      const st = statusLabel(item);
      const stCell = el('td');
      stCell.append(el('span', 'damai-status-pill ' + st.cls, st.text));
      tr.append(nameCell, ruleCell, ptCell, stCell);
      tbody.append(tr);
    }
  }

  function renderInspection(actState) {
    const inspect = actState?.inspection || null;
    const sig = JSON.stringify(inspect);
    if (sig === inspectSignature) return;
    inspectSignature = sig;

    const badge = $('damai-page-badge');
    badge.textContent = inspect?.page_label ? `当前页：${inspect.page_label}` : '尚未读取大麦页面';

    const box = $('damai-inspection-box');
    box.replaceChildren();
    if (!inspect) {
      box.hidden = true;
      return;
    }
    box.hidden = false;

    const header = el('div', 'damai-inspect-head');
    header.append(
      el('strong', '', `页面快照：${inspect.page_label}`),
      el('small', '', inspect.screen_size ? `${inspect.screen_size[0]}×${inspect.screen_size[1]}` : '')
    );
    box.append(header);

    if (inspect.sell_time_raw) {
      const sellLine = el('div', 'damai-inspect-sell');
      sellLine.append(el('span', '', `识别到开售时间：${inspect.sell_time_raw}`));
      if (inspect.sell_time_parsed) {
        const fillBtn = el('button', 'text-button', `填入 ${inspect.sell_time_parsed}`);
        fillBtn.type = 'button';
        fillBtn.addEventListener('click', () => {
          $('damai-sell-time').value = inspect.sell_time_parsed;
          dirty = true;
        });
        sellLine.append(fillBtn);
      }
      box.append(sellLine);
    }

    if ((inspect.sessions || []).length) {
      const group = el('div', 'damai-chip-group');
      group.append(el('span', 'damai-chip-label', '点击场次快速填入：'));
      for (const s of inspect.sessions) {
        const chip = el('button', 'damai-chip' + (s.selected ? ' is-selected' : '') + (s.sold_out ? ' is-soldout' : ''),
          `[${s.index}] ${s.label}${s.sold_out ? ' (无票)' : ''}`);
        chip.type = 'button';
        chip.addEventListener('click', () => {
          $('damai-session-text').value = s.label.split(/\s+/)[0] || s.label;
          $('damai-session-index').value = String(s.index);
          dirty = true;
        });
        group.append(chip);
      }
      box.append(group);
    }

    if ((inspect.prices || []).length) {
      const group = el('div', 'damai-chip-group');
      group.append(el('span', 'damai-chip-label', '点击票档快速填入：'));
      for (const p of inspect.prices) {
        const chip = el('button', 'damai-chip' + (p.selected ? ' is-selected' : '') + (p.sold_out ? ' is-soldout' : ''),
          `[${p.index}] ${p.label}${p.sold_out ? ' (缺货)' : ''}`);
        chip.type = 'button';
        chip.addEventListener('click', () => {
          const numMatch = p.label.match(/\d{2,5}/);
          $('damai-price-text').value = numMatch ? numMatch[0] : p.label;
          $('damai-price-index').value = String(p.index);
          dirty = true;
        });
        group.append(chip);
      }
      box.append(group);
    }

    if ((inspect.viewers || []).length) {
      const group = el('div', 'damai-chip-group');
      group.append(el('span', 'damai-chip-label', '识别到实名观演人：'));
      for (const v of inspect.viewers) {
        group.append(el('span', 'damai-viewer-tag' + (v.checked ? ' is-checked' : ''),
          `${v.checked ? '☑' : '☐'} ${v.name || '#' + (v.index + 1)}`));
      }
      box.append(group);
    }
  }

  function renderModels(actState) {
    const models = actState?.models || [];
    const sig = JSON.stringify(models);
    if (sig === modelsSignature) return;
    modelsSignature = sig;
    const datalist = $('damai-llm-model-options');
    datalist.replaceChildren();
    const defaults = models.length ? models : ['claude-sonnet-4-6', 'claude-opus-4-7'];
    for (const m of defaults) {
      const opt = el('option');
      opt.value = m;
      datalist.append(opt);
    }
  }

  function renderControls() {
    const panel = $('damai-panel');
    const visible = isDamaiVisible();
    panel.hidden = !visible;
    if (!visible) return;

    const busy = isBusy();
    const connected = Boolean(snapshot?.connected);
    const actState = snapshot?.app === 'damai' ? (snapshot?.activity_state || {}) : {};
    const hasKey = Boolean(actState.has_llm_key) && !clearStoredKeyRequested;

    $('damai-save').disabled = busy;
    $('damai-inspect').disabled = busy || !connected;
    $('damai-calibrate').disabled = busy || !connected;
    $('damai-clear-cache').disabled = busy;
    $('damai-llm-models-btn').disabled = busy;
    $('damai-clear-key').hidden = !hasKey;
    $('damai-clear-key').disabled = busy;
    $('damai-llm-api-key').placeholder = hasKey
      ? '本机已加密保存 API Key（留空沿用，输入新值覆盖）'
      : '输入 Anthropic 兼容 API Key（仅存本机 secrets.json）';

    for (const id of [
      'damai-session-text', 'damai-session-index', 'damai-price-text', 'damai-price-index',
      'damai-sell-time', 'damai-lead-ms', 'damai-order-mode', 'damai-viewer-count',
      'damai-viewer-names', 'damai-submit-order', 'damai-jitter-min', 'damai-jitter-max',
      'damai-max-attempts', 'damai-page-timeout', 'damai-agree-tolerance',
      'damai-auto-resume-verify', 'damai-verify-wait', 'damai-auto-select-session',
      'damai-llm-enabled', 'damai-llm-base-url', 'damai-llm-model', 'damai-llm-api-key',
      'damai-ocr-min-conf',
    ]) {
      $(id).disabled = busy;
    }

    renderCoords(actState);
    renderInspection(actState);
    renderModels(actState);
  }

  for (const id of [
    'damai-session-text', 'damai-session-index', 'damai-price-text', 'damai-price-index',
    'damai-sell-time', 'damai-lead-ms', 'damai-order-mode', 'damai-viewer-count',
    'damai-viewer-names', 'damai-submit-order', 'damai-jitter-min', 'damai-jitter-max',
    'damai-max-attempts', 'damai-page-timeout', 'damai-agree-tolerance',
    'damai-auto-resume-verify', 'damai-verify-wait', 'damai-auto-select-session',
    'damai-llm-enabled', 'damai-llm-base-url', 'damai-llm-model', 'damai-llm-api-key',
    'damai-ocr-min-conf',
  ]) {
    $(id).addEventListener('input', () => { dirty = true; });
    $(id).addEventListener('change', () => { dirty = true; });
  }

  $('damai-save').addEventListener('click', () => runDamaiRequest('save'));
  $('damai-inspect').addEventListener('click', () => runDamaiRequest('inspect'));
  $('damai-calibrate').addEventListener('click', () => runDamaiRequest('calibrate'));
  $('damai-clear-cache').addEventListener('click', () => runDamaiRequest('clear_cache'));
  $('damai-llm-models-btn').addEventListener('click', () => runDamaiRequest('models'));
  $('damai-clear-key').addEventListener('click', () => {
    clearStoredKeyRequested = true;
    $('damai-llm-api-key').value = '';
    runDamaiRequest('save');
  });

  window.addEventListener('miaotask:view-app', event => {
    viewApp = event.detail || '';
    renderControls();
  });

  window.addEventListener('miaotask:device-selected', () => {
    snapshot = null;
    dirty = false;
    clearStoredKeyRequested = false;
    coordSignature = '';
    inspectSignature = '';
    $('damai-llm-api-key').value = '';
    renderControls();
  });

  window.addEventListener('miaotask:state', event => {
    snapshot = event.detail;
    const dev = MiaoAPI.device;
    if (workspace !== dev) {
      workspace = dev;
      dirty = false;
      clearStoredKeyRequested = false;
      coordSignature = '';
      inspectSignature = '';
    }
    const opts = snapshot?.config?.activity_options;
    if (!dirty && snapshot?.app === 'damai' && opts) {
      applyOptions(opts);
    }
    renderControls();
  });

  window.MiaoDamai = Object.freeze({
    collectOptions,
    collectSecrets,
    markSaved,
    validate,
  });
})();
