'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const fields = [
    'group-url', 'keywords', 'excludes', 'match-mode', 'browser',
    'interval-seconds', 'max-checks', 'duration-minutes',
    'max-notifications-per-check', 'max-posts-per-check',
    'telegram-token', 'telegram-chat-id', 'remember-telegram-token'
  ];
  const busyPhases = new Set(['opening', 'confirming', 'checking', 'reading', 'sending', 'closing']);
  const phaseLabels = {
    idle: '尚未連線', opening: '開啟視窗中', confirming: '確認社團中',
    awaiting_login: '等待登入', login: '等待登入', ready: '已就緒',
    checking: '讀取貼文中', reading: '讀取貼文中', running: '追蹤中',
    paused: '需要你確認', error: '需要處理', closing: '關閉視窗中',
    sending: '發送中', stopped: '已停止'
  };
  const notificationLabels = {
    baseline: '起點貼文', pending: '等待通知', sending: '發送中', sent: '已通知',
    error: '通知失敗', unknown: '送達狀態未確認', held: '保留預覽',
    skipped_no_link: '暫無原文連結', non_match: '未符合條件', not_matched: '未符合條件',
    preview: '預覽', not_sent: '尚未通知', skipped: '未發送'
  };
  const state = {
    data: null, loaded: false, dirty: false, actionBusy: false,
    refreshBusy: false, connected: true, stopped: false, serial: 0,
    filter: 'all', expanded: new Set()
  };

  function escapeHTML(value) {
    const entities = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
    return String(value ?? '').replace(/[&<>"']/g, character => entities[character]);
  }

  function sanitize(value) {
    let text = String(value ?? '').replace(/(?:bot)?\d{5,}:[A-Za-z0-9_-]+/g, '[Token 已隱藏]');
    const draftToken = $('telegram-token').value.trim();
    if (draftToken) text = text.split(draftToken).join('[Token 已隱藏]');
    return text;
  }

  function message(text, error = false) {
    $('action-message').textContent = sanitize(text);
    $('action-message').classList.toggle('error', error);
    $('action-message').hidden = !text;
  }

  async function api(path, method = 'GET', body) {
    const options = {method, headers: {'X-App-Request': '1'}};
    if (method !== 'GET') {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body ?? {});
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch {
      throw new Error('無法連上本機程式，請確認啟動視窗仍在執行。');
    }
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error('本機程式回應異常，請稍後重新整理。');
    }
    if (!response.ok) {
      const detail = Array.isArray(data.detail)
        ? data.detail.map(item => String(item.msg || '設定有誤').replace(/^Value error, /, '')).join('。')
        : data.detail;
      throw new Error(sanitize(detail || '操作未完成，請查看執行紀錄。'));
    }
    return data;
  }

  function dateLabel(value) {
    if (!value) return '—';
    const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-TW', {hour12: false});
  }

  function remaining(value) {
    const timestamp = Number(value);
    if (!Number.isFinite(timestamp) || !timestamp) return 0;
    return Math.max(0, Math.ceil(timestamp - Date.now() / 1000));
  }

  function facebookURL(value) {
    try {
      const url = new URL(value);
      const allowed = ['facebook.com', 'www.facebook.com', 'm.facebook.com'];
      if (url.protocol !== 'https:' || !allowed.includes(url.hostname) || url.username || url.password) return '';
      if (url.port && url.port !== '443') return '';
      return url.href;
    } catch {
      return '';
    }
  }

  function populateSettings(settings) {
    $('remember-telegram-token').checked = settings.telegram_storage_supported === true && settings.remember_telegram_token !== false;
    $('group-url').value = settings.group_url || '';
    $('keywords').value = (settings.keywords || []).join('、');
    $('excludes').value = (settings.excludes || []).join('、');
    $('match-mode').value = settings.match_mode || 'any';
    $('browser').value = settings.browser || 'auto';
    $('interval-seconds').value = settings.interval_seconds ?? 120;
    $('max-checks').value = settings.max_checks ?? 3;
    $('duration-minutes').value = settings.duration_minutes ?? 10;
    $('max-notifications-per-check').value = settings.max_notifications_per_check ?? 3;
    $('max-posts-per-check').value = settings.max_posts_per_check ?? 20;
    $('telegram-chat-id').value = settings.telegram_chat_id || '';
    // Tokens are write-only, including when an unexpected API response contains one.
    $('telegram-token').value = '';
    state.dirty = false;
  }

  function renderSettingsSummary() {
    const seconds = Number($('interval-seconds').value) || 120;
    const interval = seconds % 60 === 0 ? `${seconds / 60} 分鐘` : `${seconds} 秒`;
    $('run-summary').textContent = `每 ${interval}，最多 ${$('max-checks').value || 3} 次`;
    $('rules-summary').textContent = $('match-mode').value === 'all' ? '全部關鍵字' : '任一關鍵字';
    $('draft-status').textContent = state.dirty ? '有未儲存的設定' : '已載入本機設定';
    $('draft-status').classList.toggle('unsaved', state.dirty);
  }

  function runDescription(settings) {
    const seconds = settings.interval_seconds ?? 120;
    const interval = seconds % 60 === 0 ? `${seconds / 60} 分鐘` : `${seconds} 秒`;
    return `每 ${interval}檢查一次，最多 ${settings.max_checks ?? 3} 次或 ${settings.duration_minutes ?? 10} 分鐘`;
  }

  function workflow(data, busy) {
    const status = data.status || {};
    const settings = data.settings || {};
    const stage = status.baseline_ready ? 2 : status.group_confirmed ? 1 : 0;
    const base = {stage, disabled: false, note: '', action: ''};
    if (!state.connected) {
      return {...base, title: '本機程式暫時沒有回應', description: '請確認啟動視窗仍在執行。連線恢復後就能繼續。', label: '重新連線', action: 'refresh'};
    }
    if (busy) {
      const reading = status.phase === 'checking' || status.phase === 'reading';
      return {...base, disabled: true, title: reading ? '正在載入更多貼文' : '正在處理，請稍候', description: reading ? '讀取完成後，會在下方顯示貼文、符合原因與原文連結。' : sanitize(status.message || '正在等待本機程式完成操作。'), label: reading ? '讀取貼文中…' : '處理中…'};
    }
    if (state.dirty) {
      return {...base, title: '把想追蹤的條件儲存下來', description: '來源、關鍵字或通知設定已變更。儲存後，就能依照新設定繼續下一步。', label: '儲存設定', action: 'save', note: '修改內容只會在按下儲存後套用'};
    }
    if (status.phase === 'paused' || status.phase === 'error') {
      return {...base, title: '追蹤已暫停，請確認 Facebook 狀態', description: sanitize(status.message || '請在追蹤視窗親自完成登入或驗證，再回來確認。'), label: status.browser_open ? '確認登入／限制已處理' : '重新開啟追蹤視窗', action: status.browser_open ? 'confirm-browser' : 'open-browser'};
    }
    if (status.auto_running) {
      return {...base, title: '正在幫你留意符合條件的貼文', description: '新偵測的貼文符合條件且有原文連結時，會傳到已設定的 Telegram 聊天。', label: '停止這次追蹤', action: 'stop-auto', note: '可以隨時停止，稍後再開始'};
    }
    if (!String(settings.group_url || '').trim()) {
      return {...base, stage: 0, title: '先選一個你想關注的社團', description: '在左側貼上 Facebook 社團網址，再填入你關心的關鍵字。', label: '填入社團網址', action: 'source'};
    }
    if (!status.browser_open) {
      const cooldown = remaining(status.cooldown_until);
      return {...base, stage: 0, title: '連接你的 Facebook 社團', description: '開啟獨立的追蹤視窗，用自己的帳號登入。登入後，請選擇「新貼文」排序，再回來確認。', label: cooldown ? `約 ${cooldown} 秒後可連線` : '開啟 Facebook 追蹤視窗', action: 'open-browser', disabled: cooldown > 0, note: 'Facebook 密碼直接在 Facebook 視窗輸入'};
    }
    if (!status.group_confirmed) {
      return {...base, stage: 0, title: '在 Facebook 完成登入與社團確認', description: '請在剛開啟的視窗完成登入及驗證，確認看得到這個社團的貼文，並切換到「新貼文」排序。', label: '已登入，確認社團', action: 'confirm-browser', note: '完成後回到這個頁面繼續'};
    }
    if (!status.baseline_ready) {
      return {...base, stage: 1, title: '先試讀，看看找到了什麼', description: '讀取社團貼文並預覽比對結果。這次會建立追蹤起點，已有的貼文不會自動推播。', label: '讀取貼文預覽', action: 'check-once', note: `每次最多讀取 ${settings.max_posts_per_check ?? 20} 篇，實際篇數依頁面而定`};
    }
    if (!(data.posts || []).length) {
      const cooldown = remaining(status.cooldown_until);
      return {...base, stage: 1, title: '這次還沒有讀取到貼文', description: '確認追蹤視窗已顯示社團貼文後，再試讀一次。也可以展開執行紀錄查看原因。', label: cooldown ? `約 ${cooldown} 秒後可重試` : '再讀取一次', action: 'check-once', disabled: cooldown > 0};
    }
    if (!settings.telegram_configured || !String(settings.telegram_chat_id || '').trim()) {
      return {...base, stage: 2, title: '預覽好了，把消息送到手機', description: '展開手機通知設定，連接你自己的 Telegram Bot 與私人聊天。儲存後可以發送一則測試訊息。', label: settings.telegram_configured ? '選擇接收通知的聊天' : '設定 Telegram 通知', action: 'telegram', note: '每則貼文通知會附上可直接開啟的原文連結'};
    }
    return {...base, stage: 2, title: '準備好了，開始留意新消息', description: `${runDescription(settings)}。到達上限即停止，之後可再次開始。`, label: '開始追蹤', action: 'start-auto', note: '符合條件的首次收錄貼文才會自動通知'};
  }

  function renderWorkflow(data, busy) {
    const next = workflow(data, busy);
    const labels = ['第 1 步 · 連接社團', '第 2 步 · 試讀與預覽', '第 3 步 · 通知與追蹤'];
    $('step-label').textContent = labels[next.stage];
    $('workflow-title').textContent = next.title;
    $('workflow-description').textContent = next.description;
    $('next-action').innerHTML = `${escapeHTML(next.label)} <span aria-hidden="true">${next.action === 'stop-auto' ? 'Ⅱ' : '→'}</span>`;
    $('next-action').dataset.action = next.action;
    $('next-action').disabled = next.disabled;
    $('next-action-note').textContent = next.note;
    ['step-connect', 'step-preview', 'step-track'].forEach((id, index) => {
      $(id).classList.toggle('current', index === next.stage);
      $(id).classList.toggle('complete', index < next.stage);
      if (index === next.stage) $(id).setAttribute('aria-current', 'step');
      else $(id).removeAttribute('aria-current');
    });
  }

  function renderStatus(data) {
    const status = data.status || {};
    const settings = data.settings || {};
    const pending = Array.isArray(status.pending) && status.pending.length > 0;
    const busy = state.actionBusy || busyPhases.has(status.phase) || pending;
    const blocked = status.phase === 'paused' || status.phase === 'error';
    const auto = !!status.auto_running;
    const recipient = !!settings.telegram_configured && !!String(settings.telegram_chat_id || '').trim();
    const cooldown = remaining(status.cooldown_until);
    const unavailable = busy || !state.connected;
    $('phase-badge').textContent = auto && !busy ? '追蹤中' : phaseLabels[status.phase] || '尚未開始';
    $('phase-badge').className = 'badge' + (blocked ? ' error' : auto ? ' active' : busy ? ' warning' : '');
    $('status-message').textContent = sanitize(status.message || '請先設定追蹤來源。');
    $('last-checked').textContent = status.last_checked ? dateLabel(status.last_checked) : '尚未讀取';
    $('cooldown').textContent = cooldown ? `尚需約 ${cooldown} 秒` : '目前可讀取';
    $('next-check').textContent = auto && status.next_check_at ? dateLabel(status.next_check_at) : '未排定';
    $('remaining-checks').textContent = auto ? `${status.remaining_checks ?? 0} 次` : '未開始';
    $('tracking-stats').hidden = !auto && !status.last_checked;
    $('resolved-group').textContent = status.resolved_group_url || '尚未確認';
    $('baseline-description').textContent = status.baseline_ready
      ? '追蹤起點已建立。後續新偵測的貼文符合條件且有原文連結時，才會自動通知。'
      : '第一次讀取只建立起點，已有的貼文不會自動通知。';
    $('telegram-status').textContent = recipient ? '已連結' : settings.telegram_configured ? '待選聊天' : '未設定';
    $('telegram-status').className = 'badge' + (recipient ? ' active' : '');
    $('open-browser').disabled = unavailable || auto || !!status.browser_open || cooldown > 0 || state.dirty;
    $('confirm-browser').disabled = unavailable || auto || !status.browser_open || state.dirty;
    $('close-browser').disabled = unavailable || !status.browser_open;
    $('check-once').textContent = status.baseline_ready ? '再檢查一次' : '讀取貼文預覽';
    $('check-once').disabled = unavailable || auto || blocked || !status.browser_open || !status.group_confirmed || (status.baseline_ready && cooldown > 0) || state.dirty;
    $('start-auto').disabled = unavailable || auto || blocked || !status.browser_open || !status.group_confirmed || !status.baseline_ready || !recipient || state.dirty;
    $('stop-auto').disabled = state.actionBusy || !state.connected || !(auto || busyPhases.has(status.phase) || pending);
    $('discover-chats').disabled = unavailable || auto || !settings.telegram_configured || state.dirty;
    $('test-telegram').disabled = unavailable || auto || !recipient || state.dirty;
    ['save-settings', 'save-telegram', 'save-token'].forEach(id => { $(id).disabled = unavailable || auto; });
    $('shutdown').disabled = state.actionBusy || !state.connected;
    document.querySelectorAll('[data-test-post]').forEach(button => {
      button.disabled = unavailable || auto || !recipient || state.dirty;
    });
    fields.forEach(id => { $(id).disabled = unavailable || auto; });
    $('remember-telegram-token').disabled = unavailable || auto || settings.telegram_storage_supported !== true;
    $('forget-telegram-token').disabled = unavailable || auto || state.dirty || !(settings.telegram_configured || settings.telegram_token_saved);
    let storageMessage = '勾選並儲存後，Token 會使用目前的 Windows 帳戶加密，重開程式自動載入。';
    if (settings.telegram_storage_supported === undefined) storageMessage = '重新啟動程式後，即可使用記住 Token 功能。';
    else if (!settings.telegram_storage_supported) storageMessage = '此系統目前僅支援在本次執行使用 Token，Windows 可加密保存。';
    else if (settings.telegram_storage_error) storageMessage = sanitize(settings.telegram_storage_error);
    else if (!$('remember-telegram-token').checked) storageMessage = '儲存設定後會移除已保存的 Token，本次執行仍可繼續使用。';
    else if (settings.telegram_token_saved) storageMessage = 'Token 已加密保存，重開程式會自動載入。';
    $('token-storage-status').textContent = storageMessage;
    renderSettingsSummary();
    renderWorkflow(data, busy);
  }

  function renderPost(post) {
    const url = facebookURL(post.url);
    const author = post.author || '未提供發文者';
    const id = String(post.id ?? '');
    const content = String(post.content ?? '');
    const long = content.length > 420 || content.split('\n').length > 7;
    const expanded = state.expanded.has(id);
    const label = notificationLabels[post.notification_status] || '尚未通知';
    const hits = Array.isArray(post.hits) ? post.hits : [];
    let reason = '未通過關鍵字或排除詞比對';
    if (post.matched) reason = hits.length ? `包含 ${hits.map(word => `「${String(word)}」`).join('、')}` : '符合已儲存的追蹤條件';
    return `<article class="post">
      <div class="post-heading"><span class="author-avatar" aria-hidden="true">${escapeHTML(Array.from(author)[0])}</span><div class="post-author"><h3>${escapeHTML(author)}</h3><p>${escapeHTML(post.posted_at || '未提供發文時間')}</p></div><span class="badge ${post.notification_status === 'sent' ? 'active' : ''}">${escapeHTML(label)}</span></div>
      <p class="post-content${long && !expanded ? ' collapsed' : ''}">${escapeHTML(content)}</p>
      ${long ? `<button type="button" class="text-button expand-post" data-expand-post="${escapeHTML(id)}" aria-expanded="${expanded}">${expanded ? '收合內容' : '展開完整已讀取內容'}</button>` : ''}
      <div class="post-match${post.matched ? '' : ' unmatched'}"><span aria-hidden="true">${post.matched ? '✓' : '−'}</span><span>${post.matched ? '符合條件' : '未符合條件'} · ${escapeHTML(reason)}</span></div>
      <div class="post-footer">${url ? `<a class="post-link" href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">開啟原始貼文 ↗</a><button type="button" class="text-button test-post" data-test-post="${escapeHTML(id)}" disabled>發送這篇測試通知</button>` : '<span class="post-note">暫無可用原文連結，保留預覽</span>'}</div>
      ${post.is_baseline ? '<p class="post-note">首次試讀收錄，不會因建立追蹤起點而自動推播。</p>' : ''}
      ${post.error ? `<p class="post-error">${escapeHTML(sanitize(post.error))}</p>` : ''}
    </article>`;
  }

  function renderPosts(data) {
    const posts = [...(data.posts || [])];
    const sourceOrder = new Map((data.status?.preview_post_ids || []).map((id, index) => [String(id), index]));
    if (sourceOrder.size) {
      posts.sort((left, right) =>
        (sourceOrder.get(String(left.post_id)) ?? sourceOrder.size) -
        (sourceOrder.get(String(right.post_id)) ?? sourceOrder.size));
    }
    const matched = posts.filter(post => post.matched);
    const shown = state.filter === 'matched' ? matched : posts;
    $('post-count').textContent = posts.length;
    $('all-count').textContent = posts.length;
    $('matched-count').textContent = matched.length;
    $('results-toolbar').hidden = !posts.length;
    ['all', 'matched'].forEach(filter => {
      $(`filter-${filter}`).classList.toggle('selected', state.filter === filter);
      $(`filter-${filter}`).setAttribute('aria-pressed', String(state.filter === filter));
    });
    const scan = data.status?.scan_summary;
    $('results-caption').textContent = scan && Number.isFinite(scan.loaded_posts)
      ? `本次讀取 ${scan.loaded_posts} 篇${scan.unlinked_posts ? ' · 部分內容待確認原文連結' : ''}`
      : `${posts.filter(post => facebookURL(post.url)).length} 篇有原文連結`;
    let html;
    if (shown.length) html = shown.map(renderPost).join('');
    else if (posts.length) html = '<div class="empty-state"><div class="empty-symbol" aria-hidden="true">⌕</div><h3>目前沒有符合條件的貼文</h3><p>可以查看全部貼文，或調整左側的關鍵字再儲存。</p></div>';
    else if (data.status?.baseline_ready) html = '<div class="empty-state"><div class="empty-symbol" aria-hidden="true">▤</div><h3>這次沒有讀取到貼文</h3><p>請確認追蹤視窗顯示社團貼文，再使用上方按鈕試讀一次。</p></div>';
    else html = '<div class="empty-state"><div class="empty-symbol" aria-hidden="true">▤</div><h3>關心的消息，會在這裡集合</h3><p>連接社團並試讀後，即可預覽貼文與關鍵字比對結果。</p><div class="empty-steps"><span>連接社團</span><span aria-hidden="true">→</span><span>試讀預覽</span><span aria-hidden="true">→</span><span>通知手機</span></div></div>';
    if ($('posts').innerHTML !== html) $('posts').innerHTML = html;
  }

  function renderEvents(events) {
    $('event-count').textContent = events.length;
    $('events').innerHTML = events.length ? events.map(event => `<div class="event${event.level === 'error' ? ' error' : ''}"><time>${escapeHTML(dateLabel(event.at))}</time><p>${escapeHTML(sanitize(event.message))}</p></div>`).join('') : '<p class="empty">尚無紀錄</p>';
  }

  async function refresh() {
    if (state.stopped || state.refreshBusy) return;
    state.refreshBusy = true;
    const serial = ++state.serial;
    try {
      const data = await api('/api/state');
      if (state.stopped || serial !== state.serial) return;
      state.data = data;
      state.connected = true;
      if (!state.loaded) {
        populateSettings(data.settings || {});
        state.loaded = true;
      }
      renderPosts(data);
      renderEvents(data.events || []);
      renderStatus(data);
      $('connection-error').hidden = true;
    } catch (error) {
      state.connected = false;
      $('connection-error').textContent = sanitize(error.message);
      $('connection-error').hidden = false;
      if (state.data) renderStatus(state.data);
      else {
        $('workflow-title').textContent = '無法連上本機程式';
        $('workflow-description').textContent = '請確認啟動視窗仍在執行，再重新連線。';
        $('next-action').textContent = '重新連線';
        $('next-action').dataset.action = 'refresh';
        $('next-action').disabled = false;
      }
    } finally {
      state.refreshBusy = false;
    }
  }

  function dirty() {
    state.dirty = true;
    if (state.data) renderStatus(state.data);
  }

  function settingsBody() {
    const words = value => [...new Set(value.split(/[,，、\n]+/).map(word => word.trim()).filter(Boolean))];
    const body = {
      group_url: $('group-url').value.trim(), keywords: words($('keywords').value),
      excludes: words($('excludes').value), match_mode: $('match-mode').value,
      browser: $('browser').value, interval_seconds: Number($('interval-seconds').value),
      max_checks: Number($('max-checks').value), duration_minutes: Number($('duration-minutes').value),
      max_notifications_per_check: Number($('max-notifications-per-check').value),
      max_posts_per_check: Number($('max-posts-per-check').value),
      telegram_chat_id: $('telegram-chat-id').value.trim(),
      remember_telegram_token: $('remember-telegram-token').checked
    };
    const token = $('telegram-token').value.trim();
    if (token) body.telegram_token = token;
    return body;
  }

  async function action(path, success, body = {}) {
    if (state.actionBusy || state.stopped) return null;
    state.actionBusy = true;
    if (state.data) renderStatus(state.data);
    message('正在處理…');
    try {
      const result = await api(path, 'POST', body);
      message(success);
      return result;
    } catch (error) {
      message(error.message, true);
      return null;
    } finally {
      state.actionBusy = false;
      await refresh();
      if (state.data && !state.stopped) renderStatus(state.data);
    }
  }

  async function saveSettings() {
    if (state.actionBusy || state.stopped) return;
    const form = $('settings-form');
    if (!form.checkValidity()) {
      const invalid = form.querySelector(':invalid');
      let parent = invalid?.parentElement;
      while (parent && parent !== form) {
        if (parent.tagName === 'DETAILS') parent.open = true;
        parent = parent.parentElement;
      }
      form.reportValidity();
      return;
    }
    const result = await action('/api/settings', '設定已儲存，可以繼續下一步。', settingsBody());
    if (result !== null) {
      $('telegram-token').value = '';
      state.dirty = false;
      if (state.data) renderStatus(state.data);
    }
  }

  const commands = {
    'open-browser': () => action('/api/browser/open', '已送出開啟要求，請在 Facebook 視窗完成登入，並選擇新貼文排序。'),
    'confirm-browser': () => action('/api/browser/confirm', '已送出社團確認要求，請稍候查看狀態。'),
    'close-browser': () => action('/api/browser/close', '已送出關閉追蹤視窗的要求。'),
    'check-once': () => action('/api/check', state.data?.status?.baseline_ready ? '已送出讀取要求，完成後會更新下方貼文。' : '已送出試讀要求，這次會建立追蹤起點，舊貼文不會自動通知。'),
    'start-auto': () => action('/api/auto/start', '已送出開始追蹤要求，進度會顯示在上方。'),
    'stop-auto': () => action('/api/auto/stop', '已送出停止要求，請稍候查看狀態。'),
    'test-telegram': () => action('/api/telegram/test', '已送出測試訊息要求，請查看 Telegram 與執行紀錄。')
  };

  fields.forEach(id => {
    $(id).addEventListener('input', dirty);
    $(id).addEventListener('change', dirty);
  });
  $('settings-form').addEventListener('submit', event => {
    event.preventDefault();
    saveSettings();
  });
  Object.entries(commands).forEach(([id, command]) => $(id).addEventListener('click', command));
  $('next-action').addEventListener('click', () => {
    const name = $('next-action').dataset.action;
    if (name === 'save') saveSettings();
    else if (name === 'refresh') refresh();
    else if (name === 'source') $('group-url').focus();
    else if (name === 'telegram') {
      $('telegram-details').open = true;
      const target = state.data?.settings?.telegram_configured ? 'telegram-chat-id' : 'telegram-token';
      $(target).focus();
    } else if (commands[name] && !$(name).disabled) commands[name]();
  });
  $('refresh-state').addEventListener('click', refresh);
  $('forget-telegram-token').addEventListener('click', async () => {
    const result = await action('/api/telegram/forget', 'Token 已清除，可重新貼入並儲存。');
    if (result !== null) $('telegram-token').value = '';
  });
  ['all', 'matched'].forEach(filter => {
    $(`filter-${filter}`).addEventListener('click', () => {
      state.filter = filter;
      if (state.data) {
        renderPosts(state.data);
        renderStatus(state.data);
      }
    });
  });
  $('posts').addEventListener('click', event => {
    const expand = event.target.closest('[data-expand-post]');
    if (expand) {
      const id = expand.dataset.expandPost;
      if (state.expanded.has(id)) state.expanded.delete(id);
      else state.expanded.add(id);
      renderPosts(state.data);
      renderStatus(state.data);
    }
    const button = event.target.closest('[data-test-post]');
    if (button && !button.disabled) {
      action(`/api/posts/${encodeURIComponent(button.dataset.testPost)}/test-notification`, '已送出這篇貼文的測試通知要求，請查看 Telegram 與執行紀錄。');
    }
  });
  $('discover-chats').addEventListener('click', async () => {
    const result = await action('/api/telegram/discover', '已讀取聊天清單，請核對自己的聊天並儲存接收對象。');
    if (result === null) return;
    const chats = result.chats || [];
    $('discovered-chats').innerHTML = '<option value="">選擇自己的私人聊天</option>' + chats.map(chat => `<option value="${escapeHTML(chat.id)}">${escapeHTML(chat.name || '未提供名稱')}（${escapeHTML(chat.id)}）</option>`).join('');
    $('discovered-chats-wrap').hidden = false;
    if (!chats.length) message('尚未找到私人聊天。請先向自己的 Bot 傳送 /start，再讀取清單。');
  });
  $('discovered-chats').addEventListener('change', event => {
    if (event.target.value) {
      $('telegram-chat-id').value = event.target.value;
      dirty();
    }
  });
  $('shutdown').addEventListener('click', async () => {
    const result = await action('/api/shutdown', '已送出結束程式的要求。');
    if (result === null) return;
    state.stopped = true;
    state.serial++;
    $('connection-error').hidden = true;
    $('phase-badge').textContent = '已結束';
    $('phase-badge').className = 'badge';
    $('workflow-title').textContent = '這次追蹤已結束';
    $('workflow-description').textContent = state.data?.settings?.telegram_token_saved && !state.data?.settings?.telegram_storage_error
      ? '可以關閉這個頁面。Token 已加密保存，重新啟動會自動載入。'
      : '可以關閉這個頁面。下次啟動後，可在手機通知設定中填入 Telegram Token。';
    $('next-action').textContent = '程式已結束';
    $('next-action-note').textContent = '';
    document.querySelectorAll('button,input,textarea,select').forEach(element => { element.disabled = true; });
  });

  refresh();
  setInterval(() => {
    if (!state.actionBusy) refresh();
  }, 5000);
})();
