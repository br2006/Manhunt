(() => {
  const $ = (id) => document.getElementById(id);
  const esc = GameMap.escapeHtml;
  // In sim mode each tab is its own player, so keep the session per tab.
  const store = Geo.sim ? sessionStorage : localStorage;
  const COLOURS = { hider: '#30a46c', seeker: '#e5484d' };

  const S = {
    session: loadSession(),
    ws: null,
    retry: 0,
    offset: 0,          // server_time - client_time (seconds)
    offsetFromPong: false,
    state: null,
    hiderMap: null, hiderMapAt: null,
    seekerMap: null, seekerMapAt: null,
    compass: null,
    seekerDistance: null,
    lastSent: 0,
    gpsOk: false, gpsWeak: false, gpsError: null,
    modalKind: null,
    arrowAngle: 0,
    settingsBuilt: false,
  };

  // ---------- utils ----------
  function loadSession() { try { return JSON.parse(store.getItem('manhunt_session')); } catch { return null; } }
  function saveSession(s) {
    S.session = s;
    if (s) store.setItem('manhunt_session', JSON.stringify(s)); else store.removeItem('manhunt_session');
  }
  const serverNow = () => Date.now() / 1000 + S.offset;
  const vibrate = (p) => { try { navigator.vibrate && navigator.vibrate(p); } catch {} };

  function fmtClock(sec) {
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    const mm = String(m).padStart(2, '0'), ss = String(s).padStart(2, '0');
    return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  }
  function fmtDist(m) {
    if (m == null) return '—';
    if (m >= 1000) return (m / 1000).toFixed(m >= 10000 ? 0 : 1) + ' km';
    return (m < 200 ? Math.round(m / 5) * 5 : Math.round(m / 10) * 10) + ' m';
  }
  function fmtAgo(sec) {
    if (sec == null) return '';
    sec = Math.max(0, Math.round(sec));
    if (sec < 10) return 'just now';
    if (sec < 90) return `${sec}s ago`;
    return `${Math.round(sec / 60)} min ago`;
  }

  function toast(text, kind = '', ms = 4500) {
    const el = document.createElement('div');
    el.className = 'toast ' + kind;
    el.textContent = text;
    $('toasts').appendChild(el);
    setTimeout(() => el.remove(), ms);
  }
  function banner(text) {
    $('banner').textContent = text || '';
    $('banner').classList.toggle('hidden', !text);
  }

  function show(name) {
    for (const s of ['home', 'room', 'game', 'results']) {
      $('screen-' + s).classList.toggle('hidden', s !== name);
    }
    // One Leaflet map, physically moved between the game screen and the results screen.
    if (name === 'game' && $('map').parentElement !== $('screen-game')) {
      $('screen-game').insertBefore($('map'), $('screen-game').querySelector('.game-footer'));
    }
    if (name === 'results' && $('map').parentElement !== $('end-map-slot')) {
      $('end-map-slot').appendChild($('map'));
    }
    if (name === 'game' || name === 'results') {
      GameMap.init($('map'));
      if (Geo.last) GameMap.setMe(Geo.last.lat, Geo.last.lon, Geo.last.acc);
      GameMap.resize();
    }
    if (name === 'home') refreshLobbies();
  }

  // ---------- REST ----------
  async function api(path, body) {
    const r = await fetch(path, body ? {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    } : undefined);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed');
    return data;
  }

  async function refreshLobbies() {
    if (S.session) return;
    try {
      const lobbies = await api('/api/lobbies');
      const ul = $('lobby-list');
      ul.innerHTML = lobbies.map((l) => `
        <li class="lobby-item">
          <div>
            <strong>${esc(l.name)}</strong>
            <div class="muted small-text">Host: ${esc(l.host || '?')} · ${l.players} player${l.players === 1 ? '' : 's'} (min ${l.min_players})</div>
          </div>
          <button class="primary small" data-join="${esc(l.id)}">Join</button>
        </li>`).join('');
      $('lobby-empty').classList.toggle('hidden', lobbies.length > 0);
    } catch { /* offline; try again later */ }
  }

  function nickname() {
    const n = $('nickname').value.trim();
    if (!n) { toast('Enter a nickname first', 'error'); $('nickname').focus(); return null; }
    localStorage.setItem('manhunt_nick', n);
    return n;
  }

  async function createLobby() {
    const nick = nickname(); if (!nick) return;
    try {
      const s = await api('/api/lobbies', { lobby_name: $('lobby-name').value, nickname: nick });
      enterSession(s);
    } catch (e) { toast(e.message, 'error'); }
  }

  async function joinLobby(id) {
    const nick = nickname(); if (!nick) return;
    try {
      const s = await api(`/api/lobbies/${encodeURIComponent(id)}/join`, { nickname: nick });
      enterSession(s);
    } catch (e) { toast(e.message, 'error'); refreshLobbies(); }
  }

  function enterSession(s) {
    saveSession(s);
    // Called from a tap, so this is a good moment to ask for permissions.
    requestPermissions();
    connect();
  }

  async function requestPermissions() {
    Geo.start();
    await Geo.startCompass();
    await Geo.keepAwake();
    if (S.state) render();
  }

  function leave() {
    if (!confirm('Leave this game?')) return;
    send({ t: 'leave' });
    resetToHome();
  }

  function resetToHome() {
    saveSession(null);
    const ws = S.ws; S.ws = null;
    if (ws) try { ws.close(); } catch {}
    Object.assign(S, { state: null, hiderMap: null, seekerMap: null, allMap: null, compass: null, seekerDistance: null, settingsBuilt: false, matesKey: null });
    GameMap.clearMates();
    closeModal();
    banner(null);
    show('home');
  }

  // ---------- websocket ----------
  function connect() {
    if (!S.session) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(S.session.token)}`);
    S.ws = ws;
    ws.onopen = () => { S.retry = 0; banner(null); ping(); sendPos(true); };
    ws.onmessage = (e) => { try { onMessage(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    ws.onclose = (ev) => {
      if (S.ws !== ws) return;
      S.ws = null;
      if (!S.session) return;
      if (ev.code === 4001) return;              // invalid session: handled by message
      if (ev.code === 4002) { banner('This game was opened in another tab or device.'); return; }
      banner('Connection lost: reconnecting…');
      setTimeout(connect, Math.min(10000, 1000 * 2 ** S.retry++));
    };
  }
  function send(msg) {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) { S.ws.send(JSON.stringify(msg)); return true; }
    return false;
  }
  function ping() { send({ t: 'ping', client_time: Date.now() / 1000 }); }

  function sendPos(force = false) {
    const p = Geo.last;
    if (!p) return;
    if (!force && Date.now() - S.lastSent < 5000) return;
    if (send({ t: 'pos', lat: p.lat, lon: p.lon, acc: p.acc })) S.lastSent = Date.now();
  }

  function onMessage(m) {
    switch (m.t) {
      case 'state': applyState(m); break;
      case 'hider_map': S.hiderMap = m.players; S.hiderMapAt = m.at; renderGame(); break;
      case 'seeker_map': S.seekerMap = m.players; S.seekerMapAt = m.at; renderGame(); break;
      case 'all_map': S.allMap = m.players; S.allMapAt = m.at; renderEndMap(); break;
      case 'compass': S.compass = m; renderGame(); break;
      case 'seeker_distance':
        if (S.seekerDistance && m.available && m.distance_m < 150 && S.seekerDistance.distance_m >= 150) vibrate([200, 100, 200]);
        S.seekerDistance = m; renderGame(); break;
      case 'phase':
        if (m.phase === 'active') {
          vibrate([300, 100, 300]);
          toast(S.state && S.state.you.team === 'seeker' ? 'Head start over: start hunting!' : 'Head start over: seekers are hunting!');
        }
        break;
      case 'catch_candidates': showCandidates(m.hiders); break;
      case 'catch_prompt': showPrompt(m); break;
      case 'catch_waiting': showWaiting(m); break;
      case 'catch_result':
        if (S.modalKind === 'waiting') closeModal();
        toast(m.ok ? `You caught ${m.hider_name}!` : m.reason, m.ok ? 'good' : 'error');
        break;
      case 'catch_cancelled': if (S.modalKind === 'prompt') closeModal(); break;
      case 'player_caught':
        if (S.state && m.hider === S.state.you.name) { toast("You've been caught. You're a seeker now!", 'error'); vibrate(500); }
        else toast(`${m.hider} was caught by ${m.seeker}`);
        break;
      case 'chat': showIncomingChat(m); break;
      case 'chat_sent': toast(`Sent to ${m.to_name}: ${m.text}`, 'good', 2500); break;
      case 'game_over': vibrate([200, 100, 200, 100, 400]); break;
      case 'survival_reached':
        vibrate([300, 100, 300]);
        toast(`Survival time reached: ${m.hiders_left} hiders left. The last one standing wins; at the hard limit, the hiders still free win.`, '', 8000);
        break;
      case 'pos_ack': S.gpsWeak = !m.accepted; renderGpsWarn(); break;
      case 'pong': {
        const nowC = Date.now() / 1000;
        const rtt = nowC - m.client_time;
        S.offset = m.server_time - (m.client_time + rtt / 2);
        S.offsetFromPong = true;
        break;
      }
      case 'error': toast(m.msg, 'error'); break;
      case 'invalid_session': resetToHome(); toast('That game no longer exists'); break;
      case 'kicked': resetToHome(); toast(m.msg, 'error'); break;
    }
  }

  function applyState(m) {
    if (!S.offsetFromPong) S.offset = m.server_time - Date.now() / 1000;
    const prevTeam = S.state && S.state.you.team;
    S.state = m;
    if ('hider_map' in m) { S.hiderMap = m.hider_map; S.hiderMapAt = m.hider_map_at; }
    if ('seeker_map' in m) { S.seekerMap = m.seeker_map; S.seekerMapAt = m.seeker_map_at; }
    if ('compass' in m) S.compass = m.compass;
    if ('seeker_distance' in m) S.seekerDistance = m.seeker_distance;
    if ('all_map' in m) { S.allMap = m.all_map; S.allMapAt = m.all_map_at; }
    if (prevTeam && prevTeam !== m.you.team) {
      S.hiderMap = null; S.seekerDistance = null; S.matesKey = null;
      GameMap.clearMates();
      GameMap.resize();
    }
    if (m.catch_prompt && S.modalKind !== 'prompt') showPrompt(m.catch_prompt);
    if (!m.catch_prompt && S.modalKind === 'prompt') closeModal();
    if (m.lobby.phase !== 'active' && S.modalKind === 'candidates') closeModal();
    if (m.lobby.phase === 'lobby' && S.modalKind === 'chat') closeModal();
    render();
  }

  // ---------- rendering ----------
  function render() {
    if (!S.state) { show('home'); return; }
    const phase = S.state.lobby.phase;
    if (phase === 'lobby') { renderRoom(); show('room'); }
    else if (phase === 'ended') { show('results'); renderResults(); }
    else { show('game'); renderGame(); }
  }

  const SETTINGS_UI = [
    ['min_players', 'Minimum players', '', 1],
    ['head_start_s', 'Hider head start', 'min', 60],
    ['survival_time_s', 'Survival time (after this, last hider left wins)', 'min', 60],
    ['time_limit_s', 'Hard time limit (2+ hiders left = hiders win)', 'min', 60],
    ['hider_map_interval_s', 'Hider map refresh', 's', 1],
    ['hider_dist_interval_s', 'Hider "nearest seeker" refresh', 's', 1],
    ['compass_interval_s', 'Seeker compass refresh', 's', 1],
    ['seeker_map_interval_s', 'Seeker map refresh', 's', 1],
    ['catch_radius_m', 'Catch radius', 'm', 1],
    ['catch_confirm_timeout_s', 'Catch confirm timeout', 's', 1],
  ];

  function buildSettings() {
    $('settings').innerHTML = SETTINGS_UI.map(([k, label, unit]) => `
      <label class="setting">
        <span>${label}</span>
        <span class="setting-input"><input type="number" inputmode="numeric" min="0" step="1" data-setting="${k}"><em>${unit}</em></span>
      </label>`).join('');
    S.settingsBuilt = true;
  }

  function renderRoom() {
    const st = S.state, isHost = st.you.is_host;
    $('room-name').textContent = st.lobby.name;
    $('host-hint').classList.toggle('hidden', !isHost);

    // permissions
    const needCompass = Geo.needsCompassPermission() && !Geo.compassActive();
    $('perm-card').classList.toggle('hidden', S.gpsOk && !needCompass && !S.gpsError);
    $('perm-text').textContent = S.gpsError ||
      'Enable location (and compass) so the game can work. Keep this page open and the screen on during the game.';
    $('gps-status').textContent = S.gpsOk
      ? `GPS ✓ (±${Math.round(Geo.last.acc)} m)${Geo.sim ? ' · SIM MODE' : ''}${Geo.hasWakeLock() ? ' · screen will stay on' : ''}`
      : 'Waiting for GPS…';

    // roster
    const groups = [[null, 'Unassigned'], ['hider', 'Hiders'], ['seeker', 'Seekers']];
    $('roster').innerHTML = groups.map(([team, title]) => {
      const ps = st.roster.filter((p) => p.team === team);
      if (team === null && ps.length === 0) return '';
      return `<h3 class="team-title ${team || ''}">${title} (${ps.length})</h3>
        <ul class="list">${ps.map((p) => playerRow(p, st, isHost)).join('') || '<li class="muted small-text">nobody yet</li>'}</ul>`;
    }).join('');

    // settings
    if (!S.settingsBuilt) buildSettings();
    for (const input of $('settings').querySelectorAll('input')) {
      const [, , , factor] = SETTINGS_UI.find((s) => s[0] === input.dataset.setting);
      if (document.activeElement !== input) input.value = Math.round(st.lobby.settings[input.dataset.setting] / factor);
      input.disabled = !isHost;
    }

    // start
    const problem = st.lobby.start_problem;
    $('btn-start').classList.toggle('hidden', !isHost);
    $('btn-start').disabled = !!problem;
    $('wait-host').classList.toggle('hidden', isHost || !!problem);
    $('start-problem').textContent = problem || (isHost ? 'Ready to start!' : '');
  }

  function playerRow(p, st, isHost) {
    const you = p.id === st.you.id;
    const controls = isHost ? `
      <span class="row-controls">
        <button class="chip ${p.team === 'hider' ? 'on hider' : ''}" data-team="hider" data-pid="${p.id}">Hider</button>
        <button class="chip ${p.team === 'seeker' ? 'on seeker' : ''}" data-team="seeker" data-pid="${p.id}">Seeker</button>
        ${you ? '' : `<button class="chip kick" data-kick="${p.id}" title="Remove">✕</button>`}
      </span>` : '';
    return `<li class="player-row">
      <span class="dot ${p.connected ? 'on' : ''}" title="${p.connected ? 'online' : 'offline'}"></span>
      <span class="pname">${esc(p.name)}${you ? ' <em>(you)</em>' : ''}${p.is_host ? ' 👑' : ''}</span>
      <span class="gps ${p.has_fix ? 'ok' : ''}">${p.has_fix ? 'GPS ✓' : 'no GPS'}</span>
      ${controls}
    </li>`;
  }

  function renderGpsWarn() {
    $('gps-warn').classList.toggle('hidden', !S.gpsWeak);
  }

  function renderGame() {
    const st = S.state;
    if (!st || !['head_start', 'active'].includes(st.lobby.phase)) return;
    const team = st.you.team, phase = st.lobby.phase, now = serverNow();
    $('team-badge').textContent = team === 'seeker' ? 'SEEKER' : 'HIDER';
    $('team-badge').className = 'badge ' + team;
    const hidersLeft = st.roster.filter((p) => p.team === 'hider').length;
    $('hiders-left').textContent = `${hidersLeft} hider${hidersLeft === 1 ? '' : 's'} left`;
    $('hider-hud').classList.toggle('hidden', team !== 'hider');
    $('seeker-hud').classList.toggle('hidden', team !== 'seeker');
    $('screen-game').classList.toggle('is-seeker', team === 'seeker');

    if (team === 'hider') {
      const d = S.seekerDistance;
      const el = $('seeker-dist');
      el.textContent = d && d.available ? fmtDist(d.distance_m) : '—';
      el.className = 'bignum ' + (!d || !d.available ? '' : d.distance_m < 150 ? 'near' : d.distance_m < 500 ? 'mid' : 'far');
      $('seeker-dist-age').textContent = d ? (d.available ? `updated ${fmtAgo(now - d.at)}` : 'no seeker position yet') : 'waiting for first update…';
      drawMates(S.hiderMap, st.you.id, COLOURS.hider, now, S.hiderMapAt, 'hider');
      $('map-age').textContent = S.hiderMapAt ? `Teammates updated ${fmtAgo(now - S.hiderMapAt)}` : '';
    } else {
      const head = phase === 'head_start';
      $('headstart').classList.toggle('hidden', !head);
      $('compass-wrap').classList.toggle('hidden', head);
      $('btn-catch').disabled = head;
      const c = S.compass;
      $('hider-dist').textContent = c && c.available ? fmtDist(c.distance_m) : '—';
      let note = !c ? 'waiting for first update…' : !c.available ? 'no hider position available' : `updated ${fmtAgo(now - c.at)}`;
      if (!Geo.compassActive()) note += ' · no compass: top of arrow = north';
      $('compass-note').textContent = note;
      drawMates(S.seekerMap, st.you.id, COLOURS.seeker, now, S.seekerMapAt, 'seeker');
      $('map-age').textContent = S.seekerMapAt ? `Seekers updated ${fmtAgo(now - S.seekerMapAt)}` : '';
    }
    renderGpsWarn();
    updateTimers();
  }

  // Redraw teammate markers only when a new snapshot arrives or every 20 s (to refresh "x min ago").
  function drawMates(players, myId, colour, now, at, team) {
    const key = `${team}:${at}:${Math.floor(now / 20)}`;
    if (key === S.matesKey) return;
    S.matesKey = key;
    GameMap.setMates(players, myId, colour, now, at);
  }

  function updateTimers() {
    const st = S.state;
    const now = serverNow();
    for (const el of document.querySelectorAll('.countdown[data-expires]')) {
      el.textContent = Math.max(0, Math.ceil(Number(el.dataset.expires) - now)) + 's';
    }
    if (!st) return;
    const L = st.lobby;
    if (L.phase === 'head_start') {
      $('timer').textContent = 'Head start ' + fmtClock(L.active_at - now);
      $('headstart-timer').textContent = fmtClock(L.active_at - now);
    } else if (L.phase === 'active') {
      $('timer').textContent = L.survival_at && now < L.survival_at
        ? 'Survive ' + fmtClock(L.survival_at - now)
        : 'Ends ' + fmtClock(L.ends_at - now);
    }
  }

  // After the game: everyone on one map, coloured by the team they finished on.
  function renderEndMap() {
    const st = S.state;
    if (!st || st.lobby.phase !== 'ended') return;
    const now = serverNow();
    drawMates(S.allMap, st.you.id, (p) => COLOURS[p.team] || '#888', now, S.allMapAt, 'all');
    $('end-map-age').textContent = S.allMapAt ? `updated ${fmtAgo(now - S.allMapAt)}` : '';
  }

  function renderResults() {
    const st = S.state, L = st.lobby;
    let title, won;
    if (L.winner === 'last_hider') {
      const champ = st.roster.find((p) => p.id === L.winner_id);
      won = st.you.id === L.winner_id;
      title = won ? 'You win: last hider standing!' : `${champ ? champ.name : 'The last hider'} wins: last hider standing!`;
    } else {
      won = st.you.team === L.winner;
      title = L.winner === 'seeker' ? 'Seekers win!' : 'Hiders win!';
    }
    $('result-title').textContent = title + (won ? ' 🎉' : '');
    renderEndMap();
    const t0 = L.active_at || L.started_at;
    const evs = L.events.map((e) => e.t === 'caught'
      ? `<li><span class="muted">${fmtClock(e.at - t0)}</span> ${esc(e.hider)} caught by ${esc(e.seeker)}</li>`
      : `<li><span class="muted">${fmtClock(e.at - t0)}</span> ${esc(e.name)} left the game</li>`);
    $('result-events').innerHTML = evs.join('') || '<li class="muted">No catches.</li>';
    $('result-players').innerHTML = st.roster.map((p) => {
      const status = p.caught_at ? `caught after ${fmtClock(p.caught_at - t0)}` : p.team === 'hider' ? 'never caught 🏆' : 'seeker';
      return `<li><strong>${esc(p.name)}</strong> <span class="muted">· ${status}</span></li>`;
    }).join('');
  }

  // ---------- compass animation ----------
  function animateArrow() {
    const c = S.compass;
    if (c && c.available) {
      const heading = Geo.heading ?? 0;
      const target = c.bearing_deg - heading;
      const diff = ((target - S.arrowAngle) % 360 + 540) % 360 - 180;
      S.arrowAngle += diff * 0.15;
      $('arrow').setAttribute('transform', `rotate(${S.arrowAngle.toFixed(1)})`);
      $('arrow').classList.remove('off');
    } else {
      $('arrow').classList.add('off');
    }
    const ticks = $('dial-ticks');
    ticks.setAttribute('transform', `rotate(${-(Geo.heading ?? 0)})`);
    requestAnimationFrame(animateArrow);
  }

  function buildDial() {
    let html = '';
    for (let a = 0; a < 360; a += 30) {
      const long = a % 90 === 0;
      html += `<line x1="0" y1="-92" x2="0" y2="${long ? -80 : -86}" transform="rotate(${a})" class="tick"/>`;
    }
    html += '<text x="0" y="-64" class="north">N</text>';
    $('dial-ticks').innerHTML = html;
  }

  // ---------- modals ----------
  function openModal(kind, title, bodyHtml, actions) {
    S.modalKind = kind;
    $('modal-title').textContent = title;
    $('modal-body').innerHTML = bodyHtml;
    const box = $('modal-actions');
    box.innerHTML = '';
    for (const [label, cls, fn] of actions) {
      const b = document.createElement('button');
      b.textContent = label; b.className = cls;
      b.onclick = fn;
      box.appendChild(b);
    }
    $('modal').classList.remove('hidden');
    updateTimers();
  }
  function closeModal() { S.modalKind = null; $('modal').classList.add('hidden'); }

  function showCandidates(hiders) {
    const r = S.state ? S.state.lobby.settings.catch_radius_m : '?';
    if (!hiders.length) {
      openModal('candidates', 'Nobody in range',
        `<p>No hiders are within ${r} m of you (according to GPS). Get closer and try again.</p>`,
        [['OK', 'primary', closeModal]]);
      return;
    }
    openModal('candidates', 'Who did you catch?', `<p class="muted">Hiders within ${r} m of you:</p>`,
      [...hiders.map((h) => [h.name, 'primary', () => { send({ t: 'catch_request', hider_id: h.id }); closeModal(); }]),
       ['Cancel', 'ghost', closeModal]]);
  }

  function showPrompt(p) {
    vibrate([400, 150, 400]);
    openModal('prompt', `${p.seeker_name} says they caught you`,
      `<p>Confirm only if you really were caught. You'll become a seeker.</p>
       <p class="muted">Auto-declines in <span class="countdown" data-expires="${p.expires_at}"></span></p>`,
      [['Yes, I was caught', 'danger', () => { send({ t: 'catch_reply', catch_id: p.catch_id, accept: true }); closeModal(); }],
       ['No', 'ghost', () => { send({ t: 'catch_reply', catch_id: p.catch_id, accept: false }); closeModal(); }]]);
  }

  function showWaiting(m) {
    openModal('waiting', `Waiting for ${m.hider_name}…`,
      `<p>They need to confirm on their phone.</p>
       <p class="muted">Expires in <span class="countdown" data-expires="${m.expires_at}"></span></p>`,
      [['Hide', 'ghost', closeModal]]);
  }

  // ---------- team chat ----------
  const QUICK_EMOJIS = ['👍', '👎', '👀', '🏃', '🙈', '⚠️', '🆘', '👋'];

  function showChatMenu(mate) {
    const st = S.state;
    if (!st || !['head_start', 'active', 'ended'].includes(st.lobby.phase)) return;
    const r = st.roster.find((p) => p.id === mate.id);
    if (!r) { toast(`${mate.name} has left`, 'error'); return; }
    if (st.lobby.phase !== 'ended' && r.team !== st.you.team) { toast(`${mate.name} isn't on your team any more`, 'error'); return; }
    openModal('chat', `Message ${mate.name}`, `
      ${r.connected ? '' : '<p class="warn-text">They look offline: the message may not arrive.</p>'}
      <div class="emoji-grid">${QUICK_EMOJIS.map((e) => `<button class="emoji-btn" data-emoji="${e}">${e}</button>`).join('')}</div>
      <form id="chat-form" class="chat-form">
        <input id="chat-text" maxlength="200" placeholder="Type a message…" autocomplete="off" enterkeyhint="send">
        <button type="submit" class="primary small">Send</button>
      </form>`,
      [['Cancel', 'ghost', closeModal]]);
    const sendText = (text) => {
      text = text.trim();
      if (!text) return;
      if (send({ t: 'chat', to: mate.id, text })) closeModal();
      else toast('Not connected: message not sent', 'error');
    };
    $('modal-body').querySelector('.emoji-grid').onclick = (e) => {
      const b = e.target.closest('[data-emoji]'); if (b) sendText(b.dataset.emoji);
    };
    $('chat-form').onsubmit = (e) => { e.preventDefault(); sendText($('chat-text').value); };
  }

  function showIncomingChat(m) {
    vibrate([100, 60, 100]);
    toast(`💬 ${m.from_name}: ${m.text}`, 'chat', 8000);
  }

  // ---------- wiring ----------
  Geo.onPosition = (p) => {
    const first = !S.gpsOk;
    S.gpsOk = true; S.gpsError = null;
    GameMap.setMe(p.lat, p.lon, p.acc);
    sendPos(Geo.sim);
    if (first && S.state && S.state.lobby.phase === 'lobby') renderRoom();
  };
  Geo.onError = (msg) => { S.gpsError = msg; if (S.state && S.state.lobby.phase === 'lobby') renderRoom(); };
  GameMap.onTap = (lat, lon) => { if (Geo.sim) Geo.setSimPosition(lat, lon); };
  GameMap.onMateTap = showChatMenu;

  $('nickname').value = localStorage.getItem('manhunt_nick') || '';
  $('btn-create').onclick = createLobby;
  $('btn-refresh').onclick = refreshLobbies;
  $('lobby-list').onclick = (e) => { const id = e.target.dataset.join; if (id) joinLobby(id); };
  $('btn-perms').onclick = requestPermissions;
  $('btn-leave-room').onclick = leave;
  $('btn-leave-game').onclick = leave;
  $('btn-home').onclick = () => { send({ t: 'leave' }); resetToHome(); };
  $('btn-start').onclick = () => send({ t: 'start' });
  $('btn-catch').onclick = () => send({ t: 'catch_list' });
  $('roster').onclick = (e) => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.team) send({ t: 'set_team', player_id: b.dataset.pid, team: b.dataset.team });
    if (b.dataset.kick && confirm('Remove this player?')) send({ t: 'kick', player_id: b.dataset.kick });
  };
  $('settings').addEventListener('change', (e) => {
    const k = e.target.dataset.setting; if (!k) return;
    const [, , , factor] = SETTINGS_UI.find((s) => s[0] === k);
    send({ t: 'set_settings', settings: { [k]: Math.round(Number(e.target.value) * factor) } });
  });
  // Re-acquire the wake lock on any tap (browsers drop it when the tab is hidden).
  document.addEventListener('click', () => { if (S.session) Geo.keepAwake(); });

  buildDial();
  requestAnimationFrame(animateArrow);
  setInterval(() => { updateTimers(); if (S.state && S.state.lobby.phase !== 'lobby') { renderGame(); renderEndMap(); } }, 1000);
  setInterval(() => sendPos(true), 15000);
  setInterval(ping, 20000);
  setInterval(() => { if (!S.session && !$('screen-home').classList.contains('hidden')) refreshLobbies(); }, 5000);

  if (Geo.sim) document.title = 'Manhunt (sim)';
  if (S.session) {
    Geo.start();
    if (!Geo.needsCompassPermission()) Geo.startCompass();
    connect();
    banner('Connecting…');
  } else {
    if (!Geo.needsCompassPermission()) Geo.startCompass();
    show('home');
  }
})();
