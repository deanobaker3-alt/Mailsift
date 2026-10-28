(() => {
  'use strict';

  // ------------------------------------------------------------------ setup
  const TOKEN = document.querySelector('meta[name="mailsift-token"]').content;
  const $ = (id) => document.getElementById(id);
  const main = $('main');
  const actionbar = $('actionbar');
  const dialog = $('confirm');

  const SCOPES = { '12m': 'last 12 months', '2y': 'last 2 years', all: 'everything' };
  const UNSUBSCRIBABLE = new Set(['oneclick', 'mailto', 'link']);
  const ACTIONS_FOR = {
    oneclick: ['keep', 'unsub', 'unsubdel', 'block'],
    mailto: ['keep', 'unsub', 'unsubdel', 'block'],
    link: ['keep', 'unsub', 'unsubdel', 'block'],
    none: ['keep', 'block'],
    unverified: ['keep', 'block'],
    dodgy: ['keep', 'block'],
  };
  const ACTION_LABEL = { keep: 'keep', unsub: 'unsub', unsubdel: 'unsub+purge', block: 'block' };
  const ACTION_TITLE = {
    keep: 'Leave this sender alone',
    unsub: 'Unsubscribe from this sender',
    unsubdel: 'Unsubscribe and move their old emails to Deleted Items',
    block: 'Block: future emails go to Junk, old ones too',
  };
  const KIND = {
    oneclick: ['1-click', 'chip-green', 'Supports one-click unsubscribe (RFC 8058)'],
    mailto: ['mailto', 'chip-green', 'Unsubscribes by sending them an email'],
    link: ['web form', 'chip-cyan', 'Unsubscribe needs one click from you on their website'],
    none: ['no unsub', 'chip-dim', 'Mailing list with no unsubscribe option'],
    unverified: ['unverified', 'chip-amber', "Couldn't verify who sent these: block only"],
    dodgy: ['suspicious', 'chip-red', 'Looks dodgy: block only, never unsubscribe'],
  };
  const SORTS = {
    count: ['most emails', (a, b) => b.count - a.count],
    rate: ['most per month', (a, b) => b.per_month - a.per_month],
    open: ['least opened', (a, b) => a.open_pct - b.open_pct || b.count - a.count],
    recent: ['most recent', (a, b) => (a.last < b.last ? 1 : a.last > b.last ? -1 : 0)],
    name: ['name a–z', (a, b) => a.name.localeCompare(b.name)],
  };
  const PAGE = 150;

  const S = {
    status: null, scope: '12m', senders: [], choices: {}, tab: 'all', q: '', sort: 'count',
    limit: PAGE, timer: null, quitting: false,
  };

  // ---------------------------------------------------------------- helpers
  // All DOM is built with createElement + textContent: sender names come
  // from emails and are never interpreted as HTML.
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key.startsWith('on')) el.addEventListener(key.slice(2).toLowerCase(), value);
      else el.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function panel(title, ...body) {
    const right = Array.isArray(title) ? title[1] : null;
    const label = Array.isArray(title) ? title[0] : title;
    return h('section', { class: 'panel' }, h('div', { class: 'panel-title' }, h('span', {}, label), right), body);
  }

  function stopPolling() { if (S.timer) clearTimeout(S.timer); S.timer = null; }

  function show(...nodes) {
    stopPolling();
    main.replaceChildren(...nodes);
    actionbar.hidden = true;
    window.scrollTo(0, 0);
  }

  function announce(text) { $('announce').textContent = text; }

  async function api(path, body) {
    const opts = { cache: 'no-store', headers: {}, credentials: 'same-origin' };
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers['Content-Type'] = 'application/json';
      opts.headers['X-MailSift-Token'] = TOKEN;
      opts.body = JSON.stringify(body);
    }
    const response = await fetch(path, opts);
    let data = {};
    try { data = await response.json(); } catch (e) { /* empty */ }
    if (response.status === 401 && data.error === 'locked') { window.location.replace('/'); throw new Error('locked'); }
    if (!response.ok) {
      const err = new Error(data.message || data.error || `request failed (${response.status})`);
      err.status = response.status;
      throw err;
    }
    return data;
  }

  function poll(fn, ms, onError) {
    stopPolling();
    const tick = async () => {
      let again = false;
      try { again = await fn(); } catch (e) { if (e.message !== 'locked') (onError || renderScan)(e.message); return; }
      if (again) S.timer = setTimeout(tick, ms);
    };
    S.timer = setTimeout(tick, ms);
  }

  const fmt = (n) => Number(n || 0).toLocaleString('en-GB');
  const pct = (n, d) => (d ? Math.min(100, Math.round((100 * n) / d)) : 0);

  function bar(percent, width) {
    const w = width || 34;
    const filled = Math.round((w * percent) / 100);
    return `[${'█'.repeat(filled)}${'░'.repeat(w - filled)}] ${String(percent).padStart(3)}%`;
  }

  function duration(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '—';
    const s = Math.round(seconds);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
  }

  function relDate(iso) {
    if (!iso) return '—';
    const then = new Date(iso);
    const days = Math.floor((Date.now() - then.getTime()) / 86400000);
    if (days <= 0) return 'today';
    if (days === 1) return '1d ago';
    if (days < 30) return `${days}d ago`;
    if (days < 365) return `${Math.floor(days / 30)}mo ago`;
    return then.toLocaleDateString('en-GB', { month: 'short', year: 'numeric' });
  }

  function logClass(line) {
    const l = line.toLowerCase();
    if (/error|fail|refused/.test(l)) return 'bad';
    if (/backing off|429|throttl|cancel/.test(l)) return 'warn';
    if (/complete|done/.test(l)) return 'ok';
    if (/worker|range|started|reports/.test(l)) return 'info';
    return '';
  }

  function renderLog(el, lines) {
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.replaceChildren(...lines.map((line) => h('div', { class: logClass(line) }, line)));
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }

  function banner() { return $('banner-art').content.cloneNode(true); }

  // ------------------------------------------------------------------ chrome
  function updateChrome(st) {
    $('version').textContent = `v${st.version}`;
    $('session').hidden = !st.signed_in;
    $('session-anon').hidden = st.signed_in;
    $('account-name').textContent = st.user || '';
    const tokens = st.demo ? 'tokens: n/a (demo)' : st.token_store === 'encrypted' ? 'tokens: OS-encrypted'
      : st.token_store === 'memory-only' ? 'tokens: memory-only' : 'tokens: none';
    $('strip-tokens').textContent = tokens;
    const mode = $('strip-mode');
    mode.className = `mode ${st.demo ? 'demo' : st.dry_run ? 'dry' : 'live'}`;
    mode.textContent = `mode: ${st.demo ? 'DEMO' : st.dry_run ? 'DRY RUN' : 'LIVE'}`;
  }

  async function quit() {
    S.quitting = true;
    try { await api('/api/quit', {}); } catch (e) { /* already gone */ }
    clearInterval(S.heartbeat);
    $('session').hidden = true; $('session-anon').hidden = true;
    show(h('section', { class: 'panel hero' },
      h('div', { class: 'panel-title' }, 'process // terminated'),
      banner(),
      h('h1', {}, 'MailSift has stopped'),
      h('p', { class: 'lede' }, 'You can close this tab. Launch MailSift again any time.'),
      h('p', { class: 'hint' }, '$ exit 0')));
  }
  $('quit').addEventListener('click', quit);
  $('quit-anon').addEventListener('click', quit);
  $('signout').addEventListener('click', async () => {
    await api('/api/logout', {});
    S.senders = []; S.choices = {};
    boot();
  });

  // keep-alive: lets MailSift close itself a while after you close the tab
  S.heartbeat = setInterval(() => { if (!S.quitting) api('/api/ping').catch(() => {}); }, 30000);

  // -------------------------------------------------------------------- boot
  async function boot() {
    let st;
    try {
      st = S.status = await api('/api/status');
    } catch (e) {
      if (e.message === 'locked') return;
      show(panel('error // unreachable', h('div', { class: 'panel-body' }, h('p', { class: 'error' }, e.message))));
      return;
    }
    updateChrome(st);
    if (!st.configured) return renderSetup();
    if (!st.signed_in) return renderSignIn(st.auth);
    if (st.apply_state === 'running') return renderApplying();
    if (st.scan_state === 'running') return renderScanning();
    if (st.has_results) return loadResults(await api('/api/scan'));
    return renderScan();
  }

  // -------------------------------------------------------------------- setup
  function renderSetup(error) {
    const input = h('input', { id: 'client-id', type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: '00000000-0000-0000-0000-000000000000' });
    const save = async () => {
      try { await api('/api/config', { client_id: input.value.trim() }); boot(); } catch (e) { renderSetup(e.message); }
    };
    show(h('section', { class: 'panel hero' },
      h('div', { class: 'panel-title' }, 'setup // publisher'),
      banner(),
      h('h1', {}, 'No app ID built in'),
      h('p', { class: 'lede' }, 'If someone gave you MailSift, ask them for an updated copy. If you’re the publisher, register the app (see the README), then put its Application (client) ID in mailsift/client_id.txt before building so your users never see this screen.'),
      error ? h('p', { class: 'error', role: 'alert' }, error) : null,
      h('div', { class: 'field' }, h('label', { for: 'client-id' }, 'application (client) id'), input),
      h('button', { type: 'button', class: 'btn btn-go btn-big', onclick: save }, 'save & continue')));
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
    input.focus();
  }

  // ------------------------------------------------------------------ sign in
  function renderSignIn(flow) {
    flow = flow || { state: 'idle' };
    const start = async (mode) => {
      try { renderSignIn(await api('/api/login', mode ? { mode } : {})); } catch (e) { renderSignIn({ state: 'error', message: e.message }); }
    };
    const pending = flow.state === 'pending';
    const body = [];
    if (pending && flow.mode === 'device') {
      body.push(
        h('p', { class: 'prompt' }, 'mailsift auth --device-code', h('span', { class: 'cursor', 'aria-hidden': 'true' })),
        h('p', { class: 'lede' }, 'On any phone or computer, open this page and enter the code:'),
        h('a', { href: flow.verification_uri, target: '_blank', rel: 'noopener noreferrer' }, flow.verification_uri),
        h('div', { class: 'code' }, flow.user_code),
        h('p', { class: 'hint' }, 'waiting for Microsoft…'));
    } else if (pending) {
      body.push(
        h('p', { class: 'prompt' }, 'mailsift auth --provider microsoft', h('span', { class: 'cursor', 'aria-hidden': 'true' })),
        h('p', { class: 'lede' }, 'A Microsoft sign-in tab has opened. Finish signing in there, then come back here.'),
        h('p', { class: 'hint' }, 'waiting for Microsoft…'),
        h('button', { type: 'button', class: 'ghost', onclick: () => start('device') }, 'no tab? use a sign-in code'));
    } else {
      body.push(
        banner(),
        h('h1', {}, 'Sign in to your mailbox'),
        h('p', { class: 'lede' }, 'Hotmail, Outlook.com, Live or MSN. You sign in on Microsoft’s own page, so MailSift never sees your password. Microsoft will ask you to allow:'),
        h('ul', { class: 'scopes' },
          h('li', {}, h('code', {}, 'Mail.ReadWrite'), 'read headers, move mail to Deleted Items/Junk'),
          h('li', {}, h('code', {}, 'Mail.Send'), 'send “unsubscribe” emails where that’s the only option'),
          h('li', {}, h('code', {}, 'MailboxSettings.ReadWrite'), 'create the rule that blocks a sender'),
          h('li', {}, h('code', {}, 'User.Read'), 'show which account is signed in')),
        flow.state === 'error' ? h('p', { class: 'error', role: 'alert' }, flow.message || 'Sign-in failed') : null,
        h('div', { class: 'buttons center' },
          h('button', { type: 'button', class: 'btn btn-go btn-big', onclick: () => start() }, '▶ sign in with microsoft'),
          h('button', { type: 'button', class: 'btn', onclick: () => start('device') }, 'use a code instead')),
        h('p', { class: 'hint' }, 'Remove access any time at account.live.com/consent/Manage'));
    }
    show(h('section', { class: 'panel hero' }, h('div', { class: 'panel-title' }, 'auth // microsoft'), body));
    if (pending) {
      poll(async () => {
        const st = await api('/api/status');
        if (st.signed_in) { boot(); return false; }
        if (st.auth.state !== 'pending') { renderSignIn(st.auth); return false; }
        return true;
      }, 1500, (msg) => renderSignIn({ state: 'error', message: msg }));
    }
  }

  // --------------------------------------------------------------------- scan
  function privacyPanel() {
    const st = S.status;
    const tokenLine = st.demo ? 'Demo mode: no real sign-in is stored.'
      : st.token_store === 'encrypted' ? 'Your sign-in token is stored encrypted by your operating system (Windows DPAPI on Windows), so you don’t have to sign in every time.'
        : 'Your sign-in token is kept in memory only and forgotten when MailSift closes.';
    const forget = () => confirmBox({
      title: 'confirm // forget me',
      body: [h('p', {}, 'This signs you out, deletes your stored sign-in token, and wipes MailSift’s local history (who you unsubscribed from and blocked).'),
        h('p', { class: 'hint' }, 'Your mailbox, and any block rules already created in Outlook, are not touched.')],
      ok: 'forget me',
      onOk: async () => {
        try { await api('/api/forget', {}); S.senders = []; S.choices = {}; boot(); } catch (e) { renderScan(e.message); }
      },
    });
    return h('section', { class: 'panel privacy' },
      h('div', { class: 'panel-title' }, 'privacy // what mailsift keeps'),
      h('div', { class: 'panel-body' },
        h('ul', {},
          h('li', {}, 'Your password never touches MailSift: you sign in on Microsoft’s page.'),
          h('li', {}, tokenLine),
          h('li', {}, 'It reads email headers only, never message bodies, and everything runs on this computer. Nothing goes to whoever made or gave you the app.'),
          h('li', {}, 'A local log of what it unsubscribed from and blocked, so it can catch senders who ignore you.')),
        h('div', { class: 'buttons' },
          h('button', { type: 'button', class: 'btn btn-warn', onclick: forget, disabled: st.demo }, 'forget me: sign out + wipe local data'))));
  }

  function renderScan(error, note) {
    const opts = Object.entries(SCOPES).map(([id, label]) =>
      h('button', { type: 'button', class: 'opt', 'aria-pressed': String(S.scope === id), onclick: () => { S.scope = id; renderScan(); } }, label));
    show(
      h('section', { class: 'panel hero' },
        h('div', { class: 'panel-title' }, h('span', {}, 'scan // config'), h('span', {}, 'ready')),
        banner(),
        h('p', { class: 'prompt' }, `mailsift scan --range ${S.scope}`, h('span', { class: 'cursor', 'aria-hidden': 'true' })),
        h('p', { class: 'lede' }, 'Finds every mailing list in your mailbox and shows who sends what. Nothing is unsubscribed, blocked or moved until you choose.'),
        error ? h('p', { class: 'error', role: 'alert' }, error) : null,
        note ? h('p', { class: 'hint' }, note) : null,
        h('div', { class: 'radio-row', role: 'group', 'aria-label': 'Scan range' }, opts),
        h('button', { type: 'button', class: 'btn btn-go btn-big', onclick: startScan }, '▶ run scan'),
        h('p', { class: 'hint' }, 'read-only · headers only · 4 parallel workers')),
      privacyPanel());
  }

  async function startScan() {
    try { await api('/api/scan', { scope: S.scope }); renderScanning(); } catch (e) {
      if (e.status === 401) return boot();
      if (e.message !== 'locked') renderScan(e.message);
    }
  }

  function renderScanning() {
    const asciiEl = h('pre', { class: 'asciibar', 'aria-hidden': 'true' }, bar(0));
    const m = {};
    const metric = (key, label, cls) => h('div', { class: 'metric' },
      h('div', { class: 'metric-label' }, label), (m[key] = h('div', { class: `metric-value ${cls || ''}` }, '—')));
    const logEl = h('div', { class: 'log', role: 'log', 'aria-label': 'Scan log' });
    const cancel = h('button', { type: 'button', class: 'ghost', onclick: async () => { cancel.disabled = true; cancel.textContent = 'cancelling…'; await api('/api/scan/cancel', {}); } }, 'cancel');
    show(h('div', { class: 'console' },
      panel(['scan // telemetry', cancel], asciiEl,
        h('div', { class: 'metrics' },
          metric('checked', 'emails checked', 'green'), metric('total', 'in range'),
          metric('lists', 'mailing lists', 'amber'), metric('rate', 'emails / sec'),
          metric('elapsed', 'elapsed'), metric('eta', 'eta'))),
      panel('scan // log', logEl)));
    announce('Scan started');

    poll(async () => {
      const job = await api('/api/scan');
      if (job.state === 'error') { renderScan(job.message); return false; }
      if (job.state === 'cancelled') { renderScan(null, 'scan cancelled'); return false; }
      if (job.state === 'done') { announce(`Scan complete: ${fmt(job.senders.length)} mailing lists`); loadResults(job); return false; }
      const elapsed = job.started ? Date.now() / 1000 - job.started : 0;
      const rate = elapsed > 1 ? job.checked / elapsed : 0;
      const p = job.total ? pct(job.checked, job.total) : 0;
      asciiEl.textContent = job.total ? bar(p) : `[${'▒'.repeat(34)}] scanning`;
      m.checked.textContent = fmt(job.checked);
      m.total.textContent = job.total ? fmt(job.total) : '—';
      m.lists.textContent = fmt(job.found);
      m.rate.textContent = rate ? rate.toFixed(0) : '—';
      m.elapsed.textContent = duration(elapsed);
      m.eta.textContent = job.total && rate ? duration((job.total - job.checked) / rate) : '—';
      renderLog(logEl, job.log || []);
      return true;
    }, 600);
  }

  function loadResults(job) {
    S.senders = job.senders || [];
    S.scope = job.scope || S.scope;
    S.limit = PAGE;
    const ids = new Set(S.senders.map((s) => s.id));
    for (const id of Object.keys(S.choices)) if (!ids.has(id)) delete S.choices[id];
    renderReview();
  }

  // ------------------------------------------------------------------- review
  const choiceOf = (s) => S.choices[s.id] || 'keep';
  const isDodgy = (s) => s.kind === 'dodgy' || s.kind === 'unverified';

  const TABS = {
    all: ['all', () => true],
    rare: ['opened <5%', (s) => s.open_pct < 5 && UNSUBSCRIBABLE.has(s.kind)],
    noisy: ['noisy ≥4/mo', (s) => s.per_month >= 4],
    dodgy: ['suspicious', isDodgy],
    ignored: ['ignored you', (s) => s.ignored],
    chosen: ['selected', (s) => choiceOf(s) !== 'keep'],
  };

  function visibleSenders() {
    const q = S.q.trim().toLowerCase();
    const inTab = TABS[S.tab][1];
    return S.senders
      .filter((s) => inTab(s) && (!q || s.name.toLowerCase().includes(q) || s.address.includes(q)))
      .sort(SORTS[S.sort][1]);
  }

  function setChoices(list, action, label) {
    let n = 0;
    for (const s of list) {
      if (s.blocked && action === 'block') continue;
      if (!(ACTIONS_FOR[s.kind] || ['keep']).includes(action)) continue;
      if (action === 'keep') delete S.choices[s.id]; else S.choices[s.id] = action;
      n++;
    }
    announce(`${label}: ${n} sender${n === 1 ? '' : 's'}`);
    renderRows();
  }

  function smartPick() {
    let n = 0;
    for (const s of S.senders) {
      if (s.blocked) continue;
      if (s.kind === 'dodgy' || s.ignored) { S.choices[s.id] = 'block'; n++; } else if (s.open_pct < 5 && UNSUBSCRIBABLE.has(s.kind)) { S.choices[s.id] = 'unsubdel'; n++; }
    }
    announce(`Smart pick selected ${n} senders`);
    renderRows();
  }

  function authChip(label, verdict) {
    const v = verdict || 'none';
    const [cls, mark] = v === 'pass' || v === 'bestguesspass' ? ['chip-green', '✓']
      : v === 'fail' || v === 'permerror' ? ['chip-red', '✗']
        : v === 'softfail' || v === 'temperror' ? ['chip-amber', '~'] : ['chip-dim', '–'];
    return h('span', { class: `chip ${cls}`, title: `${label}: ${v}`, 'aria-label': `${label} ${v}` }, `${label} ${mark}`);
  }

  function senderRow(s) {
    const current = choiceOf(s);
    const [kindText, kindClass, kindTitle] = KIND[s.kind] || KIND.none;
    const fill = h('div', { class: `meter-fill${s.open_pct < 5 ? ' low' : ''}` });
    fill.style.width = `${Math.max(s.open_pct, 2)}%`;
    const stat = (label, value, extra) => h('div', {}, h('div', { class: 'stat-label' }, label), h('div', { class: 'stat-value' }, value), extra);
    return h('div', { class: 'row' },
      h('div', { class: 'who' },
        h('div', { class: 'who-top' },
          h('span', { class: 'who-name' }, s.name),
          h('span', { class: `chip ${kindClass}`, title: kindTitle }, kindText),
          s.ignored ? h('span', { class: 'chip chip-red' }, 'ignored unsub') : null,
          s.blocked ? h('span', { class: 'chip chip-dim' }, 'blocked') : null),
        h('span', { class: 'who-addr' }, s.address),
        s.reasons && s.reasons.length ? h('span', { class: `why${s.kind === 'unverified' ? ' amber' : ''}` }, s.reasons.join(' · ')) : null,
        s.ignored ? h('span', { class: 'why' }, `still sent ${fmt(s.ignored_count)} emails after you unsubscribed: block them`) : null),
      h('div', { class: 'auth', role: 'group', 'aria-label': 'Sender authentication' },
        authChip('spf', s.auth && s.auth.spf), authChip('dkim', s.auth && s.auth.dkim), authChip('dmarc', s.auth && s.auth.dmarc)),
      h('div', { class: 'stats' },
        stat('emails', fmt(s.count)),
        stat('per mo', s.per_month >= 10 ? Math.round(s.per_month) : s.per_month),
        stat('opened', `${s.open_pct}%`, h('div', { class: 'meter' }, fill)),
        stat('last', relDate(s.last))),
      h('div', { class: 'choices', role: 'group', 'aria-label': `Action for ${s.name}` },
        (ACTIONS_FOR[s.kind] || ['keep']).map((action) => h('button', {
          type: 'button', class: `choice c-${action}`, 'aria-pressed': String(current === action), title: ACTION_TITLE[action],
          onclick: () => { if (action === 'keep') delete S.choices[s.id]; else S.choices[s.id] = action; renderRows(); },
        }, ACTION_LABEL[action]))));
  }

  let rowsEl, moreEl, tabsEl;

  function renderReview() {
    const senders = S.senders;
    if (!senders.length) {
      show(h('section', { class: 'panel hero' }, h('div', { class: 'panel-title' }, 'scan // results'),
        h('h1', {}, 'No mailing lists found'),
        h('p', { class: 'lede' }, 'Either your inbox is admirably tidy, or try scanning further back.'),
        h('button', { type: 'button', class: 'btn', onclick: () => renderScan() }, '← back')));
      return;
    }
    const total = senders.reduce((n, s) => n + s.count, 0);
    const never = senders.reduce((n, s) => n + (s.count - s.read), 0);
    const kpi = (label, value, cls) => h('div', { class: 'metric' }, h('div', { class: 'metric-label' }, label), h('div', { class: `metric-value ${cls || ''}` }, value));

    const search = h('input', { type: 'search', id: 'grep', placeholder: 'name or address…', value: S.q, autocomplete: 'off' });
    search.addEventListener('input', () => { S.q = search.value; S.limit = PAGE; renderRows(); });
    const sort = h('select', { id: 'sort' }, Object.entries(SORTS).map(([id, [label]]) => h('option', { value: id, selected: S.sort === id }, label)));
    sort.addEventListener('change', () => { S.sort = sort.value; renderRows(); });

    tabsEl = h('div', { class: 'tabs', role: 'group', 'aria-label': 'Filter senders' });
    rowsEl = h('div', { class: 'rows' });
    moreEl = h('div', { class: 'more' });

    show(h('div', { class: 'review' },
      h('div', { class: 'kpis' },
        kpi('senders', fmt(senders.length), 'green'), kpi('emails', fmt(total)), kpi('never opened', fmt(never)),
        kpi('safe to unsub', fmt(senders.filter((s) => UNSUBSCRIBABLE.has(s.kind)).length), 'green'),
        kpi('suspicious', fmt(senders.filter(isDodgy).length), 'red')),
      panel([`senders // ${SCOPES[S.scope]}`, h('button', { type: 'button', class: 'ghost', onclick: () => renderScan() }, 'new scan')],
        h('div', { class: 'toolbar' },
          h('div', { class: 'grep' }, h('label', { for: 'grep' }, 'grep>'), search),
          h('div', { class: 'sort' }, h('label', { for: 'sort' }, 'sort'), sort)),
        tabsEl,
        h('div', { class: 'bulk' }, h('span', {}, 'bulk:'),
          h('button', { type: 'button', class: 'ghost', title: 'Suspicious and ignored senders → block; opened under 5% → unsub+purge', onclick: smartPick }, 'smart pick'),
          h('button', { type: 'button', class: 'ghost', onclick: () => setChoices(visibleSenders(), 'unsub', 'Unsubscribe visible') }, 'visible → unsub'),
          h('button', { type: 'button', class: 'ghost', onclick: () => setChoices(visibleSenders(), 'unsubdel', 'Unsub+purge visible') }, 'visible → unsub+purge'),
          h('button', { type: 'button', class: 'ghost', onclick: () => setChoices(visibleSenders(), 'keep', 'Reset visible') }, 'visible → keep'),
          h('button', { type: 'button', class: 'ghost', onclick: () => { S.choices = {}; announce('All selections cleared'); renderRows(); } }, 'reset all')),
        rowsEl, moreEl),
      h('p', { class: 'hint' }, 'Suspicious senders can only be blocked: unsubscribing from a scammer just confirms your address is live. Nothing is ever permanently deleted: purged and blocked mail goes to Deleted Items or Junk.')));
    renderRows();
  }

  function renderRows() {
    tabsEl.replaceChildren(...Object.entries(TABS)
      .map(([id, [label, test]]) => [id, label, S.senders.filter(test).length])
      .filter(([id, , n]) => n > 0 || id === 'all' || id === S.tab)
      .map(([id, label, n]) => h('button', {
        type: 'button', class: 'opt', 'aria-pressed': String(S.tab === id),
        onclick: () => { S.tab = id; S.limit = PAGE; renderRows(); },
      }, `${label} (${n})`)));

    const list = visibleSenders();
    rowsEl.replaceChildren(...(list.length ? list.slice(0, S.limit).map(senderRow) : [h('div', { class: 'empty' }, '0 matches. grep harder.')]));
    moreEl.replaceChildren(list.length > S.limit
      ? h('button', { type: 'button', class: 'ghost', onclick: () => { S.limit += PAGE; renderRows(); } }, `show more (${fmt(list.length - S.limit)} hidden)`)
      : h('span', { class: 'hint' }, `${fmt(list.length)} shown`));
    updateBar();
  }

  function tally() {
    const t = { unsub: 0, block: 0, purge: 0, list: [] };
    for (const s of S.senders) {
      const c = choiceOf(s);
      if (c === 'keep') continue;
      t.list.push([s, c]);
      if (c === 'unsub' || c === 'unsubdel') t.unsub++;
      if (c === 'block') t.block++;
      if (c === 'unsubdel' || c === 'block') t.purge += s.count;
    }
    return t;
  }

  function updateBar() {
    const t = tally();
    const n = t.unsub + t.block;
    actionbar.hidden = false;
    const cmd = $('cmd');
    cmd.replaceChildren(n
      ? h('span', {}, '$ mailsift apply --unsub ', h('b', {}, t.unsub), ' --block ', h('b', {}, t.block), ' --purge ', h('b', {}, fmt(t.purge)), ' emails')
      : h('span', {}, '$ mailsift apply  # nothing selected yet'));
    $('apply').disabled = !n;
    $('apply').textContent = n ? `review & apply (${n})` : 'review & apply';
  }

  // ----------------------------------------------------------------- confirm
  function confirmBox({ title, body, ok, onOk }) {
    $('confirm-title').textContent = title;
    $('confirm-content').replaceChildren(...body);
    $('confirm-ok').textContent = ok;
    dialog.returnValue = '';
    dialog.onclose = () => { if (dialog.returnValue === 'ok') onOk(); };
    dialog.showModal();
  }

  $('apply').addEventListener('click', () => {
    const t = tally();
    const st = S.status;
    const simulate = st.dry_run || st.demo;
    const order = { block: 0, unsubdel: 1, unsub: 2 };
    const lines = t.list.sort((a, b) => order[a[1]] - order[b[1]] || a[0].name.localeCompare(b[0].name))
      .map(([s, c]) => h('div', {}, h('b', {}, ACTION_LABEL[c].toUpperCase().padEnd(12)), `${s.name} <${s.address}>`));
    confirmBox({
      title: simulate ? `confirm // ${st.demo ? 'demo' : 'dry'} run` : 'confirm // live run',
      body: [
        h('p', {}, `Unsubscribe from ${t.unsub}, block ${t.block}, and tidy away ${fmt(t.purge)} old emails.`),
        h('div', { class: 'plan' }, lines),
        h('p', { class: 'hint' }, simulate
          ? 'Simulation: nothing in your mailbox will change.'
          : 'Unsubscribes can’t be undone. Old emails go to Deleted Items or Junk, so they can still be recovered.'),
      ],
      ok: simulate ? 'simulate' : 'execute',
      onOk: async () => {
        const choices = {};
        for (const [s, c] of t.list) choices[s.id] = c;
        try { await api('/api/apply', { choices }); renderApplying(); } catch (e) {
          if (e.status === 401) boot(); else if (e.message !== 'locked') renderScan(e.message);
        }
      },
    });
  });

  // -------------------------------------------------------------------- apply
  const STATUS_TAG = { done: '[ OK ]', dry_run: '[ SIM]', needs_you: '[ YOU]', failed: '[FAIL]' };

  function resultLine(r) {
    const cls = r.status === 'failed' ? 'bad' : r.status === 'needs_you' ? 'warn' : 'ok';
    return h('div', { class: cls }, `${STATUS_TAG[r.status] || '[ ?? ]'} ${ACTION_LABEL[r.action] || r.action} ${r.address}: ${r.outcome}`);
  }

  function renderApplying() {
    const asciiEl = h('pre', { class: 'asciibar', 'aria-hidden': 'true' }, bar(0));
    const m = {};
    const metric = (key, label, cls) => h('div', { class: 'metric' }, h('div', { class: 'metric-label' }, label), (m[key] = h('div', { class: `metric-value ${cls || ''}` }, '0')));
    const logEl = h('div', { class: 'log', role: 'log', 'aria-label': 'Clean-up log' });
    show(h('div', { class: 'console' },
      panel('apply // telemetry', asciiEl, h('div', { class: 'metrics' },
        metric('done', 'processed', 'green'), metric('total', 'queued'), metric('you', 'need you', 'amber'), metric('fail', 'failed', 'red'))),
      panel('apply // log', logEl)));
    poll(async () => {
      const job = await api('/api/apply');
      const results = job.results || [];
      asciiEl.textContent = bar(pct(job.done || results.length, job.total || 1));
      m.done.textContent = fmt(job.done || results.length);
      m.total.textContent = fmt(job.total);
      m.you.textContent = fmt(results.filter((r) => r.status === 'needs_you').length);
      m.fail.textContent = fmt(results.filter((r) => r.status === 'failed').length);
      const nearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
      logEl.replaceChildren(...results.map(resultLine));
      if (nearBottom) logEl.scrollTop = logEl.scrollHeight;
      if (job.state === 'error') { renderScan(job.message); return false; }
      if (job.state === 'done') { renderDone(job); return false; }
      return true;
    }, 500);
  }

  function renderDone(job) {
    const results = job.results || [];
    const ok = (r) => ['done', 'dry_run', 'needs_you'].includes(r.status);
    const unsubbed = results.filter((r) => r.action !== 'block' && ok(r)).length;
    const blocked = results.filter((r) => r.action === 'block' && ok(r)).length;
    const needs = results.filter((r) => r.status === 'needs_you');
    const failed = results.filter((r) => r.status === 'failed').length;
    S.choices = {};
    announce('Clean-up finished');
    const kpi = (label, value, cls) => h('div', { class: 'metric' }, h('div', { class: 'metric-label' }, label), h('div', { class: `metric-value ${cls || ''}` }, value));

    show(h('div', { class: 'review' },
      h('section', { class: 'panel hero' },
        h('div', { class: 'panel-title' }, job.dry_run ? 'apply // simulated' : 'apply // complete'),
        h('p', { class: 'prompt' }, 'exit 0'),
        h('h1', {}, job.dry_run ? 'Dry run finished: here’s what would have happened' : 'Done. Your inbox just got lighter.'),
        h('p', { class: 'lede' }, needs.length
          ? `${needs.length} sender(s) need one click from you: open their unsubscribe page below.`
          : 'MailSift remembers who you unsubscribed from. If anyone keeps emailing after a few days, the next scan flags them so you can block them.')),
      h('div', { class: 'kpis' },
        kpi(job.dry_run ? 'would unsub' : 'unsubscribed', fmt(unsubbed), 'green'), kpi(job.dry_run ? 'would block' : 'blocked', fmt(blocked), 'red'),
        kpi('need you', fmt(needs.length), 'amber'), kpi('failed', fmt(failed), failed ? 'red' : ''), kpi('processed', fmt(results.length))),
      needs.length ? panel('action // needs you', h('ul', { class: 'results' }, needs.map((r) => h('li', { class: 'result' },
        h('span', { class: 'result-status needs_you' }, 'NEEDS YOU'),
        h('span', { class: 'result-main' }, h('div', { class: 'result-name' }, r.name), h('div', { class: 'result-outcome' }, r.address)),
        r.link && /^https?:\/\//i.test(r.link) ? h('a', { class: 'open-link', href: r.link, target: '_blank', rel: 'noopener noreferrer' }, 'open unsubscribe page ↗') : null)))) : null,
      panel('apply // log', h('ul', { class: 'results' }, results.map((r) => h('li', { class: 'result' },
        h('span', { class: `result-status ${r.status}` }, (STATUS_TAG[r.status] || r.status).replace(/[[\]]/g, '').trim()),
        h('span', { class: 'result-main' }, h('div', { class: 'result-name' }, `${r.name} · ${ACTION_LABEL[r.action] || r.action}`), h('div', { class: 'result-outcome' }, r.outcome)),
        h('span', {}))))),
      h('div', { class: 'buttons' },
        h('button', { type: 'button', class: 'btn btn-go', onclick: startScan }, '▶ scan again'),
        h('button', { type: 'button', class: 'btn', onclick: () => renderScan() }, 'change range'))));
  }

  boot();
})();
