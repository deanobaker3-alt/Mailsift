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
    score: ['junk score', (a, b) => (b.score || 0) - (a.score || 0) || b.count - a.count],
    count: ['most emails', (a, b) => b.count - a.count],
    rate: ['most per month', (a, b) => b.per_month - a.per_month],
    open: ['least opened', (a, b) => a.open_pct - b.open_pct || b.count - a.count],
    recent: ['most recent', (a, b) => (a.last < b.last ? 1 : a.last > b.last ? -1 : 0)],
    name: ['name a–z', (a, b) => a.name.localeCompare(b.name)],
  };
  const PAGE = 150;

  const S = {
    status: null, scope: '12m', senders: [], choices: {}, tab: 'all', q: '', sort: 'score',
    group: true, open: {},   // company grouping on, and which groups are expanded
    limit: PAGE, timer: null, quitting: false,
    stale: false,   // true once a clean-up has run: the sender list no longer matches the mailbox
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
    main.replaceChildren(...nodes.filter(Boolean));
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

  // `immediate` fetches straight away instead of waiting out the first
  // interval. Rejoining a job in progress needs it: half a second of empty
  // counters before the real ones arrive reads as "that restarted it".
  function poll(fn, ms, onError, immediate = false) {
    stopPolling();
    const tick = async () => {
      let again = false;
      try { again = await fn(); } catch (e) { if (e.message !== 'locked') (onError || renderScan)(e.message); return; }
      if (again) S.timer = setTimeout(tick, ms);
    };
    S.timer = setTimeout(tick, immediate ? 0 : ms);
  }

  const fmt = (n) => Number(n || 0).toLocaleString('en-GB');
  const plural = (n, word) => (Number(n) === 1 ? word : `${word}s`);
  const pct = (n, d) => (d ? Math.min(100, Math.round((100 * n) / d)) : 0);

  // ------------------------------------------------------------ animation
  // The app only reports progress every ~half second. Everything on screen is
  // eased towards those targets every frame, so bars glide, counters roll and
  // clocks tick smoothly instead of jumping.
  const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const ease = (cur, target, dt, speed) => (REDUCED ? target : cur + (target - cur) * (1 - Math.exp(-dt * (speed || 6))));

  function animate(step) {
    let last = performance.now();
    const frame = (now) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      if (step(dt) !== false) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  function clock(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '--:--';
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    const ss = String(s % 60).padStart(2, '0');
    return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  }

  function energyBar(label) {
    const fill = h('div', { class: 'energy-fill' });
    const pctEl = h('span', { class: 'energy-label' }, '0%');
    const el = h('div', { class: 'energy', role: 'progressbar', 'aria-label': label || 'Progress', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': 0 }, fill);
    let lastWhole = -1;
    return {
      wrap: h('div', { class: 'energy-wrap' }, sprite(ENVELOPE, 4, 'sprite fly'), el, pctEl),
      el,
      set(p) {
        const v = Math.max(0, Math.min(100, p));
        fill.style.width = `calc((100% - 6px) * ${v / 100})`;
        const whole = Math.floor(v);
        if (whole !== lastWhole) { lastWhole = whole; pctEl.textContent = `${whole}%`; el.setAttribute('aria-valuenow', whole); }
      },
      indeterminate(on) { el.classList.toggle('indeterminate', on); },
    };
  }

  // Original pixel art: a little envelope with a pink seal.
  const ENVELOPE = [
    '.KKKKKKKKKKKK.',
    'KWWWWWWWWWWWWK',
    'KPWWWWWWWWWWPK',
    'KWPPWWWWWWPPWK',
    'KWWWPPWWPPWWWK',
    'KWWWWWRRWWWWWK',
    'KWWWWWRRWWWWWK',
    'KWWWWWWWWWWWWK',
    '.KKKKKKKKKKKK.',
  ];
  const PALETTE = { K: '#07040F', W: '#F4F1FF', P: '#C9B8FF', R: '#FF66C4', B: '#7A4A2A', S: '#C9B8FF' };
  // Original pixel art: a steaming mug.
  const MUG = [
    '..S..S...',
    '.S..S....',
    'KKKKKKK..',
    'KWWWWWKKK',
    'KWBBBWK.K',
    'KWBBBWK.K',
    'KWWWWWKKK',
    '.KKKKK...',
  ];
  function sprite(rows, scale, cls) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    const w = rows[0].length;
    svg.setAttribute('width', w * scale);
    svg.setAttribute('height', rows.length * scale);
    svg.setAttribute('viewBox', `0 0 ${w} ${rows.length}`);
    svg.setAttribute('class', cls || 'sprite');
    svg.setAttribute('aria-hidden', 'true');
    rows.forEach((row, y) => [...row].forEach((c, x) => {
      if (!PALETTE[c]) return;
      const r = document.createElementNS(ns, 'rect');
      r.setAttribute('x', x); r.setAttribute('y', y); r.setAttribute('width', 1); r.setAttribute('height', 1);
      r.setAttribute('fill', PALETTE[c]);
      svg.append(r);
    }));
    return svg;
  }

  // Arcade-style score counter: rolls up from 0 to the value.
  function countUp(el, target) {
    if (REDUCED || !target) { el.textContent = fmt(target); return; }
    const start = performance.now();
    const dur = Math.min(1200, 500 + target * 2);
    const frame = (now) => {
      if (!el.isConnected && now - start > 50) return;
      const t = Math.min(1, (now - start) / dur);
      el.textContent = fmt(Math.round(target * (1 - Math.pow(1 - t, 3))));
      if (t < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
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
    const shown = el.lastElementChild ? el.lastElementChild.textContent : null;
    const from = shown === null ? 0 : lines.lastIndexOf(shown) + 1;
    if (shown !== null && from === 0) el.replaceChildren();
    for (const line of lines.slice(from)) el.append(h('div', { class: logClass(line) }, line));
    while (el.childElementCount > 120) el.firstElementChild.remove();
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }

  function banner(sub) {
    return h('div', { class: 'title-block', 'aria-hidden': 'true' },
      sprite(ENVELOPE, 6, 'sprite bob'),
      h('div', { class: 'pixel-title' }, 'MAILSIFT'),
      h('div', { class: 'pixel-sub' }, sub || 'SUBSCRIPTION CLEANER'));
  }

  // ------------------------------------------------------------------ chrome
  function coffeeLink(text) {
    const url = S.status && S.status.support_url;
    if (!url || !/^https:\/\//.test(url)) return null;
    return h('a', { class: 'coffee', href: url, target: '_blank', rel: 'noopener noreferrer' }, sprite(MUG, 2, 'sprite'), h('span', {}, text));
  }

  function updateChrome(st) {
    $('version').textContent = `v${st.version}`;
    const coffee = $('coffee');
    if (st.support_url && /^https:\/\//.test(st.support_url)) {
      coffee.href = st.support_url; coffee.hidden = false;
      coffee.replaceChildren(sprite(MUG, 2, 'sprite'), h('span', {}, 'buy me a coffee'));
    } else coffee.hidden = true;
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
      h('div', { class: 'panel-title' }, 'game over'),
      banner('THANKS FOR PLAYING'),
      h('h1', {}, 'MailSift has stopped'),
      h('p', { class: 'lede' }, 'You can close this tab. Launch MailSift again any time to play another round.'),
      h('p', { class: 'hint' }, '> exit 0')));
  }
  // The logo is the home button. boot() re-reads the server's state, so if a
  // scan, clean-up or undo is running it lands back on that, and if there are
  // results it reopens them rather than losing them.
  $('home').addEventListener('click', () => { stopPolling(); goHome(); });
  $('history').addEventListener('click', () => { stopPolling(); renderHistory(); });
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
    if (st.undo_state === 'running') return renderUndoing();
    if (st.scan_state === 'running') return renderScanning(true);
    if (st.has_results) return loadResults(await api('/api/scan'));
    return renderScan();
  }

  // The logo is a main menu, not a jump back into whatever happens to be
  // running: someone clicking it during a scan wants to leave that screen.
  // The scan carries on server-side either way.
  async function goHome() {
    let st;
    try {
      st = S.status = await api('/api/status');
    } catch (e) {
      if (e.message === 'locked') return;
      return show(panel('error // unreachable', h('div', { class: 'panel-body' }, h('p', { class: 'error' }, e.message))));
    }
    updateChrome(st);
    if (!st.configured) return renderSetup();
    if (!st.signed_in) return renderSignIn(st.auth);
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
    const start = async () => {
      try { renderSignIn(await api('/api/login', {})); } catch (e) { renderSignIn({ state: 'error', message: e.message }); }
    };
    const pending = flow.state === 'pending';
    const body = [];
    if (pending) {
      body.push(
        h('p', { class: 'prompt' }, 'mailsift auth --provider microsoft', h('span', { class: 'cursor', 'aria-hidden': 'true' })),
        h('p', { class: 'lede' }, 'A Microsoft sign-in tab has opened. Finish signing in there, then come back here.'),
        h('p', { class: 'hint' }, 'waiting for Microsoft… (no tab? quit MailSift and launch it again)'));
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
          h('button', { type: 'button', class: 'btn btn-go btn-big', onclick: () => start() }, '▶ sign in with microsoft')),
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
          h('li', {}, 'To avoid suggesting anyone you actually correspond with, it checks who your recent sent emails went to. Recipient addresses only, held in memory during the scan and never saved.'),
          h('li', {}, 'A local log of what it unsubscribed from and blocked, so it can catch senders who ignore you.')),
        h('div', { class: 'buttons' },
          h('button', { type: 'button', class: 'btn btn-warn', onclick: forget, disabled: st.demo }, 'forget me: sign out + wipe local data'))));
  }

  function renderScan(error, note) {
    const opts = Object.entries(SCOPES).map(([id, label]) =>
      h('button', { type: 'button', class: 'opt', 'aria-pressed': String(S.scope === id), onclick: () => { S.scope = id; renderScan(); } }, label));
    // Leaving a running scan is fine, it carries on server-side. But offering
    // "start scan" while one is going is a lie, so offer the way back instead.
    const scanning = Boolean(S.status && S.status.scan_state === 'running');
    show(
      h('section', { class: 'panel hero' },
        h('div', { class: 'panel-title' }, h('span', {}, 'scan // config'), h('span', {}, scanning ? 'scanning' : 'ready')),
        banner(),
        h('p', { class: 'prompt' }, `mailsift scan --range ${S.scope}`, h('span', { class: 'cursor', 'aria-hidden': 'true' })),
        h('p', { class: 'lede' }, 'Finds every mailing list in your mailbox and shows who sends what. Nothing is unsubscribed, blocked or moved until you choose.'),
        error ? h('p', { class: 'error', role: 'alert' }, error) : null,
        note ? h('p', { class: 'hint' }, note) : null,
        h('div', { class: 'radio-row', role: 'group', 'aria-label': 'Scan range' }, opts),
        h('p', { class: 'press' }, scanning ? 'SCAN IN PROGRESS' : 'PRESS START'),
        scanning
          ? h('button', { type: 'button', class: 'btn btn-go btn-big', onclick: () => renderScanning(true) },
              '▶ back to the running scan')
          : h('button', { type: 'button', class: 'btn btn-go btn-big', onclick: startScan }, '▶ start scan'),
        // Coming back here from the review screen used to strand you: the
        // results were still held, but nothing offered a way back to them.
        S.senders.length && !S.stale
          ? h('button', { type: 'button', class: 'btn', onclick: () => renderReview() },
              `← back to your ${fmt(S.senders.length)} results`)
          : null,
        S.senders.length && !S.stale
          ? h('p', { class: 'hint' }, 'Your last scan is still here. Starting a new one replaces it.')
          : null,
        S.stale ? h('p', { class: 'hint' }, 'Your last results are out of date now you’ve made changes. Scan again to see where things stand.') : null,
        h('p', { class: 'hint' }, 'read-only · headers only · 4 parallel workers'),
        h('button', { type: 'button', class: 'ghost', onclick: renderDiagnostics,
                      title: 'Tests the real connections MailSift uses on this computer' }, 'check my setup')),
      undoPanel(),
      privacyPanel());
  }

  async function startScan() {
    try { await api('/api/scan', { scope: S.scope }); renderScanning(); } catch (e) {
      if (e.status === 401) return boot();
      if (e.message !== 'locked') renderScan(e.message);
    }
  }

  // `resume` means we're rejoining a scan that's already running, not starting
  // one. Without it the bar and counters climb from zero again, which reads as
  // "clicking that restarted my scan".
  function renderScanning(resume = false) {
    const meter = energyBar('Scan progress');
    const m = {};
    const metric = (key, label, cls) => h('div', { class: 'metric' },
      h('div', { class: 'metric-label' }, label), (m[key] = h('div', { class: `metric-value ${cls || ''}` }, '—')));
    const logEl = h('div', { class: 'log', role: 'log', 'aria-label': 'Scan log' });
    const cancel = h('button', { type: 'button', class: 'ghost', onclick: async () => { cancel.disabled = true; cancel.textContent = 'cancelling…'; await api('/api/scan/cancel', {}); } }, 'cancel');
    show(h('div', { class: 'console' },
      panel(['scan // telemetry', cancel], meter.wrap,
        h('div', { class: 'metrics' },
          metric('checked', 'emails checked', 'green'), metric('total', 'in range'),
          metric('lists', 'mailing lists', 'amber'), metric('rate', 'emails / sec'),
          metric('elapsed', 'elapsed'), metric('eta', 'eta'))),
      panel('scan // log', logEl)));
    announce(resume ? 'Rejoining the scan already running' : 'Scan started');
    meter.indeterminate(true);

    // targets come from the server; "shown" values chase them every frame
    const T = { checked: 0, found: 0, total: null, started: null };
    const V = { checked: 0, found: 0, rate: 0 };
    let ema = null; let eta = null; let last = null;
    let snap = resume;   // first reading jumps to where the scan already is

    animate((dt) => {
      if (!meter.el.isConnected) return false;
      // Rejoining: hold the placeholders rather than painting zeros for the
      // moment it takes the first reading to arrive.
      if (snap) return true;
      V.checked = ease(V.checked, T.checked, dt);
      V.found = ease(V.found, T.found, dt);
      if (ema !== null) V.rate = ease(V.rate, ema, dt, 3);
      m.checked.textContent = fmt(Math.round(V.checked));
      m.lists.textContent = fmt(Math.round(V.found));
      m.rate.textContent = ema !== null ? fmt(Math.round(V.rate)) : '--';
      if (T.total) meter.set((100 * V.checked) / T.total);
      if (T.started) m.elapsed.textContent = clock(Date.now() / 1000 - T.started);
      if (eta !== null) { eta = Math.max(0, eta - dt); m.eta.textContent = clock(eta); }
      return true;
    });

    poll(async () => {
      const job = await api('/api/scan');
      if (job.state === 'error') { renderScan(job.message); return false; }
      if (job.state === 'cancelled') { renderScan(null, 'scan cancelled'); return false; }
      if (job.state === 'done') { announce(`Scan complete: ${fmt(job.senders.length)} mailing lists`); loadResults(job); return false; }
      const now = performance.now() / 1000;
      if (last && job.checked > last.checked) {
        const instant = (job.checked - last.checked) / Math.max(0.05, now - last.t);
        ema = ema === null ? instant : ema * 0.7 + instant * 0.3;   // smoothed speed
      }
      if (!last || job.checked !== last.checked) last = { t: now, checked: job.checked };
      Object.assign(T, { checked: job.checked || 0, found: job.found || 0, total: job.total, started: job.started });
      if (snap) {   // rejoining: show where the scan is, don't animate up to it
        V.checked = T.checked; V.found = T.found;
        if (T.total) meter.set((100 * V.checked) / T.total);
        snap = false;
      }
      if (job.total) {
        meter.indeterminate(false);
        m.total.textContent = fmt(job.total);
        if (ema) {
          const fresh = Math.max(0, (job.total - job.checked) / ema);
          eta = eta === null ? fresh : eta * 0.75 + fresh * 0.25;    // blend, so the countdown never lurches
        }
      }
      renderLog(logEl, job.log || []);
      return true;
    }, 500, null, resume);
  }

  function loadResults(job) {
    S.stale = false;   // fresh from the server, so they're safe to go back to
    S.senders = job.senders || [];
    S.otherSenders = job.other_senders || 0;      // seen but not mailing-list mail
    S.otherMessages = job.other_messages || 0;
    S.sentChecked = job.sent_checked !== false;   // older servers didn't say: assume fine
    S.pickNote = '';
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
    picks: ['smart picks', (s) => s.tier === 'pick'],
    worth: ['worth a look', (s) => s.tier === 'suggest'],
    rare: ['opened <5%', (s) => s.open_pct < 5 && UNSUBSCRIBABLE.has(s.kind)],
    never: ['never opened', (s) => !s.read],
    noisy: ['noisy ≥4/mo', (s) => s.per_month >= 4],
    safe: ['safe to unsub', (s) => UNSUBSCRIBABLE.has(s.kind) && !s.blocked],
    dodgy: ['suspicious', isDodgy],
    ignored: ['ignored you', (s) => s.ignored],
    handled: ['already done', (s) => Boolean(s.blocked || s.unsubscribed_at)],
    chosen: ['selected', (s) => choiceOf(s) !== 'keep'],
    protected: ['never touch', (s) => s.protected],
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
      if (s.protected) continue;
      if (s.blocked && action === 'block') continue;
      if (!(ACTIONS_FOR[s.kind] || ['keep']).includes(action)) continue;
      if (action === 'keep') delete S.choices[s.id]; else S.choices[s.id] = action;
      n++;
    }
    announce(`${label}: ${n} sender${n === 1 ? '' : 's'}`);
    renderRows();
  }

  // Payoff: roughly how many emails a year stop reaching the inbox. Based on
  // each sender's monthly rate over the scan, so it's an estimate, and said so.
  const perYear = (list) => Math.round(list.reduce((n, s) => n + (Number(s.per_month) || 0) * 12, 0));

  // Smart pick follows the server's junk score. It only *selects* senders in
  // the "pick" tier, with the action the score recommended, and only if that
  // action is allowed for the sender's kind (checked again here, and again
  // server-side when applied). "Worth a look" senders are never selected.
  function smartPick() {
    let picked = 0; let worth = 0;
    for (const s of S.senders) {
      if (s.protected || s.blocked || !s.suggest) continue;
      if (s.tier === 'pick' && (ACTIONS_FOR[s.kind] || []).includes(s.suggest)) {
        S.choices[s.id] = s.suggest; picked++;
      } else if (s.tier === 'suggest') worth++;
    }
    S.pickNote = picked
      ? `picked ${picked}${worth ? ` · ${worth} more worth a look` : ''}`
      : worth ? `nothing clear-cut · ${worth} worth a look` : 'nothing stands out';
    announce(`Smart pick selected ${picked} ${plural(picked, 'sender')}${worth ? `, and ${worth} more are worth a look` : ''}`);
    renderRows();
  }

  const scoreClass = (s) => (s.tier === 'pick' ? 'chip-red' : s.tier === 'suggest' ? 'chip-amber' : 'chip-dim');
  const SCORE_TITLE = 'Junk score: 0 means you clearly want these, 100 means you clearly don’t. '
    + 'Smart pick selects 70+; 45–69 is “worth a look”.';

  function authChip(label, verdict) {
    const v = verdict || 'none';
    const [cls, mark] = v === 'pass' || v === 'bestguesspass' ? ['chip-green', '✓']
      : v === 'fail' || v === 'permerror' ? ['chip-red', '✗']
        : v === 'softfail' || v === 'temperror' ? ['chip-amber', '~'] : ['chip-dim', '–'];
    return h('span', { class: `chip ${cls}`, title: `${label}: ${v}`, 'aria-label': `${label} ${v}` }, `${label} ${mark}`);
  }

  function senderRow(s, member) {
    const current = choiceOf(s);
    const [kindText, kindClass, kindTitle] = KIND[s.kind] || KIND.none;
    const fill = h('div', { class: `meter-fill${s.open_pct < 5 ? ' low' : ''}` });
    fill.style.width = `${Math.max(s.open_pct, 2)}%`;
    const stat = (label, value, extra) => h('div', {}, h('div', { class: 'stat-label' }, label), h('div', { class: 'stat-value' }, value), extra);
    const hasScore = typeof s.score === 'number';
    // Spam and ignored-unsubscribe senders explain themselves on their own
    // red lines below; the score's reasons would only repeat them.
    const scoreWhy = hasScore && !isDodgy(s) && !s.ignored && s.score_why && s.score_why.length ? s.score_why : null;
    return h('div', { class: `row${member ? ' row-member' : ''}` },
      h('div', { class: 'who' },
        h('div', { class: 'who-top' },
          h('span', { class: 'who-name' }, s.name),
          hasScore ? h('span', { class: `chip ${scoreClass(s)}`, title: SCORE_TITLE }, `junk ${s.score}`) : null,
          h('span', { class: `chip ${kindClass}`, title: kindTitle }, kindText),
          s.tier === 'suggest' && current === 'keep' && !s.protected
            ? h('span', { class: 'chip chip-amber', title: `Suggested: ${ACTION_LABEL[s.suggest] || s.suggest}. Smart pick won't select it for you.` }, 'worth a look') : null,
          s.emailed ? h('span', { class: 'chip chip-cyan', title: 'Found in your Sent Items, so smart pick will never select it' }, 'you’ve emailed them') : null,
          s.trend === 'faded' ? h('span', { class: 'chip chip-amber', title: 'You used to open these and stopped' }, 'faded') : null,
          s.ignored ? h('span', { class: 'chip chip-red' }, 'ignored unsub') : null,
          s.blocked ? h('span', { class: 'chip chip-dim' }, 'blocked') : null,
          s.protected ? h('span', { class: 'chip chip-cyan' }, 'never touch') : null),
        h('span', { class: 'who-addr' }, s.address),
        scoreWhy ? h('span', { class: 'why-score' }, scoreWhy.join(' · ')) : null,
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
        h('button', {
          type: 'button', class: `choice c-protect`, 'aria-pressed': String(!!s.protected),
          title: s.protected ? 'Remove from your never-touch list' : 'Never touch this sender (bulk actions skip it)',
          'aria-label': s.protected ? `Remove ${s.name} from never-touch list` : `Never touch ${s.name}`,
          onclick: () => toggleProtect(s),
        }, s.protected ? '★' : '☆'),
        s.protected ? h('span', { class: 'hint protected-note' }, 'protected: bulk actions skip it') : (ACTIONS_FOR[s.kind] || ['keep']).map((action) => h('button', {
          type: 'button', class: `choice c-${action}`, 'aria-pressed': String(current === action), title: ACTION_TITLE[action],
          onclick: () => { if (action === 'keep') delete S.choices[s.id]; else S.choices[s.id] = action; renderRows(); },
        }, ACTION_LABEL[action]))));
  }

  // -- company groups ------------------------------------------------------
  // Grouping is only ever a view over the same senders: choices are still
  // stored per sender, and a group action is just setChoices() over its
  // members, so each member's own allowed actions and never-touch status
  // still apply. Nothing about what gets sent to the server changes.
  function toItems(list) {
    if (!S.group) return list.map((s) => ({ s }));
    const order = []; const byKey = new Map();
    for (const s of list) {
      const key = s.company || `addr:${s.address}`;
      if (!byKey.has(key)) { byKey.set(key, []); order.push(key); }
      byKey.get(key).push(s);
    }
    return order.map((key) => {
      const members = byKey.get(key);
      return members.length > 1 ? { key, members } : { s: members[0] };
    });
  }

  function groupRow(key, members) {
    const lead = members.reduce((a, b) => (b.count > a.count ? b : a));
    const count = members.reduce((n, s) => n + s.count, 0);
    const read = members.reduce((n, s) => n + (s.read || 0), 0);
    const perMonth = members.reduce((n, s) => n + (Number(s.per_month) || 0), 0);
    const last = members.map((s) => s.last).sort().pop();
    const top = members.reduce((a, b) => ((b.score || 0) > (a.score || 0) ? b : a));
    const openPct = count ? Math.round((100 * read) / count) : 0;
    const open = Boolean(S.open[key]);
    const fill = h('div', { class: `meter-fill${openPct < 5 ? ' low' : ''}` });
    fill.style.width = `${Math.max(openPct, 2)}%`;
    const stat = (label, value, extra) => h('div', {}, h('div', { class: 'stat-label' }, label), h('div', { class: 'stat-value' }, value), extra);
    const actionable = members.filter((s) => !s.protected);
    const actions = ['keep', 'unsub', 'unsubdel', 'block']
      .filter((a) => actionable.some((s) => (ACTIONS_FOR[s.kind] || ['keep']).includes(a)));
    const allAre = (a) => actionable.length > 0 && actionable.every((s) => choiceOf(s) === a);
    const label = lead.company_label || lead.address.split('@').pop();
    return h('div', { class: 'row row-group' },
      h('div', { class: 'who' },
        h('div', { class: 'who-top' },
          h('button', {
            type: 'button', class: 'expand', 'aria-expanded': String(open),
            'aria-label': `${open ? 'Hide' : 'Show'} the ${members.length} senders from ${label}`,
            onclick: () => { S.open[key] = !open; renderRows(); },
          }, open ? '▾' : '▸'),
          h('span', { class: 'who-name' }, lead.name),
          typeof top.score === 'number' ? h('span', { class: `chip ${scoreClass(top)}`, title: `${SCORE_TITLE} Shows the highest in the group.` }, `junk ${top.score}`) : null,
          h('span', { class: 'chip chip-cyan', title: 'Several addresses from the same company' }, `${members.length} senders`)),
        h('span', { class: 'who-addr' }, label),
        h('span', { class: 'why-score' }, members.map((s) => s.address.split('@')[0]).join(' · '))),
      h('div', {}),
      h('div', { class: 'stats' },
        stat('emails', fmt(count)),
        stat('per mo', perMonth >= 10 ? Math.round(perMonth) : Math.round(perMonth * 10) / 10),
        stat('opened', `${openPct}%`, h('div', { class: 'meter' }, fill)),
        stat('last', relDate(last))),
      h('div', { class: 'choices', role: 'group', 'aria-label': `Action for every sender from ${label}` },
        actions.map((action) => h('button', {
          type: 'button', class: `choice c-${action}`, 'aria-pressed': String(allAre(action)),
          title: `${ACTION_TITLE[action]} (all ${members.length}, where allowed)`,
          onclick: () => setChoices(members, action, `${lead.name}: ${ACTION_LABEL[action]}`),
        }, ACTION_LABEL[action]))));
  }

  async function toggleProtect(s) {
    const on = !s.protected;
    try {
      await api('/api/protect', { id: s.id, on });
      s.protected = on;
      if (on) delete S.choices[s.id];
      announce(on ? `${s.name} added to your never-touch list` : `${s.name} removed from your never-touch list`);
      renderRows();
    } catch (e) { if (e.message !== 'locked') announce(e.message); }
  }

  let rowsEl, moreEl, tabsEl, pickNoteEl;

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
    const kpi = (label, value, cls) => {
      const v = h('div', { class: `metric-value ${cls || ''}` }, '0');
      countUp(v, Number(String(value).replace(/,/g, '')) || 0);
      return h('div', { class: 'metric' }, h('div', { class: 'metric-label' }, label), v);
    };

    const search = h('input', { type: 'search', id: 'grep', placeholder: 'name or address…', value: S.q, autocomplete: 'off' });
    search.addEventListener('input', () => { S.q = search.value; S.limit = PAGE; renderRows(); });
    const sort = h('select', { id: 'sort' }, Object.entries(SORTS).map(([id, [label]]) => h('option', { value: id, selected: S.sort === id }, label)));
    sort.addEventListener('change', () => { S.sort = sort.value; renderRows(); });

    tabsEl = h('div', { class: 'tabs', role: 'group', 'aria-label': 'Filter senders' });
    rowsEl = h('div', { class: 'rows' });
    moreEl = h('div', { class: 'more' });
    pickNoteEl = h('span', { class: 'pick-note', role: 'status' });
    const groupToggle = h('button', {
      type: 'button', class: 'ghost', 'aria-pressed': String(S.group),
      title: 'Show several addresses from the same company as one row',
      onclick: () => {
        S.group = !S.group;
        groupToggle.setAttribute('aria-pressed', String(S.group));
        groupToggle.textContent = `group by company: ${S.group ? 'on' : 'off'}`;
        renderRows();
      },
    }, `group by company: ${S.group ? 'on' : 'off'}`);

    show(h('div', { class: 'review' },
      h('div', { class: 'kpis' },
        kpi('senders', fmt(senders.length), 'green'), kpi('emails', fmt(total)), kpi('never opened', fmt(never)),
        kpi('safe to unsub', fmt(senders.filter((s) => UNSUBSCRIBABLE.has(s.kind)).length), 'green'),
        kpi('suspicious', fmt(senders.filter(isDodgy).length), 'red')),
      panel([`senders // ${SCOPES[S.scope]}`, h('button', { type: 'button', class: 'ghost', onclick: () => renderScan() }, 'new scan')],
        h('div', { class: 'toolbar' },
          h('div', { class: 'grep' }, h('label', { for: 'grep' }, 'grep>'), search),
          h('div', { class: 'sort' }, h('label', { for: 'sort' }, 'sort'), sort),
          groupToggle),
        tabsEl,
        h('div', { class: 'bulk' }, h('span', {}, 'bulk:'),
          h('button', { type: 'button', class: 'ghost', title: 'Selects senders with a junk score of 70 or more, using the action the score suggests. 45–69 are marked “worth a look” but never selected. Anyone you’ve emailed, or on your never-touch list, is always left alone.', onclick: smartPick }, 'smart pick'),
          h('button', { type: 'button', class: 'ghost', onclick: () => setChoices(visibleSenders(), 'unsub', 'Unsubscribe visible') }, 'visible: unsub'),
          h('button', { type: 'button', class: 'ghost', onclick: () => setChoices(visibleSenders(), 'unsubdel', 'Unsub+purge visible') }, 'visible: unsub+purge'),
          h('button', { type: 'button', class: 'ghost', onclick: () => setChoices(visibleSenders(), 'keep', 'Reset visible') }, 'visible: keep'),
          h('button', { type: 'button', class: 'ghost', onclick: () => { S.choices = {}; S.pickNote = ''; announce('All selections cleared'); renderRows(); } }, 'reset all'),
          pickNoteEl),
        rowsEl, moreEl),
      S.sentChecked === false
        ? h('p', { class: 'hint' }, 'Couldn’t read your Sent Items this time, so “you’ve emailed them” can’t be shown and smart pick can’t use it. Everything else works as normal.')
        : null,
      S.otherSenders
        ? h('p', { class: 'hint' },
            `Not everyone is listed: ${fmt(S.otherSenders)} other ${plural(S.otherSenders, 'sender')} `
            + `(${fmt(S.otherMessages)} ${plural(S.otherMessages, 'email')}) weren’t mailing-list mail, so MailSift leaves them alone. `
            + 'That includes personal email and anyone who doesn’t send a proper unsubscribe header. '
            + 'If someone you expected is missing, that’s usually why — or they fall outside your scan range.')
        : null,
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
    const items = toItems(list);
    const rows = [];
    let shownSenders = 0;
    for (const item of items.slice(0, S.limit)) {
      if (item.s) { rows.push(senderRow(item.s)); shownSenders++; continue; }
      rows.push(groupRow(item.key, item.members));
      shownSenders += item.members.length;
      if (S.open[item.key]) rows.push(...item.members.map((s) => senderRow(s, true)));
    }
    if (pickNoteEl) pickNoteEl.textContent = S.pickNote || '';
    rowsEl.replaceChildren(...(list.length
      ? rows
      : [h('div', { class: 'empty' },
          h('div', {}, '0 matches. grep harder.'),
          // Looking for someone specific and not finding them is the moment
          // to explain why they might not be here at all.
          S.q.trim() ? h('p', { class: 'hint' },
            'Looking for a particular sender? MailSift only lists mail that carries a '
            + 'proper unsubscribe header, so personal email and senders who don’t set one '
            + 'never appear. They may also be outside your scan range — try "everything".') : null)]));
    moreEl.replaceChildren(items.length > S.limit
      ? h('button', { type: 'button', class: 'ghost', onclick: () => { S.limit += PAGE; renderRows(); } }, `show more (${fmt(list.length - shownSenders)} hidden)`)
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
    const year = perYear(t.list.map(([s]) => s));
    cmd.replaceChildren(n
      ? h('span', {}, '$ mailsift apply --unsub ', h('b', {}, t.unsub), ' --block ', h('b', {}, t.block), ' --purge ', h('b', {}, fmt(t.purge)), ' emails',
        year ? h('span', { class: 'payoff' }, '  # ≈ ', h('b', {}, fmt(year)), ' fewer a year') : null)
      : h('span', {}, '$ mailsift apply  # nothing selected yet'));
    $('apply').disabled = !n;
    $('apply').textContent = n ? `review & apply (${n})` : 'review & apply';
  }

  // ----------------------------------------------------------------- confirm
  function confirmBox({ title, body, ok, onOk, ack }) {
    $('confirm-title').textContent = title;
    const okBtn = $('confirm-ok');
    const parts = body.filter(Boolean);
    okBtn.disabled = false;
    if (ack) {
      const box = h('input', { type: 'checkbox', id: 'confirm-ack' });
      box.addEventListener('change', () => { okBtn.disabled = !box.checked; });
      parts.push(h('label', { class: 'ack', for: 'confirm-ack' }, box, h('span', {}, ack)));
      okBtn.disabled = true;
    }
    $('confirm-content').replaceChildren(...parts);
    okBtn.textContent = ok;
    dialog.returnValue = '';
    dialog.onclose = () => { if (dialog.returnValue === 'ok') onOk(); };
    dialog.showModal();
  }

  $('apply').addEventListener('click', () => {
    const t = tally();
    const st = S.status;
    const simulate = st.dry_run || st.demo;
    const byName = (a, b) => a[0].name.localeCompare(b[0].name);
    const line = ([s, c]) => h('div', {}, h('b', {}, ACTION_LABEL[c].toUpperCase().padEnd(12)), `${s.name} <${s.address}>`);
    const blocks = t.list.filter(([, c]) => c === 'block').sort(byName);
    const others = t.list.filter(([, c]) => c !== 'block').sort((a, b) => (a[1] === b[1] ? byName(a, b) : a[1] === 'unsubdel' ? -1 : 1));
    const legitBlocks = blocks.filter(([s]) => !isDodgy(s) && !s.ignored); // ignored-your-unsub senders deserve a block
    confirmBox({
      title: simulate ? `confirm // ${st.demo ? 'demo' : 'dry'} run` : 'confirm // live run',
      ack: !st.dry_run && legitBlocks.length
        ? `Yes, block ${legitBlocks.length === 1 ? 'that sender' : `those ${legitBlocks.length} senders`} too. Their future emails will go straight to Junk.`
        : null,
      body: [
        h('p', {}, `Unsubscribe from ${t.unsub}, block ${t.block}, and tidy away ${fmt(t.purge)} old emails.`
          + (perYear(t.list.map(([s]) => s)) ? ` Going by how often they’ve been sending, that’s roughly ${fmt(perYear(t.list.map(([s]) => s)))} fewer emails a year.` : '')),
        blocks.length ? h('div', { class: 'block-section' },
          h('div', { class: 'block-head' }, `⛔ blocking ${blocks.length} sender${blocks.length === 1 ? '' : 's'}: every future email from them goes straight to Junk`),
          h('div', { class: 'plan' }, blocks.map(line))) : null,
        legitBlocks.length ? h('p', { class: 'warn-box' },
          `Heads up: ${legitBlocks.map(([s]) => s.name).join(', ')} ${legitBlocks.length === 1 ? 'looks' : 'look'} like ${legitBlocks.length === 1 ? 'a genuine sender' : 'genuine senders'}, not ${legitBlocks.length === 1 ? 'a scammer' : 'scammers'}. If you just want fewer emails, “unsub” is usually better than “block”.`) : null,
        others.length ? h('div', { class: 'plan' }, others.map(line)) : null,
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
  const STATUS_TAG = { done: '[ OK ]', dry_run: '[ SIM]', needs_you: '[ YOU]', failed: '[FAIL]', pending: '[ .. ]' };

  function resultLine(r) {
    const cls = r.status === 'failed' ? 'bad' : r.status === 'needs_you' ? 'warn' : 'ok';
    return h('div', { class: cls }, `${STATUS_TAG[r.status] || '[ ?? ]'} ${ACTION_LABEL[r.action] || r.action} ${r.address}: ${r.outcome}`);
  }

  function renderApplying() {
    const meter = energyBar('Clean-up progress');
    const m = {};
    const metric = (key, label, cls) => h('div', { class: 'metric' }, h('div', { class: 'metric-label' }, label), (m[key] = h('div', { class: `metric-value ${cls || ''}` }, '0')));
    const logEl = h('div', { class: 'log', role: 'log', 'aria-label': 'Clean-up log' });
    show(h('div', { class: 'console' },
      panel('apply // telemetry', meter.wrap, h('div', { class: 'metrics' },
        metric('done', 'processed', 'green'), metric('total', 'queued'), metric('you', 'need you', 'amber'), metric('fail', 'failed', 'red'))),
      panel('apply // log', logEl)));
    const T = { done: 0, total: 1 };
    let shown = 0;
    animate((dt) => {
      if (!meter.el.isConnected) return false;
      shown = ease(shown, T.done, dt, 8);
      meter.set((100 * shown) / (T.total || 1));
      m.done.textContent = fmt(Math.round(shown));
      return true;
    });
    poll(async () => {
      const job = await api('/api/apply');
      const results = job.results || [];
      T.done = job.done || results.length; T.total = job.total || 1;
      m.total.textContent = fmt(job.total);
      m.you.textContent = fmt(results.filter((r) => r.status === 'needs_you').length);
      m.fail.textContent = fmt(results.filter((r) => r.status === 'failed').length);
      const nearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
      results.forEach((r, i) => {   // update changed lines, append new ones (so each animates in once)
        const fresh = resultLine(r);
        const old = logEl.children[i];
        if (!old) logEl.append(fresh);
        else if (old.textContent !== fresh.textContent) { old.textContent = fresh.textContent; old.className = fresh.className; }
      });
      if (nearBottom) logEl.scrollTop = logEl.scrollHeight;
      if (job.state === 'error') { renderScan(job.message); return false; }
      if (job.state === 'done') { renderDone(job); return false; }
      return true;
    }, 500);
  }

  function renderDone(job) {
    // Mail has moved and senders have been blocked, so the sender list in
    // memory no longer reflects the mailbox: don't offer to go back to it.
    if (!job.dry_run) S.stale = true;
    const results = job.results || [];
    const ok = (r) => ['done', 'dry_run', 'needs_you'].includes(r.status);
    const unsubbed = results.filter((r) => r.action !== 'block' && ok(r)).length;
    const blocked = results.filter((r) => r.action === 'block' && ok(r)).length;
    const needs = results.filter((r) => r.status === 'needs_you');
    const undoButton = h('button', { type: 'button', class: 'btn', hidden: true }, '↶ undo this clean-up');
    const failed = results.filter((r) => r.status === 'failed').length;
    // Payoff from what actually went through, not from what was selected.
    const byId = new Map(S.senders.map((s) => [s.id, s]));
    const payoff = perYear(results
      .filter((r) => r.status === 'done' || r.status === 'dry_run')
      .map((r) => byId.get(r.id)).filter(Boolean));
    S.choices = {};
    announce('Clean-up finished');
    const kpi = (label, value, cls) => {
      const v = h('div', { class: `metric-value ${cls || ''}` }, '0');
      countUp(v, Number(String(value).replace(/,/g, '')) || 0);
      return h('div', { class: 'metric' }, h('div', { class: 'metric-label' }, label), v);
    };

    show(h('div', { class: 'review' },
      h('section', { class: 'panel hero' },
        h('div', { class: 'panel-title' }, job.dry_run ? 'apply // simulated' : 'apply // complete'),
        h('p', { class: 'stage-clear' }, job.dry_run ? 'PRACTICE ROUND!' : 'STAGE CLEAR!'),
        h('h1', {}, job.dry_run ? 'Dry run finished: here’s what would have happened' : 'Done. Your inbox just got lighter.'),
        payoff ? h('p', { class: 'payoff-big' },
          job.dry_run ? 'That would be roughly ' : 'That’s roughly ', h('b', {}, fmt(payoff)), ' fewer emails a year.') : null,
        h('p', { class: 'lede' }, needs.length
          ? `${needs.length} ${plural(needs.length, 'sender')} ${needs.length === 1 ? 'needs' : 'need'} one click from you: open their unsubscribe page below.`
          : 'MailSift remembers who you unsubscribed from. If anyone keeps emailing after a few days, the next scan flags them so you can block them.'),
        job.dry_run ? null : coffeeLink('Saved you some hassle? Buy me a coffee')),
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
        h('button', { type: 'button', class: 'btn', onclick: () => renderScan() }, 'change range'),
        undoButton)));
    if (!job.dry_run) {
      api('/api/status').then((st) => {
        S.status = st;
        if (st.undo) { undoButton.hidden = false; undoButton.onclick = () => confirmUndo(st.undo); }
      }).catch(() => {});
    }
  }

  // ------------------------------------------------------------------ history
  const when = (iso) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime())
      ? String(iso || '')
      : d.toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
  };

  function logRow(status, title, detail) {
    return h('li', { class: 'result' },
      h('span', { class: `result-status ${status}` }, (STATUS_TAG[status] || status).replace(/[[\]]/g, '').trim()),
      h('span', { class: 'result-main' },
        h('div', { class: 'result-name' }, title),
        h('div', { class: 'result-outcome' }, detail)),
      h('span', {}));
  }

  async function renderHistory() {
    let data;
    try {
      data = await api('/api/history');
    } catch (e) {
      if (e.message === 'locked') return;
      return show(panel('history // unavailable', h('div', { class: 'panel-body' }, h('p', { class: 'error' }, e.message))));
    }
    const scans = data.scans || [];
    const items = data.items || [];
    const back = h('button', { type: 'button', class: 'ghost', onclick: () => boot() }, '← back');

    show(h('div', { class: 'review' },
      panel(['history // scans', back],
        scans.length
          ? h('ul', { class: 'results' }, scans.map((s) => logRow(
              s.dry_run ? 'dry_run' : 'done',
              `${when(s.at)} · ${SCOPES[s.scope] || s.scope}`,
              `${fmt(s.checked)} ${plural(s.checked, 'email')} checked · `
              + `${fmt(s.found)} mailing ${plural(s.found, 'list')} found`
              + (s.other_senders
                ? ` · ${fmt(s.other_senders)} other ${plural(s.other_senders, 'sender')} left alone`
                : ''))))
          : h('div', { class: 'empty' }, 'No scans yet. Run one and it’ll show up here.')),
      panel('history // what mailsift did',
        items.length
          ? h('ul', { class: 'results' }, items.map((r) => logRow(
              r.status,
              `${r.name || r.address} · ${ACTION_LABEL[r.action] || r.action}${r.dry_run ? ' (dry run)' : ''}`,
              `${when(r.at)}${r.detail ? ' — ' + r.detail : ''}`)))
          : h('div', { class: 'empty' }, 'Nothing unsubscribed or blocked yet.')),
      h('p', { class: 'hint' },
        'This log lives on this computer only and is never sent anywhere. '
        + '“Forget me” on the start screen wipes it. Scan records keep counts only — no addresses.')));
  }

  // -------------------------------------------------------------- diagnostics
  // [css class, label] per check status
  const DIAG_TAG = { ok: ['done', 'OK'], info: ['needs_you', 'NOTE'], fail: ['failed', 'FAIL'] };

  async function renderDiagnostics() {
    const back = h('button', { type: 'button', class: 'ghost', onclick: () => goHome() }, '← back');
    show(panel(['diagnostics // running', back],
      h('div', { class: 'panel-body' },
        h('p', { class: 'prompt' }, 'mailsift --self-check', h('span', { class: 'cursor', 'aria-hidden': 'true' })),
        h('p', { class: 'hint' }, 'Testing the real connections MailSift uses. Takes a few seconds.'))));
    announce('Running diagnostics');

    let report;
    try {
      report = await api('/api/diagnostics', {});
    } catch (e) {
      if (e.message === 'locked') return;
      return show(panel(['diagnostics // couldn’t run', back],
        h('div', { class: 'panel-body' }, h('p', { class: 'error' }, e.message))));
    }
    const checks = report.checks || [];
    const summary = report.problems ? `${report.problems} ${plural(report.problems, 'problem')} found` : 'all good';
    announce(`Diagnostics finished: ${summary}`);

    // Plain text a tester can paste to whoever gave them the app. No email
    // address in it: none of the check details include one.
    const plain = [`MailSift ${report.version} self-check: ${summary}`, '']
      .concat(checks.map((c) => `[${(DIAG_TAG[c.status] || [null, c.status])[1]}] ${c.name}: ${c.detail}`))
      .join('\n');
    const copy = h('button', {
      type: 'button', class: 'ghost',
      onclick: async () => {
        try { await navigator.clipboard.writeText(plain); copy.textContent = 'copied ✓'; } catch (e) { copy.textContent = 'copy failed'; }
      },
    }, 'copy report');

    show(h('div', { class: 'review' },
      panel([`diagnostics // ${summary}`, back],
        h('ul', { class: 'results' }, checks.map((c) => {
          const [cls, tag] = DIAG_TAG[c.status] || ['needs_you', c.status];
          return h('li', { class: 'result' },
            h('span', { class: `result-status ${cls}` }, tag),
            h('span', { class: 'result-main' },
              h('div', { class: 'result-name' }, c.name),
              h('div', { class: 'result-outcome' }, c.detail)),
            h('span', {}));
        }))),
      h('div', { class: 'buttons' },
        h('button', { type: 'button', class: 'btn btn-go', onclick: renderDiagnostics }, '▶ run again'),
        copy),
      h('p', { class: 'hint' },
        'Only Microsoft’s servers are contacted. Nothing is sent, nothing in your mailbox is changed, '
        + 'and the copied report leaves out your email address.')));
  }

  // --------------------------------------------------------------------- undo
  function undoPanel() {
    const u = S.status && S.status.undo;
    if (!u) return null;
    const when = new Date(u.at).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
    return h('section', { class: 'panel privacy' },
      h('div', { class: 'panel-title' }, h('span', {}, 'undo // last clean-up'), h('span', {}, when)),
      h('div', { class: 'panel-body' },
        h('p', { class: 'lede flush' }, `Moved ${fmt(u.moved)} emails and blocked ${fmt(u.blocked)} sender${u.blocked === 1 ? '' : 's'}. You can put it all back.`),
        h('div', { class: 'buttons' }, h('button', { type: 'button', class: 'btn', onclick: () => confirmUndo(u) }, '↶ undo last clean-up'))));
  }

  function confirmUndo(u) {
    confirmBox({
      title: 'confirm // undo',
      body: [
        h('p', {}, `Move ${fmt(u.moved)} emails back to where they were, and take ${fmt(u.blocked)} sender${u.blocked === 1 ? '' : 's'} off the MailSift blocklist.`),
        h('p', { class: 'hint' }, 'Unsubscribes can’t be undone: those requests have already gone to the senders. Re-subscribe on their website if you need to.'),
      ],
      ok: 'undo it',
      onOk: async () => {
        try { await api('/api/undo', { run_id: u.run_id }); renderUndoing(); } catch (e) { if (e.message !== 'locked') renderScan(e.message); }
      },
    });
  }

  function renderUndoing() {
    const meter = energyBar('Undo progress');
    const T = { done: 0, total: 1 };
    let shown = 0;
    show(h('section', { class: 'panel hero' },
      h('div', { class: 'panel-title' }, 'undo // rewinding'),
      h('p', { class: 'prompt' }, 'mailsift undo --last', h('span', { class: 'cursor', 'aria-hidden': 'true' })),
      meter.wrap,
      h('p', { class: 'hint' }, 'putting your emails back…')));
    animate((dt) => {
      if (!meter.el.isConnected) return false;
      shown = ease(shown, T.done, dt, 8);
      meter.set((100 * shown) / (T.total || 1));
      return true;
    });
    poll(async () => {
      const job = await api('/api/undo');
      if (job.state === 'error') { renderScan(job.message); return false; }
      if (job.state === 'done') {
        S.senders = []; S.choices = {};
        S.status = await api('/api/status');
        const bits = [`${fmt(job.restored)} emails restored`, `${fmt(job.unblocked)} unblocked`];
        if (job.failed) bits.push(`${fmt(job.failed)} couldn't be moved (they may have been deleted since)`);
        if (job.block_error) bits.push(`blocklist not updated: ${job.block_error}`);
        renderScan(job.failed || job.block_error ? bits.join(' · ') : null, job.failed || job.block_error ? null : `undo complete: ${bits.join(' · ')}. Scan again to see the up-to-date list.`);
        return false;
      }
      T.done = job.done || 0; T.total = job.total || 1;
      return true;
    }, 400);
  }

  boot();
})();
