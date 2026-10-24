// harness — mobile-first client for the entity-based refactor (2026-10-17).
// One file handles BOTH the index (entity list) and chat pages; the active
// page is set via <script data-page="...">. Simpler than two bundles.

(function () {
  const page = (document.currentScript && document.currentScript.dataset.page) || 'index';
  const entity = (document.currentScript && document.currentScript.dataset.entity) || null;
  console.log('[harness] booting, page=' + page + ' entity=' + entity);

  const $ = (id) => document.getElementById(id);
  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));

  // Phase 16.2 (2026-10-20): markdown rendering for assistant messages.
  // marked parses the model's markdown into HTML; DOMPurify strips
  // anything dangerous (XSS guard — we trust the model enough to
  // allow formatting tags but NOT script/style/event-handler/onload
  // payloads). Defaults: gfm=true (GitHub-flavoured: tables, strikethrough,
  // task lists), breaks=true (single newline -> <br>), no mangled
  // autolinks. Falls back to escaped plain text if marked or
  // DOMPurify failed to load (offline / CDN unreachable).
  function renderMarkdown(text) {
    if (text == null) return '';
    const raw = String(text);
    try {
      if (typeof marked !== 'undefined' && typeof marked.parse === 'function') {
        const html = marked.parse(raw, {gfm: true, breaks: true, mangle: false, headerIds: false});
        if (typeof DOMPurify !== 'undefined' && typeof DOMPurify.sanitize === 'function') {
          return DOMPurify.sanitize(html, {ADD_ATTR: ['target'], USE_PROFILES: {html: true}});
        }
        return html;  // marked loaded but not DOMPurify — risky but better than nothing
      }
    } catch (e) {
      console.warn('renderMarkdown failed:', e);
    }
    return escapeHtml(raw);
  }

  const PALETTE = ['#4f9aff', '#5fc77e', '#e89c4f', '#c45fd0', '#4fd0c0', '#e85a85'];
  function colorFor(name) {
    let h = 0; for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return PALETTE[h % PALETTE.length];
  }
  function entityIconHTML(name, size, iconUrl) {
    const c = colorFor(name);
    const sz = size ? `width:${size}px;height:${size}px;font-size:${Math.round(size*0.4)}px;` : '';
    if (iconUrl) {
      return `<div class="entity-icon" style="${sz}"><img src="${iconUrl}" alt=""></div>`;
    }
    const letter = (name[0] || '?').toUpperCase();
    return `<div class="entity-icon" style="--icon-bg:${c};background:${c};${sz}">${letter}</div>`;
  }
  function fmtTimeAgo(iso) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (isNaN(t)) return '';
    const s = Math.floor((Date.now() - t) / 1000);
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    if (s < 86400 * 7) return Math.floor(s / 86400) + 'd';
    return new Date(iso).toLocaleDateString();
  }

  async function api(path, opts) {
    opts = opts || {};
    opts.credentials = 'same-origin';
    // FormData (used by /api/v1/uploads) must be sent as-is with the
    // browser-generated multipart boundary. Don't JSON.stringify it
    // (which would turn it into '{}') and don't set Content-Type
    // manually (which would clobber the boundary). (Bug: file
    // attachments silently dropped because upload returned 400
    // "no file provided".)
    const isFormData = typeof FormData !== 'undefined' && opts.body instanceof FormData;
    if (opts.body && typeof opts.body !== 'string' && !isFormData) {
      opts.headers = Object.assign({'Content-Type': 'application/json'}, opts.headers || {});
      opts.body = JSON.stringify(opts.body);
    }
    const r = await fetch(path, opts);
    // 401: just throw — let the caller decide. (Previously we called
    // showLogin() here, which hid the chat pane mid-send and made the
    // optimistic message appear to "flash then disappear".)
    if (r.status === 401) throw new Error('unauthorized');
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).error || ''; } catch (e) {}
      throw new Error(detail || ('HTTP ' + r.status));
    }
    return r;
  }

  let currentUser = null;
  async function checkAuth() {
    try {
      const r = await fetch('/api/v1/auth/me', { credentials: 'same-origin' });
      if (r.ok) {
        const data = await r.json();
        currentUser = data.user;
        showAuth(data.user);
        return data.user;
      }
    } catch (e) {}
    currentUser = null;
    showLogin();
    return null;
  }
  function showLogin() {
    console.log('[harness] showLogin (not authenticated)');
    const lp = $('loginPane'); if (lp) lp.style.display = '';
    const ep = $('entityPane'); if (ep) ep.style.display = 'none';
    const cp = $('chatPane'); if (cp) cp.style.display = 'none';
    const menuWrap = $('menuWrap'); if (menuWrap) menuWrap.style.display = 'none';
    closeMenu();
  }

  // Transient toast notification. Auto-dismisses after 4s.
  let toastTimer = null;
  function showToast(msg, isError) {
    let t = document.getElementById('harnessToast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'harnessToast';
      t.className = 'toast';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.className = 'toast' + (isError ? ' error' : '');
    t.style.display = '';
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.style.display = 'none'; }, 4000);
  }
  function showAuth(user) {
    console.log('[harness] showAuth for', user.email, 'page=' + page);
    const lp = $('loginPane'); if (lp) lp.style.display = 'none';
    // Populate the burger menu header
    const menuHeader = $('menuHeader');
    if (menuHeader) menuHeader.textContent = user.email;
    // Show the menu wrap (it stays hidden until auth check returns OK)
    const menuWrap = $('menuWrap');
    if (menuWrap) menuWrap.style.display = '';
    // Show the "Remove image" item if a custom icon is already set
    if (page === 'chat' && entity) {
      const remove = $('menuRemoveImage');
      if (remove) {
        const hasIcon = !!currentEntityIconUrl;
        remove.style.display = hasIcon ? '' : 'none';
      }
    }
    if (page === 'index') {
      const ep = $('entityPane'); if (ep) ep.style.display = '';
      // The "new group" button is admin-only: per Mal (2026-10-23)
      // groups are created by admins.
      const ng = $('newGroupBtn');
      if (ng) ng.style.display = (user.role === 'admin') ? '' : 'none';
    } else {
      const cp = $('chatPane');
      if (cp) { cp.style.display = 'flex'; console.log('[harness] chatPane shown'); }
      else { console.error('[harness] chatPane element not found!'); }
    }
  }

  const loginForm = $('loginForm');
  if (loginForm) {
    const loginBtn = $('loginBtn');
    const loginError = $('loginError');
    loginForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const email = $('email').value.trim();
      const password = $('password').value;
      if (!email || !password) return;
      loginBtn.disabled = true;
      loginError.style.display = 'none';
      try {
        const r = await fetch('/api/v1/auth/login', {
          method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({email, password}),
        });
        if (r.ok) {
          const data = await r.json();
          currentUser = data.user;
          showAuth(data.user);
          if (page === 'index') loadEntities();
          else loadTurns();
        } else {
          const data = await r.json().catch(() => ({}));
          loginError.textContent = data.error || 'Login failed';
          loginError.style.display = 'block';
        }
      } catch (err) {
        loginError.textContent = 'Network error: ' + err;
        loginError.style.display = 'block';
      } finally {
        loginBtn.disabled = false;
      }
    });

    const passwordInput = $('password');
    const passwordToggle = $('passwordToggle');
    if (passwordInput && passwordToggle) {
      const togglePassword = (e) => {
        if (e) { e.preventDefault(); e.stopPropagation(); }
        const visible = passwordInput.type === 'password';
        passwordInput.type = visible ? 'text' : 'password';
        passwordToggle.classList.toggle('is-visible', visible);
        passwordToggle.setAttribute('aria-pressed', String(visible));
        passwordToggle.setAttribute('aria-label', visible ? 'Hide password' : 'Show password');
      };
      passwordToggle.addEventListener('pointerdown', togglePassword);
      passwordToggle.addEventListener('click', togglePassword);
    }
  }

  // --- burger menu (open/close + actions) ---
  function openMenu() {
    const pop = $('menuPop'); if (pop) pop.style.display = '';
    const btn = $('menuBtn'); if (btn) btn.setAttribute('aria-expanded', 'true');
  }
  function closeMenu() {
    const pop = $('menuPop'); if (pop) pop.style.display = 'none';
    const btn = $('menuBtn'); if (btn) btn.setAttribute('aria-expanded', 'false');
  }
  const menuBtn = $('menuBtn');
  if (menuBtn) {
    menuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const pop = $('menuPop');
      if (!pop) return;
      if (pop.style.display === 'none') openMenu(); else closeMenu();
    });
    // Close on outside click
    document.addEventListener('click', (e) => {
      const wrap = $('menuWrap');
      if (wrap && !wrap.contains(e.target)) closeMenu();
    });
    // Close on Escape
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeMenu();
    });
  }
  const menuLogout = $('menuLogout');
  if (menuLogout) {
    menuLogout.addEventListener('click', async () => {
      closeMenu();
      await fetch('/api/v1/auth/logout', { method: 'POST', credentials: 'same-origin' });
      location.href = '/';
    });
  }

  // Phase 15.12: New conversation (reset current (entity, user) conversation)
  const menuNewConversation = $('menuNewConversation');
  if (menuNewConversation) {
    menuNewConversation.addEventListener('click', async () => {
      closeMenu();
      if (!confirm('Start a new conversation? This will clear all messages in this chat (permanent memories will be kept).')) {
        return;
      }
      try {
        const r = await fetch('/api/v1/entities/' + encodeURIComponent(entity) + '/reset-conversation', {
          method: 'POST',
          credentials: 'same-origin',
        });
        if (!r.ok) {
          const data = await r.json().catch(() => ({}));
          alert('Failed to start new conversation: ' + (data.error || r.status));
          return;
        }
        // Reload the page to start fresh
        location.reload();
      } catch (e) {
        alert('Failed to start new conversation: ' + e);
      }
    });
  }

  // --- per-entity icon upload (chat page burger menu) ---
  let currentEntityIconUrl = null;
  const iconFileInput = $('iconFileInput');
  const menuChangeImage = $('menuChangeImage');
  if (menuChangeImage && iconFileInput) {
    menuChangeImage.addEventListener('click', () => {
      closeMenu();
      iconFileInput.value = '';
      iconFileInput.click();
    });
    iconFileInput.addEventListener('change', async () => {
      const file = iconFileInput.files && iconFileInput.files[0];
      if (!file) return;
      if (file.size > 2 * 1024 * 1024) {
        alert('Icon is too large (max 2 MiB).');
        return;
      }
      try {
        const fd = new FormData();
        fd.append('file', file, file.name);
        const r = await api('/api/v1/entities/' + encodeURIComponent(entity) + '/icon', {
          method: 'PUT', body: fd,
        });
        if (!r.ok) {
          const data = await r.json().catch(() => ({}));
          alert('Icon upload failed: ' + (data.error || r.status));
          return;
        }
        const data = await r.json();
        currentEntityIconUrl = data.icon_url;
        applyEntityIcon();
        // Make "Remove image" visible now
        const remove = $('menuRemoveImage'); if (remove) remove.style.display = '';
      } catch (e) {
        alert('Icon upload failed: ' + e);
      }
    });
  }
  const menuRemoveImage = $('menuRemoveImage');
  if (menuRemoveImage) {
    menuRemoveImage.addEventListener('click', async () => {
      closeMenu();
      try {
        const r = await api('/api/v1/entities/' + encodeURIComponent(entity) + '/icon', {
          method: 'DELETE',
        });
        if (r.ok) {
          currentEntityIconUrl = null;
          applyEntityIcon();
          const remove = $('menuRemoveImage'); if (remove) remove.style.display = 'none';
        }
      } catch (e) {
        alert('Remove image failed: ' + e);
      }
    });
  }
  function applyEntityIcon() {
    const icon = $('entityIcon');
    if (!icon) return;
    if (currentEntityIconUrl) {
      icon.innerHTML = '<img src="' + currentEntityIconUrl + '" alt="">';
    } else {
      icon.textContent = (entity[0] || '?').toUpperCase();
      icon.innerHTML = icon.textContent;  // reset
    }
  }

  async function loadEntities() {
    if (page !== 'index') return;
    try {
      const r = await api('/api/v1/entities');
      const data = await r.json();
      renderEntities(data.entities || []);
    } catch (e) {}
    // Groups live in their own subsection below the agent rows.
    loadGroups();
  }
  function renderEntities(list) {
    const ul = $('entityList');
    const empty = $('entityEmpty');
    Array.from(ul.querySelectorAll('.entity-row, .entity-edit-input')).forEach(n => n.remove());
    if (list.length === 0) { empty.style.display = ''; return; }
    empty.style.display = 'none';
    for (const e of list) {
      const row = document.createElement('a');
      row.className = 'entity-row';
      row.href = '/c/' + encodeURIComponent(e.name);
      const preview = e.last_message ? escapeHtml(e.last_message) : (e.turn_count ? '(no messages yet)' : 'Start a conversation');
      const time = fmtTimeAgo(e.last_at);
      // Inline change-image: a small file input that uploads on change
      row.innerHTML = `
        ${entityIconHTML(e.name, 44, e.icon_url)}
        <div class="meta">
          <div class="name">${escapeHtml(e.name)}</div>
          <div class="preview">${preview}</div>
        </div>
        <div class="time">${time}</div>
        <label class="edit-btn" title="Change image for ${escapeHtml(e.name)}" aria-label="Change image">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
            <path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
          </svg>
        </label>
        <input type="file" class="entity-edit-input" data-entity="${escapeHtml(e.name)}" accept="image/png,image/jpeg,image/gif,image/webp,image/svg+xml,application/pdf,text/csv,text/html,.csv,.pdf,.html,.htm,.docx,.odx,.odt" style="display:none">
      `;
      // Don't trigger navigation when clicking the edit button
      const editBtn = row.querySelector('.edit-btn');
      const fileInput = row.querySelector('.entity-edit-input');
      if (editBtn && fileInput) {
        editBtn.addEventListener('click', (ev) => {
          ev.preventDefault(); ev.stopPropagation();
          fileInput.click();
        });
        fileInput.addEventListener('change', async (ev) => {
          ev.preventDefault(); ev.stopPropagation();
          const file = ev.target.files && ev.target.files[0];
          if (!file) return;
          if (file.size > 2 * 1024 * 1024) { alert('Icon too large (max 2 MiB).'); return; }
          try {
            const fd = new FormData();
            fd.append('file', file, file.name);
            const r = await api('/api/v1/entities/' + encodeURIComponent(e.name) + '/icon', { method: 'PUT', body: fd });
            if (!r.ok) {
              const data = await r.json().catch(() => ({}));
              alert('Icon upload failed: ' + (data.error || r.status));
              return;
            }
            // Refresh the entities list to pick up the new icon_url
            loadEntities();
          } catch (err) { alert('Icon upload failed: ' + err); }
        });
      }
      ul.appendChild(row);
    }
  }

  // === Groups (2026-10-23) ===============================================
  // The landing page lists the installed agents (Telegram-style rows)
  // and, below them, a "Groups" subsection with the group chats the
  // user is a member of. Groups are created by admins via the
  // "New group" button in the top bar.

  async function loadGroups() {
    if (page !== 'index') return;
    try {
      const r = await api('/api/v1/groups');
      const data = await r.json();
      renderGroups(data.groups || []);
    } catch (e) { console.error('loadGroups failed', e); }
  }

  function renderGroups(list) {
    const ul = $('entityList');
    if (!ul) return;
    // Drop any previously rendered group section (it is re-rendered on
    // every refresh, so the nodes are not tracked individually).
    Array.from(ul.querySelectorAll('.group-row, .list-section')).forEach(n => n.remove());
    if (!list.length) return;
    const header = document.createElement('div');
    header.className = 'list-section';
    header.textContent = 'Groups';
    ul.appendChild(header);
    for (const g of list) {
      const row = document.createElement('a');
      row.className = 'entity-row group-row';
      row.href = '/g/' + encodeURIComponent(g.id);
      const preview = g.last_message
        ? escapeHtml(g.last_message)
        : (g.message_count ? '(no messages yet)' : 'Start a group conversation');
      const time = fmtTimeAgo(g.last_at);
      const roster = (g.agents || []).map(a => '@' + a).join('  ');
      row.innerHTML = `
        ${entityIconHTML(g.name, 44, null)}
        <div class="meta">
          <div class="name">${escapeHtml(g.name)}</div>
          <div class="preview">${preview}</div>
          ${roster ? `<div class="preview roster">${escapeHtml(roster)}</div>` : ''}
        </div>
        <div class="time">${time}</div>
      `;
      ul.appendChild(row);
    }
  }

  // --- new group modal (admin only) ---
  const newGroupBtn = $('newGroupBtn');
  const groupModal = $('groupModal');

  function closeGroupModal() {
    if (groupModal) groupModal.style.display = 'none';
  }
  function groupModalError(msg) {
    const box = $('groupError');
    if (!box) return;
    box.textContent = msg;
    box.style.display = msg ? '' : 'none';
  }
  async function openGroupModal() {
    if (!groupModal) return;
    groupModal.style.display = '';
    groupModalError('');
    const memberList = $('groupMemberList');
    const agentList = $('groupAgentList');
    if (memberList) memberList.innerHTML = '<div class="pick-empty">Loading users…</div>';
    if (agentList) agentList.innerHTML = '<div class="pick-empty">Loading agents…</div>';
    try {
      const usersResp = await api('/api/v1/admin/users');
      const agentsResp = await api('/api/v1/agents');
      const users = (await usersResp.json()).users || [];
      const agents = (await agentsResp.json()).agents || [];
      if (memberList) {
        memberList.innerHTML = '';
        for (const u of users) {
          const label = document.createElement('label');
          label.className = 'pick-item' + (u.is_active ? '' : ' inactive');
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.value = String(u.id);
          cb.className = 'pick-check';
          if (currentUser && u.id === currentUser.id) { cb.checked = true; cb.disabled = true; }
          const text = document.createElement('span');
          text.textContent = (u.display_name || u.email) + ' — ' + u.email +
            (u.role === 'admin' ? ' (admin)' : '') + (u.is_active ? '' : ' (inactive)');
          label.appendChild(cb);
          label.appendChild(text);
          memberList.appendChild(label);
        }
      }
      if (agentList) {
        agentList.innerHTML = '';
        for (const a of agents) {
          const label = document.createElement('label');
          label.className = 'pick-item';
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.value = a;
          cb.checked = true;
          const text = document.createElement('span');
          text.textContent = '@' + a;
          label.appendChild(cb);
          label.appendChild(text);
          agentList.appendChild(label);
        }
      }
    } catch (e) {
      groupModalError('Could not load the user list: ' + (e.message || e));
    }
  }
  if (newGroupBtn) {
    newGroupBtn.addEventListener('click', (e) => { e.stopPropagation(); openGroupModal(); });
  }
  const groupCancel = $('groupCancel');
  if (groupCancel) groupCancel.addEventListener('click', closeGroupModal);
  if (groupModal) {
    groupModal.addEventListener('click', (e) => {
      if (e.target === groupModal) closeGroupModal();
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && groupModal && groupModal.style.display !== 'none') closeGroupModal();
  });
  const groupCreate = $('groupCreate');
  if (groupCreate) {
    groupCreate.addEventListener('click', async () => {
      const nameEl = $('groupName');
      const descEl = $('groupDesc');
      const name = nameEl ? nameEl.value.trim() : '';
      if (!name) { groupModalError('Give the group a name.'); return; }
      const memberIds = Array.from(
        document.querySelectorAll('#groupMemberList input:checked')
      ).map(i => Number(i.value)).filter(n => !isNaN(n));
      const agentNames = Array.from(
        document.querySelectorAll('#groupAgentList input:checked')
      ).map(i => i.value);
      groupCreate.disabled = true;
      groupModalError('');
      try {
        const r = await api('/api/v1/groups', {
          method: 'POST',
          body: {
            name: name,
            description: descEl ? descEl.value.trim() : '',
            member_ids: memberIds,
            agent_names: agentNames,
          },
        });
        const data = await r.json();
        location.href = '/g/' + encodeURIComponent(data.group.id);
      } catch (e) {
        groupModalError(e.message || String(e));
        groupCreate.disabled = false;
      }
    });
  }

  let inFlight = false;
  let pendingFiles = [];
  const messages = $('messages');

  async function loadTurns() {
    if (page !== 'chat' || !entity) { console.log('[harness] loadTurns skipped'); return; }
    console.log('[harness] loadTurns: fetching turns for', entity);
    try {
      const r = await api('/api/v1/entities/' + encodeURIComponent(entity) + '/turns?limit=50');
      const data = await r.json();
      console.log('[harness] loadTurns: got', (data.turns || []).length, 'turns');
      renderTurns((data.turns || []).slice().reverse());
    } catch (e) { console.error('[harness] loadTurns failed:', e); }
  }
  // === Live-turn reattach (2026-10-22) ==================================
  // The streaming bubble is client-side DOM. A browser refresh (or a
  // dropped connection) wipes it even though the turn keeps running
  // server-side -- on 2026-10-21 Evie worked for 40+ minutes on a turn
  // whose bubble vanished on refresh, and the chat looked empty.
  //
  // GET /api/v1/entities/<name>/turns/active exposes the in-flight
  // turn's buffered NDJSON events, so the bubble can be rebuilt from
  // scratch: iteration count, tool calls and the partial text.
  let activePollTimer = null;
  let sawActiveTurn = false;
  let activePollMisses = 0;

  function renderActiveTurn(state) {
    const events = state.events || [];
    let div = document.getElementById('activeTurnBubble');
    if (!div) {
      div = document.createElement('div');
      div.id = 'activeTurnBubble';
      div.className = 'msg assistant live';
      div.innerHTML = '<div class="live-body"></div>'
        + '<details class="trace" open><summary class="trace-summary"></summary>'
        + '<ol class="trace-list"></ol></details>';
      messages.appendChild(div);
    }
    const body = div.querySelector('.live-body');
    const list = div.querySelector('.trace-list');
    const summary = div.querySelector('.trace-summary');
    let iter = 0, toolCount = 0, lastText = '', delta = '';
    list.innerHTML = '';
    for (const ev of events) {
      if (!ev || !ev.type) continue;
      if (ev.type === 'iter_start') {
        if (ev.iter !== undefined && ev.iter !== null) iter = ev.iter;
      } else if (ev.type === 'delta') {
        delta += ev.text || '';
      } else if (ev.type === 'iter_text') {
        if (ev.text) lastText = ev.text;
      } else if (ev.type === 'tool_start') {
        toolCount += 1;
        const li = document.createElement('li');
        li.className = 'trace-item pending';
        li.dataset.tool = ev.name || '';
        li.textContent = '\ud83d\udd27 ' + (ev.name || '?');
        list.appendChild(li);
      } else if (ev.type === 'tool_end') {
        const pending = list.querySelector(
          'li.trace-item.pending[data-tool="' + CSS.escape(ev.name || '') + '"]:last-of-type');
        if (pending) {
          pending.classList.remove('pending');
          pending.classList.add(ev.ok ? 'ok' : 'err');
        } else {
          const li = document.createElement('li');
          li.className = 'trace-item ' + (ev.ok ? 'ok' : 'err');
          li.textContent = (ev.ok ? '\u2713 ' : '\u2717 ') + (ev.name || '?');
          list.appendChild(li);
        }
      }
    }
    const shown = lastText || delta;
    body.textContent = shown ? shown + ' \u258d'
      : '\u23f3 thinking... (iter ' + iter + ')';
    summary.textContent = 'iter ' + iter + ' \u00b7 ' + toolCount + ' tool call'
      + (toolCount === 1 ? '' : 's') + ' \u00b7 ' + Math.round(state.elapsed_s || 0)
      + 's \u00b7 reattached';
    requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
  }

  async function pollActiveTurn() {
    if (page !== 'chat' || !entity) return;
    // While a local stream is running, it owns the bubble.
    if (inFlight) return;
    try {
      const r = await api('/api/v1/entities/' + encodeURIComponent(entity) + '/turns/active');
      if (!r.ok) {
        // 404 = the running server predates this endpoint (the worker is
        // still on the previous code). Stop polling instead of asking
        // twice a second for something that will never arrive; the poll
        // restarts on the next page load, by which time the server has
        // been restarted onto the new code.
        if (r.status === 404) {
          activePollMisses += 1;
          if (activePollMisses >= 2 && activePollTimer) {
            clearInterval(activePollTimer);
            activePollTimer = null;
          }
        }
        return;
      }
      activePollMisses = 0;
      const st = await r.json();
      const bubble = document.getElementById('activeTurnBubble');
      if (st && st.active) {
        sawActiveTurn = true;
        renderActiveTurn(st);
      } else {
        if (bubble) bubble.remove();
        if (sawActiveTurn) { sawActiveTurn = false; await loadTurns(); }
      }
    } catch (e) { /* harness restarting, or offline -- try again next tick */ }
  }

  function startActivePolling() {
    if (activePollTimer) return;
    activePollTimer = setInterval(pollActiveTurn, 2000);
    pollActiveTurn();
  }

  function renderTurns(turns) {
    if (!messages) { console.error('[harness] renderTurns: messages element missing'); return; }
    messages.innerHTML = '';
    if (turns.length === 0) { const empty = $('empty'); if (empty) empty.style.display = ''; return; }
    const empty = $('empty'); if (empty) empty.style.display = 'none';
    for (const t of turns) appendTurn(t, false);
    requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
  }
  // Format a byte count as a short human-readable string. Used
  // for the file-attachment chip caption so the user can tell at
  // a glance whether the file is a 1 KB config snippet or a 9
  // MiB log dump.
  function fmtSize(n) {
    if (typeof n !== 'number' || n <= 0 || !isFinite(n)) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB';
    return (n / 1024 / 1024).toFixed(n < 10 * 1024 * 1024 ? 1 : 0) + ' MB';
  }
  // Build a single file-attachment chip for the assistant/user
  // bubble. The shape is the same as user uploads: a small icon,
  // the filename, an optional size hint, and a link to the served
  // URL. The download attribute makes the browser save the file
  // with the right name instead of using the uuid-suffixed URL.
  function attachmentChip(a) {
    const isImg = (a.content_type || '').startsWith('image/');
    const url = escapeHtml(a.url);
    const fname = escapeHtml(a.filename || 'file');
    const size = fmtSize(a.size);
    const icon = isImg
      ? '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>'
      : '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>';
    return `<a class="att-chip" href="${url}" download="${fname}" target="_blank" rel="noopener" title="${fname}${size ? ' (' + size + ')' : ''}">${icon}<span class="att-name">${fname}</span>${size ? `<span class="att-size">${size}</span>` : ''}</a>`;
  }
  function renderAttachments(atts) {
    if (!atts || !atts.length) return '';
    const imgs = atts.filter(a => (a.content_type || '').startsWith('image/'));
    const files = atts.filter(a => !(a.content_type || '').startsWith('image/'));
    let html = '';
    if (imgs.length) {
      html += '<div class="attachments imgs">' + imgs.map(a => `<a href="${escapeHtml(a.url)}" target="_blank" rel="noopener"><img src="${escapeHtml(a.url)}" alt=""></a>`).join('') + '</div>';
    }
    if (files.length) {
      html += '<div class="attachments files">' + files.map(attachmentChip).join('') + '</div>';
    }
    return html;
  }
  // A reverie turn (2026-10-23) IS a real turn - it is in the turns
  // table like any other - but its prompt is a long machine-generated
  // brief, not something the user typed. Rendering it as a giant blue
  // "user said this" bubble would be unreadable, so it gets its own
  // collapsed block instead.
  function isReverieTurn(t) {
    return !!(t && t.role === 'user' && typeof t.content === 'string'
      && t.content.trimStart().startsWith('<reverie'));
  }
  function appendTurn(t, scroll) {
    const empty = $('empty'); if (empty) empty.style.display = 'none';
    const div = document.createElement('div');
    const time = t.created_at ? new Date(t.created_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '';
    if (isReverieTurn(t)) {
      const m = /agent="([^"]*)"/.exec(t.content || '');
      const agent = m ? m[1] : '';
      div.className = 'msg reverie';
      div.innerHTML =
        '<details class="reverie-brief"><summary>nightly reverie' +
        (agent ? ' &middot; ' + escapeHtml(agent) : '') + '</summary>' +
        '<pre>' + escapeHtml(t.content) + '</pre></details>' +
        '<div class="meta">reverie' + (time ? ' · ' + time : '') + '</div>';
      messages.appendChild(div);
      if (scroll !== false) requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
      return;
    }
    div.className = 'msg ' + t.role;
    const attsHtml = renderAttachments(t.attachments || []);
    // Phase 16.2 (2026-10-20): render markdown on assistant messages so
    // headings, lists, code, tables, bold/italic survive the trip from
    // the model to the chat UI. User messages stay plain-text (their
    // input is treated as literal — markdown inside a user message
    // is just a thing they typed). The streaming path uses plain
    // textContent during typing (avoids flicker) and applies markdown
    // once the response is final — see the final-event handler below.
    const bodyHtml = t.role === 'assistant' ? renderMarkdown(t.content) : escapeHtml(t.content || '');
    div.innerHTML = `${bodyHtml}${attsHtml}<div class="meta">${t.role}${time ? ' · ' + time : ''}</div>`;
    messages.appendChild(div);
    if (scroll !== false) requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
  }

  const form = $('form');
  const input = $('input');
  const sendBtn = $('send');
  const attachBtn = $('attachBtn');
  const attachInput = $('attachInput');
  const pendingAttachmentsEl = $('pendingAttachments');

  if (form && input && sendBtn) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (inFlight) return;
      const text = input.value.trim();
      if (!text && pendingFiles.length === 0) return;
      const attachments = [];
      for (const f of pendingFiles) {
        try {
          const fd = new FormData();
          fd.append('file', f.blob, f.filename);
          const ur = await api('/api/v1/uploads', { method: 'POST', body: fd });
          if (ur.ok) {
            const ud = await ur.json();
            attachments.push({url: ud.url, filename: ud.filename, content_type: ud.content_type, size: ud.size});
          }
        } catch (e) { console.error('upload failed', e); }
      }
      appendTurn({role: 'user', content: text || '(attachment)', attachments, created_at: new Date().toISOString()}, true);
      input.value = ''; autoGrow();
      pendingFiles = []; renderPending();
      // Show the typing indicator next to the entity name while we
      // wait for the LLM. Mirrors the Telegram UX.
      const ti = $('typingIndicator'); if (ti) ti.classList.add('is-active');
      inFlight = true;
      sendBtn.disabled = true;
      const sendT0 = Date.now();
      try {
        // Use the streaming path (NDJSON) when the browser supports
        // it. This is the 524 fix: long-running turns stream events
        // as the model works, so the connection stays alive and
        // Cloudflare doesn't cut us off at its 125s origin timeout.
        // Falls back to the old buffered POST on any failure
        // (ReadableStream not supported, network blip, etc.).
        const supportsStream = typeof Response !== 'undefined' &&
                               typeof ReadableStream !== 'undefined' &&
                               typeof fetch === 'function';
        if (supportsStream) {
          await sendStreaming(text, attachments, sendT0);
        } else {
          await sendBuffered(text, attachments);
        }
      } catch (e) {
        const msg = e.message || 'Network error';
        appendTurn({role: 'assistant', content: '⚠️ ' + msg, created_at: new Date().toISOString()}, true);
        showToast('Send failed: ' + msg, true);
        if (msg === 'unauthorized') showLogin();  // session expired -> show login
        // Persist the failure server-side (2026-10-22). The ⚠️ bubble
        // above lives only in the DOM -- without this, the next refresh
        // makes the failure look like silence again, which is how Mal
        // lost an Evie turn to a server restart on 2026-10-21.
        // The endpoint is idempotent: if the server already wrote
        // something for this turn (partial turn, interrupted marker, or
        // the real answer) it does nothing.
        try {
          await fetch('/api/v1/entities/' + encodeURIComponent(entity) + '/turns/error', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message: msg, request_id: e.requestId || null}),
          });
        } catch (e2) {
          console.error('could not persist stream error:', e2);
        }
      } finally {
        const ti = $('typingIndicator'); if (ti) ti.classList.remove('is-active');
        inFlight = false;
        sendBtn.disabled = false;
        input.focus();
      }
    });

    // === Streaming chat (NDJSON) ======================================
    // Renders events from /api/v1/entities/<name>/chat as they arrive.
    // One live assistant bubble with role="assistant live" + a small
    // <details> trace block at the bottom shows the tool calls as
    // they happen. On 'final', the bubble is replaced with the final
    // assistant text and the trace collapses into a single
    // "<N tool calls · M sec>" line.
    async function sendStreaming(text, attachments, sendT0) {
      const liveDiv = document.createElement('div');
      liveDiv.className = 'msg assistant live';
      const liveBody = document.createElement('div');
      liveBody.className = 'live-body';
      liveBody.textContent = '…';
      const liveTrace = document.createElement('details');
      liveTrace.className = 'trace';
      // For admin users, default the trace to OPEN so all
      // thought-stream items (think / tool_start / tool_end / nudge)
      // are visible during streaming too. The final-bubble
      // transition below keeps it open for admin (non-admin gets
      // collapsed at final time). This is the user-facing
      // expression of "display the thought stream permanently".
      if (currentUser && currentUser.role === 'admin') {
        liveTrace.setAttribute('open', '');
      }
      const liveSummary = document.createElement('summary');
      liveSummary.textContent = 'working...';
      const liveTraceList = document.createElement('ol');
      liveTraceList.className = 'trace-list';
      liveTrace.appendChild(liveSummary);
      liveTrace.appendChild(liveTraceList);
      const liveMeta = document.createElement('div');
      liveMeta.className = 'meta';
      liveMeta.textContent = 'assistant';
      liveDiv.appendChild(liveBody);
      liveDiv.appendChild(liveTrace);
      liveDiv.appendChild(liveMeta);
      const messagesEl = $('messages');
      if (messagesEl) messagesEl.appendChild(liveDiv);
      requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });

      let traceItems = 0;
      let iterCount = 0;
      let lastText = '';
      // 2026-10-21: streamed token fragments arriving mid-call.
      // Kept separate from lastText so the authoritative full text
      // from iter_text overwrites rather than appends to a partial.
      let liveDelta = '';
      // Phase 15 (2026-10-18): track attachments produced during
      // the streaming run so we can render them on the live bubble
      // and on the final bubble. Mirrors the user-side attachment
      // list and the historical turn shape.
      const liveAtts = [];
      // Helper: re-render the attachment block on a given bubble.
      // We replace the inner HTML of the .attachments-files /
      // .attachments-imgs containers so the bubble stays in sync
      // with the streaming event stream.
      const renderLiveAtts = () => {
        // Remove old attachment blocks (re-render from scratch)
        Array.from(liveDiv.querySelectorAll(':scope > .attachments')).forEach(n => n.remove());
        const html = renderAttachments(liveAtts);
        if (!html) return;
        // Insert AFTER liveBody (so the order is: text, then atts,
        // then trace, then meta — matching the non-streaming shape).
        const tmp = document.createElement('div');
        tmp.innerHTML = html;
        while (tmp.firstChild) liveBody.after(tmp.firstChild);
        requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });
      };
      const r = await fetch('/api/v1/entities/' + encodeURIComponent(entity) + '/chat', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/x-ndjson',
        },
        body: JSON.stringify({message: text, attachments}),
      });
      if (r.status === 401) throw new Error('unauthorized');
      if (!r.ok) {
        let detail = '';
        try { detail = (await r.json()).error || ''; } catch (e) {}
        throw new Error(detail || ('HTTP ' + r.status));
      }
      // Correlate a stream failure with the request that started it.
      // The server echoes the same id back on the error POST, so the
      // failure can be matched to its turn.
      const streamRequestId = r.headers.get('X-Request-Id') || null;
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let finalEvent = null;
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        let nl;
        while ((nl = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, nl);
          buffer = buffer.slice(nl + 1);
          if (!line.trim()) continue;
          let ev;
          try { ev = JSON.parse(line); } catch (e) { console.warn('bad NDJSON line:', line); continue; }
          if (ev.type === 'iter_start') {
            iterCount += 1;
              liveDelta = '';
            liveBody.textContent = '⏳ thinking... (iter ' + ev.iter + ')';
            liveSummary.textContent = 'iter ' + ev.iter + '...';
            requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });
            } else if (ev.type === 'delta') {
              liveDelta += ev.text || '';
              liveBody.textContent = liveDelta + ' \u258d';
              liveSummary.textContent = 'streaming...';
              requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });
          } else if (ev.type === 'iter_text') {
            if (ev.text) {
              lastText = ev.text;
              liveBody.textContent = ev.text + ' ▍';
              requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });
            }
          } else if (ev.type === 'tool_start') {
            const li = document.createElement('li');
            const argsStr = ev.args ? JSON.stringify(ev.args) : '';
            li.textContent = '🔧 ' + ev.name + '(' + (argsStr.length > 80 ? argsStr.slice(0, 80) + '…' : argsStr) + ')';
            li.className = 'trace-item pending';
            li.dataset.tool = ev.name;
            liveTraceList.appendChild(li);
            traceItems += 1;
            liveSummary.textContent = traceItems + ' tool call' + (traceItems === 1 ? '' : 's') + '...';
            requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });
          } else if (ev.type === 'tool_end') {
            // Mark the matching pending item as done (or append a new one)
            const pending = liveTraceList.querySelector('li.trace-item.pending[data-tool="' + CSS.escape(ev.name) + '"]:last-of-type');
            if (pending) {
              pending.classList.remove('pending');
              pending.classList.add(ev.ok ? 'ok' : 'err');
              if (!ev.ok) pending.title = ev.result_preview || '';
            } else {
              const li = document.createElement('li');
              li.textContent = '✓ ' + ev.name;
              li.className = 'trace-item ' + (ev.ok ? 'ok' : 'err');
              liveTraceList.appendChild(li);
            }
            liveSummary.textContent = traceItems + ' tool call' + (traceItems === 1 ? '' : 's') + ' · ' + ((Date.now() - sendT0) / 1000).toFixed(1) + 's';
            requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });
          } else if (ev.type === 'attachment') {
            // Phase 15: attach_file produced an attachment. Add to
            // the live bubble's attachment list and re-render the
            // chip block. Dedupe by URL (defence against the model
            // calling attach_file twice with the same path).
            if (ev.attachment && ev.attachment.url) {
              if (!liveAtts.some(a => a.url === ev.attachment.url)) {
                liveAtts.push(ev.attachment);
                renderLiveAtts();
              }
            }
          } else if (ev.type === 'nudge') {
            const li = document.createElement('li');
            li.textContent = '⚠️ model narrated a tool call — nudging for retry';
            li.className = 'trace-item nudge';
            liveTraceList.appendChild(li);
            liveTrace.open = true;
            requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });
          } else if (ev.type === 'final') {
            finalEvent = ev;
          } else if (ev.type === 'error') {
            // Don't throw — render an error bubble in the chat instead.
            // Phase 16.3 (2026-10-20): surfaced after a 529 from MiniMax
            // mid-iter-3. The retry path catches transient 5xx/429 in the
            // adapter; the user only sees this bubble if all retries
            // failed (~7s of sustained outages).
            streamingError = ev;
            break;
          }
        }
      }
      if (!finalEvent) {
        // Prefer the server's actual error over the generic message.
        // 2026-10-22 15:33: the server failed instantly with "Working
        // outside of application context" (the vision path reading
        // current_app from the loop's daemon thread) and all the user
        // saw was "stream ended without a final event" -- which sent
        // the investigation after the wrong thing entirely. The server
        // message is the actionable one.
        const detail = (streamingError && streamingError.message)
          ? streamingError.message
          : 'stream ended without a final event';
        const err = new Error(detail);
        err.requestId = streamRequestId;
        throw err;
      }
      // Replace the live bubble with the final assistant turn.
      const resp = finalEvent.response || {};
      const finalText = resp.assistant || lastText || '(no response)';
      const lat = finalEvent.latency_ms || (Date.now() - sendT0);
      const iters = finalEvent.tool_iterations || iterCount;
      liveDiv.classList.remove('live');
      // Phase 16.2 (2026-10-20): now that the response is final, swap
      // the plain textContent (used during typing to avoid markdown
      // re-render flicker on every chunk) for the rendered HTML.
      liveBody.innerHTML = renderMarkdown(finalText);
      // Phase 15: render the final attachment list. The server
      // includes this in the `attachments` key on the final event
      // (Phase 15 addition). Prefer the server list over the
      // stream-derived `liveAtts` so we don't render stale state
      // if the user navigated or the stream was partial.
      const finalAtts = Array.isArray(finalEvent.attachments) ? finalEvent.attachments : liveAtts;
      if (finalAtts && finalAtts.length) {
        liveAtts.length = 0;
        for (const a of finalAtts) liveAtts.push(a);
        renderLiveAtts();
      } else {
        // Ensure no stale chip from streaming remains
        Array.from(liveDiv.querySelectorAll(':scope > .attachments')).forEach(n => n.remove());
      }
      // For admin users, keep the thought stream visible in the
      // final bubble (no collapse, no max-height cap, keep the
      // live-style summary text). For non-admins, collapse to a
      // static 'N iter · M tool calls · Zs' summary as before.
      const isAdmin = !!(currentUser && currentUser.role === 'admin');
      if (isAdmin) {
        // Drop the max-height cap so all trace items are scrollable
        // (still inside the <details>, but defaults to open below).
        liveTraceList.style.maxHeight = '';
        // Keep the <details> open so the trace is visible by default.
        liveTrace.setAttribute('open', '');
        // Keep the live-style summary (the last iter label) rather
        // than the static count. If we never got a tool_start or
        // nudge event, fall back to a static count.
        const liveSummaryText = liveSummary.textContent;
        if (!liveSummaryText || liveSummaryText === 'working...' || liveSummaryText === 'iter ' + iters + '...') {
          // already in a live-style form, leave it
        } else if (liveSummaryText.includes('thinking')) {
          // thinking... (iter N) — keep
        } else {
          // static count was set; re-set to the iter label
          liveSummary.textContent = 'iter ' + iters + '...';
        }
      } else {
        liveTraceList.style.maxHeight = '160px';
        liveSummary.textContent = iters + ' iter · ' + traceItems + ' tool call' + (traceItems === 1 ? '' : 's') + ' · ' + (lat / 1000).toFixed(1) + 's';
        liveTrace.removeAttribute('open');
      }
      const time = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
      const adminMarker = isAdmin ? ' · admin view' : '';
      liveMeta.textContent = 'assistant · ' + (finalEvent.model || '') + ' · ' + time + adminMarker;
    }

    // === Non-streaming fallback ========================================
    // Kept for older browsers that lack ReadableStream.
    async function sendBuffered(text, attachments) {
      const r = await api('/api/v1/entities/' + encodeURIComponent(entity) + '/chat', {
        method: 'POST',
        body: {message: text, attachments},
      });
      const data = await r.json();
      // Phase 15: the buffered (non-streaming) path also returns
      // an `attachments` list in the response. Pass it through to
      // appendTurn so the file-attachment chips render.
      appendTurn({
        role: 'assistant',
        content: data.assistant || '(no response)',
        attachments: Array.isArray(data.attachments) ? data.attachments : [],
        created_at: new Date().toISOString(),
      }, true);
    }
    const autoGrow = () => { input.style.height = 'auto'; input.style.height = Math.min(160, input.scrollHeight) + 'px'; };
    input.addEventListener('input', autoGrow);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        // form.requestSubmit() fires a *trusted* submit event that runs
        // our listener and respects preventDefault. dispatchEvent(new
        // Event('submit')) is deprecated AND, in Firefox, triggers a
        // duplicate native form submission (which navigates away and
        // makes the optimistic message appear to 'flash then disappear').
        if (typeof form.requestSubmit === 'function') form.requestSubmit();
        else form.dispatchEvent(new Event('submit', {cancelable: true, bubbles: true}));
      }
    });
    input.addEventListener('paste', (e) => {
      const items = (e.clipboardData || e.originalEvent.clipboardData).items || [];
      for (const it of items) {
        if (it.kind === 'file' && it.type.startsWith('image/')) {
          const file = it.getAsFile();
          if (file) addPendingFile(file, file.name || 'pasted.png');
        }
      }
    });
  }
  if (attachBtn && attachInput) {
    attachBtn.addEventListener('click', () => attachInput.click());
    attachInput.addEventListener('change', () => {
      for (const f of attachInput.files) addPendingFile(f, f.name);
      attachInput.value = '';
    });
  }
  function addPendingFile(blob, filename) {
    const id = 'p' + Math.random().toString(36).slice(2, 9);
    const blobUrl = URL.createObjectURL(blob);
    pendingFiles.push({id, blob, filename, blobUrl});
    renderPending();
  }
  function renderPending() {
    if (!pendingAttachmentsEl) return;
    pendingAttachmentsEl.innerHTML = '';
    for (const f of pendingFiles) {
      const chip = document.createElement('div');
      chip.className = 'chip';
      chip.innerHTML = `<img src="${f.blobUrl}" alt=""><span>${escapeHtml(f.filename)}</span><button type="button" class="remove" data-id="${f.id}">×</button>`;
      pendingAttachmentsEl.appendChild(chip);
    }
    pendingAttachmentsEl.querySelectorAll('.remove').forEach(btn => {
      btn.addEventListener('click', () => {
        const id = btn.dataset.id;
        const idx = pendingFiles.findIndex(f => f.id === id);
        if (idx >= 0) { try { URL.revokeObjectURL(pendingFiles[idx].blobUrl); } catch (e) {} pendingFiles.splice(idx, 1); renderPending(); }
      });
    });
  }

  if (page === 'chat' && entity) {
    const icon = $('entityIcon');
    if (icon) { icon.style.background = colorFor(entity); }
    // Try to load any custom icon. If the API returns 200, use it; else fallback.
    api('/api/v1/entities/' + encodeURIComponent(entity) + '/icon', { method: 'GET' })
      .then(r => {
        if (r.ok) {
          currentEntityIconUrl = '/api/v1/entities/' + encodeURIComponent(entity) + '/icon';
          applyEntityIcon();
          const remove = $('menuRemoveImage'); if (remove) remove.style.display = '';
        }
      })
      .catch(() => {});
  }

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/static/sw.js').catch(() => {});
    });
    // If a new SW takes over (cache version bump), reload so the page
    // picks up the new app.js — fixes 'stale PWA showing old behaviour'
    // issues when the user has the harness app pinned to their home
    // screen.
    navigator.serviceWorker.addEventListener('message', (event) => {
      if (event.data && event.data.type === 'sw-updated') {
        console.log('[harness] new service worker active; reloading');
        location.reload();
      }
    });
  }

  (async () => {
    const user = await checkAuth();
    if (user) {
      if (page === 'index') loadEntities();
      else { loadTurns(); startActivePolling(); }
    }
  })();
})();
