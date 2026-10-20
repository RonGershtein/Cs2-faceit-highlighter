// Viewer page glue: loads data, wires controls, scoreboard, kill feed, highlights panel.
(async () => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const id = new URLSearchParams(location.search).get('id');
  if (!id) { $('title').textContent = 'no demo id'; return; }
  const status = $('status');
  const toast = (html, ms = 8000) => {
    const el = document.createElement('div'); el.className = 'toast'; el.setAttribute('role', 'status'); el.setAttribute('aria-live', 'polite'); el.innerHTML = html;
    document.body.appendChild(el); setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 160); }, ms);
    return el;
  };

  status.textContent = 'loading…';
  let demo, events, frames, highlightsAll, config;
  try {
    [demo, events, frames, highlightsAll, config] = await Promise.all([
      API.getDemo(id), API.events(id), API.frames(id), API.highlights(id), API.getConfig(),
    ]);
  } catch (e) { $('title').textContent = 'failed to load: ' + e.message; return; }
  const meta = demo.meta;
  const mapInfo = demo.map || { pos_x: 0, pos_y: 0, scale: 1, threshold_z: 0, has_radar: false, has_lower: false };
  const loadImg = (src) => new Promise((res) => { const i = new Image(); i.onload = () => res(i); i.onerror = () => res(null); i.src = src; });
  const [radarImg, lowerImg] = await Promise.all([
    mapInfo.has_radar ? loadImg(`/api/radars/${meta.map}.png`) : null,
    mapInfo.has_lower ? loadImg(`/api/radars/${meta.map}_lower.png`) : null,
  ]);
  if (!radarImg) toast(`No radar image for <b>${esc(meta.map)}</b>; run <code>python scripts/fetch_radars.py ${esc(meta.map)}</code>`);

  let me = config.my_steamid || null;
  if (me && !meta.players.some(p => p.steamid === me)) me = null;

  const replay = new Replay({ canvas: $('map'), meta, frames, events, mapInfo, radarImg, lowerImg });
  replay.me = me;
  window.replay = replay;
  $('title').textContent = `${meta.map} · ${meta.team_a.name} ${meta.score.a} : ${meta.score.b} ${meta.team_b.name}`;
  document.title = `${meta.map} ${meta.score.a}:${meta.score.b} – CS2 Watcher`;
  status.textContent = `${frames.length} frames, ${events.kills.length} kills`;
  setTimeout(() => status.textContent = '', 4000);
  $('btn-level').style.display = mapInfo.has_lower ? '' : 'none';

  // ---- highlights panel --------------------------------------------------------------
  let filter = me ? 'mine' : 'all';
  let includeKills = false;
  let listed = [];
  let playAllIdx = -1;
  const selected = new Set();
  function selectedHighlights() { return listed.filter(h => selected.has(h.id)); }
  function updateSelCount() {
    const sel = selectedHighlights();
    const toRecord = sel.filter(h => !videoFor(h)).length;
    const withVideo = sel.length - toRecord;
    $('sel-count').textContent = `${sel.length} selected`;
    $('btn-record-sel').disabled = toRecord === 0;
    $('btn-record-sel').innerHTML = toRecord ? `<i class="rec-dot"></i> Record ${toRecord} selected` : '<i class="rec-dot"></i> Record selected';
    const db = $('btn-del-clips');
    db.style.display = withVideo ? '' : 'none';
    db.innerHTML = `${ic('trash')} Delete ${withVideo} clip${withVideo === 1 ? '' : 's'}`;
    const lb = $('btn-long');
    lb.disabled = sel.length < 2;
    lb.innerHTML = `${ic('film')} Montage${sel.length >= 2 ? ` (${sel.length})` : ''}`;
    lb.title = sel.length >= 2 ? `join the ${sel.length} selected highlights into one video with one song over it`
      : 'select at least two highlights';
  }
  const timeline = new Timeline($('timeline'), replay, [], me);

  function filteredHighlights() {
    let hs = highlightsAll;
    if (filter === 'mine' && me) hs = hs.filter(h => h.player === me);
    if (!includeKills) hs = hs.filter(h => h.type !== 'kill');
    return hs;
  }
  function renderHighlights() {
    listed = filteredHighlights();
    $('f-mine').classList.toggle('active', filter === 'mine');
    $('f-all').classList.toggle('active', filter === 'all');
    $('f-kills').classList.toggle('active', includeKills);
    $('f-mine').style.display = me ? '' : 'none';
    $('csdm-all').href = API.csdmUrl(id, filter === 'mine' ? me : null);
    const cur = replay.tick;
    $('hl-list').innerHTML = listed.length ? listed.map((h, i) => `
      <div class="hl ${cur >= h.start_tick && cur <= h.end_tick ? 'current' : ''} ${selected.has(h.id) ? 'selected' : ''}" data-i="${i}" role="button" tabindex="0" aria-label="Play highlight: ${esc(h.description)}" style="--i:${Math.min(i, 9)}">
        <label class="chk" title="${videoFor(h) ? 'select for a long video' : 'select for recording'}"><input type="checkbox" class="sel" data-id="${h.id}" ${selected.has(h.id) ? 'checked' : ''}></label>
        <div>
          <div class="head"><span class="type ${h.type}">${esc(h.type.replace('_', ' '))}${h.n ? ` ${h.type === 'clutch' ? '1v' + h.n : h.n + 'K'}` : ''}</span>
            <span class="meta">round ${h.round} · ${h.score} pts</span></div>
          <div class="player">${esc(h.player_name)}</div>
          <div class="desc">${esc(h.description)}</div>
          <div class="actions">
            <button class="small act-play" data-i="${i}">${ic('play')} Play</button>
            <button class="small act-cs2" data-i="${i}" title="open CS2 at this moment">Open in CS2</button>
            ${videoFor(h) ? `<button class="small act-video has-video" data-i="${i}" title="watch the recorded MP4">${ic('play')} Video</button>${videoFor(h).cine ? `<button class="small act-cine has-video" data-i="${i}" title="watch the cinematic version (slow-motion + music)">${ic('sparkles')} Cinematic</button>` : ''}` : `<button class="small act-rec" data-i="${i}" title="record only this highlight"><i class="rec-dot"></i> Rec</button>`}
            <button class="small ghost act-csdm" data-i="${i}" title="copy a CS Demo Manager sequence to the clipboard">CSDM</button>
          </div>
        </div>
      </div>`).join('') : '<div class="muted">No highlights for this filter.</div>';
    timeline.setHighlights(listed);
    updateSelCount();
    if (!renderHighlights.done) { renderHighlights.done = true; $('hl-list').classList.add('enter'); setTimeout(() => $('hl-list').classList.remove('enter'), 900); }
  }
  function playHighlight(i) {
    const h = listed[i]; if (!h) return;
    replay.me = replay.me || h.player;
    replay.playClip(h.start_tick, h.end_tick);
    renderHighlights();
    const el = $('hl-list').querySelector(`.hl[data-i="${i}"]`); if (el) el.scrollIntoView({ block: 'nearest' });
  }
  replay.onClipEnd = () => {
    if (playAllIdx >= 0 && playAllIdx < listed.length - 1) { playAllIdx++; playHighlight(playAllIdx); }
    else playAllIdx = -1;
  };
  $('hl-list').addEventListener('change', (e) => {
    const cb = e.target.closest('input.sel'); if (!cb) return;
    const hid = +cb.dataset.id;
    if (cb.checked) selected.add(hid); else selected.delete(hid);
    cb.closest('.hl').classList.toggle('selected', cb.checked);
    updateSelCount();
  });
  $('hl-list').addEventListener('click', async (e) => {
    if (e.target.closest('.chk')) return;
    const b = e.target.closest('button');
    const card = e.target.closest('.hl');
    if (!card) return;
    const i = +card.dataset.i; const h = listed[i];
    if (b?.classList.contains('act-cs2')) {
      try {
        const r = await API.launch(id, h.start_tick, h.player_name, h.player);
        toast(`<b>CS2 launching…</b><br>${esc(r.hint)}`, 15000);
      } catch (err) { toast(`<b>Could not launch CS2:</b> ${esc(err.message)}`); }
      return;
    }
    if (b?.classList.contains('act-video')) { openVideo(videoFor(h)); return; }
    if (b?.classList.contains('act-cine')) { const v = videoFor(h); openVideo({ ...v, file: v.cine, description: v.description + ' (cinematic)', seconds: v.cine_info?.seconds ?? v.seconds }); return; }
    if (b?.classList.contains('act-rec')) { startRecording([h.id]); return; }
    if (b?.classList.contains('act-csdm')) {
      const seq = { number: 1, startTick: h.start_tick, endTick: h.end_tick, showXRay: false, showAssists: true, showOnlyDeathNotices: true,
        playersOptions: [], playerCameras: [{ tick: h.start_tick, playerSteamId: String(h.player), playerName: h.player_name }],
        cameras: [], playerVoicesEnabled: false, recordAudio: true, deathNoticesDuration: 5 };
      try { await navigator.clipboard.writeText(JSON.stringify([seq], null, 2)); toast('CSDM sequence copied to clipboard', 3000); }
      catch (_) { toast('Clipboard blocked; use the CSDM export link instead'); }
      return;
    }
    playAllIdx = -1;
    playHighlight(i);
  });
  $('f-mine').onclick = () => { filter = 'mine'; renderHighlights(); };
  $('f-all').onclick = () => { filter = 'all'; renderHighlights(); };
  $('f-kills').onclick = () => { includeKills = !includeKills; renderHighlights(); };
  $('btn-play-all').onclick = () => { if (!listed.length) return; playAllIdx = 0; playHighlight(0); };

  // ---- scoreboard & kill feed ---------------------------------------------------------------
  const F = meta.flags;
  const WEAPON_NAMES = { usp_silencer: 'USP-S', hkp2000: 'P2000', glock: 'Glock', elite: 'Dualies', p250: 'P250', fiveseven: 'Five-SeveN', tec9: 'Tec-9',
    cz75a: 'CZ75', deagle: 'Deagle', revolver: 'R8', ak47: 'AK-47', m4a1: 'M4A4', m4a1_silencer: 'M4A1-S', famas: 'FAMAS', galilar: 'Galil',
    aug: 'AUG', sg556: 'SG 553', awp: 'AWP', ssg08: 'Scout', scar20: 'SCAR-20', g3sg1: 'G3SG1', mac10: 'MAC-10', mp9: 'MP9', mp7: 'MP7',
    mp5sd: 'MP5-SD', ump45: 'UMP-45', p90: 'P90', bizon: 'PP-Bizon', nova: 'Nova', xm1014: 'XM1014', mag7: 'MAG-7', sawedoff: 'Sawed-Off',
    m249: 'M249', negev: 'Negev', hegrenade: 'HE', flashbang: 'Flash', smokegrenade: 'Smoke', molotov: 'Molotov', incgrenade: 'Incendiary',
    inferno: 'Molotov', decoy: 'Decoy', knife: 'Knife', taser: 'Zeus', world: 'World', planted_c4: 'C4' };
  const wname = (w) => WEAPON_NAMES[w] || w;
  const playersBySid = new Map(meta.players.map(p => [p.steamid, p]));
  const statsBySid = new Map((demo.players || []).map(p => [p.steamid, p]));
  function renderScoreboard(st, round) {
    const rows = st.players.slice();
    const teams = { CT: [], T: [] };
    for (const p of rows) { const mp = meta.players[p.idx]; if (teams[p.team]) teams[p.team].push({ ...p, meta: mp, stats: statsBySid.get(mp.steamid) }); }
    const teamName = (side) => { for (const [name, t] of [[meta.team_a.name, meta.team_a], [meta.team_b.name, meta.team_b]]) { if (t.players.some(s => round.teams[s] === side)) return name; } return side; };
    const meName = me ? (playersBySid.get(me)?.name || '') : '';
    const html = `<div class="sb-head"><span class="muted">You are</span> <b>${me ? esc(meName) : '<span class="muted">not set</span>'}</b><span class="spacer"></span><button class="small" id="btn-who">${me ? 'Change' : 'Pick me'}</button></div>` + ['CT', 'T'].map(side => {
      const list = teams[side].sort((a, b) => (b.stats?.kills ?? 0) - (a.stats?.kills ?? 0));
      return `<div class="teamname ${side.toLowerCase()}">${side} · ${esc(teamName(side))}</div>
      <table><colgroup><col class="c-name"><col class="c-hp"><col class="c-w"><col class="c-n"><col class="c-n"><col class="c-n"></colgroup>
      <thead><tr><th>Player</th><th>HP</th><th>Weapon</th><th>K</th><th>D</th><th>A</th></tr></thead><tbody>
      ${list.map(p => `<tr class="${p.alive ? '' : 'dead'} ${p.meta.steamid === me ? 'me' : ''}">
        <td class="name" title="${esc(p.meta.name)} · $${p.money} · ADR ${p.stats?.adr ?? ''} · rating ${p.stats?.rating ?? ''}">${esc(p.meta.name)}${p.flags & F.bomb ? ' <span class="tag c4">C4</span>' : ''}${p.flags & F.defuser ? ' <span class="tag kit">KIT</span>' : ''}</td>
        <td>${p.alive ? `<span class="hp"><div style="width:${p.hp}%"></div></span> ${p.hp}` : '–'}</td>
        <td title="${esc(p.weapon)}">${p.alive ? esc(wname(p.weapon)) : ''}</td>
        <td>${p.stats?.kills ?? ''}</td><td>${p.stats?.deaths ?? ''}</td><td>${p.stats?.assists ?? ''}</td>
      </tr>`).join('')}</tbody></table>`;
    }).join('');
    $('panel-scoreboard').innerHTML = html;
  }
  function openWhoAmI() {
    const sides = { CT: [], T: [] };
    for (const p of meta.players) { const side = p.team_start === 'T' ? 'T' : 'CT'; sides[side].push(p); }
    const name = (side) => side === 'CT' ? meta.team_a.name : meta.team_b.name;
    $('me-list').innerHTML = ['CT', 'T'].map(side => `<div class="team"><div class="teamname ${side.toLowerCase()}">${esc(name(side))}</div>
      ${sides[side].map(p => { const st = statsBySid.get(p.steamid); return `<button class="p ${p.steamid === me ? 'me' : ''}" data-sid="${p.steamid}"><span>${esc(p.name)}</span><span class="st">${st ? `${st.kills}/${st.deaths} · ${st.rating}` : ''}</span></button>`; }).join('')}</div>`).join('');
    $('me-modal').style.display = '';
  }
  $('me-close').onclick = () => $('me-modal').style.display = 'none';
  $('me-modal').addEventListener('click', (e) => { if (e.target === $('me-modal')) $('me-modal').style.display = 'none'; });
  $('me-list').addEventListener('click', async (e) => {
    const b = e.target.closest('button.p'); if (!b) return;
    me = b.dataset.sid; replay.me = me; timeline.me = me;
    try { await API.setConfig({ my_steamid: me }); } catch (_) { /* ignore */ }
    $('me-modal').style.display = 'none';
    filter = 'mine'; renderHighlights(); renderScoreboard(replay.state(), replay.roundAt(replay.tick));
    toast(`You are <b>${esc(playersBySid.get(me)?.name || me)}</b>`, 2500);
  });
  $('panel-scoreboard').addEventListener('click', (e) => { if (e.target.closest('#btn-who')) openWhoAmI(); });
  let lastRoundRendered = -1;
  function fmtTick(dt) { const s = Math.max(0, dt / replay.tickrate); return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`; }

  // ---- per-tick HUD updates ------------------------------------------------------------------
  let lastPanel = 0;
  replay.onTick = (tick, st, round) => {
    const c = replay.clock(tick); const sc = replay.scoreAt(tick);
    $('hud-clock').textContent = `R${c.round} · ${c.text}`;
    $('hud-score').innerHTML = `<span>${esc(meta.team_a.name)}</span> <b>${sc.a}</b> <span class="muted">:</span> <b>${sc.b}</b> <span>${esc(meta.team_b.name)}</span>`;
    $('hud-bomb').textContent = c.bomb ? 'C4 planted' : '';
    $('time').textContent = `${fmtTick(tick - replay.first)} / ${fmtTick(replay.last - replay.first)} · tick ${Math.round(tick)}`;
    $('round-select').value = String(round.num);
    timeline.draw();
    const now = performance.now();
    if (now - lastPanel > 250 || !replay.playing) {
      lastPanel = now;
      const active = document.querySelector('.tabs button.active')?.dataset.tab;
      if (active === 'scoreboard') renderScoreboard(st, round);
      if (active === 'highlights') {
        for (const el of $('hl-list').querySelectorAll('.hl')) { const h = listed[+el.dataset.i]; el.classList.toggle('current', tick >= h.start_tick && tick <= h.end_tick); }
      }
    }
  };

  // ---- resizable side panel -----------------------------------------------------------------------------
  const viewerEl = document.querySelector('.viewer');
  // the side panel (highlights, scoreboard, kill feed) is the main working area: it takes ~62 % by default, the map the rest
  const setSideW = (px) => { const w = Math.max(360, Math.min(window.innerWidth * 0.78, px)); viewerEl.style.setProperty('--side-w', w + 'px'); try { localStorage.setItem('sideW2', String(w)); } catch (_) { /* ignore */ } };
  try { const saved = +localStorage.getItem('sideW2'); if (saved) viewerEl.style.setProperty('--side-w', saved + 'px'); else setSideW(Math.round(window.innerWidth * 0.62)); } catch (_) { setSideW(Math.round(window.innerWidth * 0.62)); }
  const divider = $('divider');
  let dragging = false;
  divider.addEventListener('pointerdown', (e) => { dragging = true; divider.classList.add('active'); divider.setPointerCapture(e.pointerId); });
  divider.addEventListener('pointermove', (e) => { if (dragging) { setSideW(window.innerWidth - e.clientX - 4); replay._resize(); replay.draw(); timeline._resize(); timeline.draw(); } });
  divider.addEventListener('pointerup', () => { dragging = false; divider.classList.remove('active'); });
  divider.addEventListener('dblclick', () => { setSideW(Math.round(window.innerWidth * 0.62)); replay._resize(); replay.draw(); timeline._resize(); timeline.draw(); });

  // ---- controls ------------------------------------------------------------------------------------
  const playBtn = $('btn-play');
  const syncPlay = () => { playBtn.innerHTML = replay.playing ? '<svg class="ico" viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg> Pause' : '<svg class="ico" viewBox="0 0 24 24"><path d="M7 5v14l11-7z"/></svg> Play'; };
  setInterval(syncPlay, 200);
  playBtn.onclick = () => { playAllIdx = -1; replay.toggle(); syncPlay(); };
  $('btn-next-kill').onclick = () => replay.nextKill();
  $('btn-prev-kill').onclick = () => replay.prevKill();
  $('btn-next-round').onclick = () => replay.nextRound();
  $('btn-prev-round').onclick = () => replay.prevRound();
  $('speed').onchange = (e) => replay.setSpeed(+e.target.value);
  $('round-select').innerHTML = meta.rounds.map(r => `<option value="${r.num}">Round ${r.num} · ${r.winner}</option>`).join('');
  $('round-select').onchange = (e) => replay.seekRound(+e.target.value);
  $('btn-watch-cs2').onclick = async () => {
    const r0 = meta.rounds[0];
    try {
      const r = await API.launch(id, Math.max(0, r0.freeze_end - 5 * replay.tickrate), me ? (playersBySid.get(me)?.name || null) : null, me);
      toast(`<b>CS2 launching with the full demo.</b><br>${esc(r.hint)}`, 15000);
    } catch (err) { toast(`<b>Could not launch CS2:</b> ${esc(err.message)}`); }
  };
  $('btn-level').onclick = () => { replay.showLower = !replay.showLower; $('btn-level').classList.toggle('active', replay.showLower); replay.draw(); };
  $('btn-labels').onclick = () => { replay.showLabels = !replay.showLabels; $('btn-labels').classList.toggle('active', replay.showLabels); replay.draw(); };
  $('btn-cones').onclick = () => { replay.showCones = !replay.showCones; $('btn-cones').classList.toggle('active', replay.showCones); replay.draw(); };
  for (const b of document.querySelectorAll('.tabs button')) {
    b.onclick = () => {
      document.querySelectorAll('.tabs button').forEach(x => x.classList.toggle('active', x === b));
      document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + b.dataset.tab));
      replay.draw();
    };
  }
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    const tr = replay.tickrate;
    switch (e.key) {
      case ' ': e.preventDefault(); playAllIdx = -1; replay.toggle(); syncPlay(); break;
      case 'ArrowLeft': replay.seek(replay.tick - 5 * tr, true); break;
      case 'ArrowRight': replay.seek(replay.tick + 5 * tr, true); break;
      case ',': replay.stepFrames(-1); break;
      case '.': replay.stepFrames(1); break;
      case '[': replay.prevKill(); break;
      case ']': replay.nextKill(); break;
      case 'PageUp': e.preventDefault(); replay.prevRound(); break;
      case 'PageDown': e.preventDefault(); replay.nextRound(); break;
      case 'l': case 'L': $('btn-level').click(); break;
      default: return;
    }
  });

  // ---- recording (CS2 + HLAE + FFmpeg) ----------------------------------------------------------
  let videos = { videos: [], long: [] };
  let tools = null;
  let recPoll = null;
  function videoFor(h) { return videos.videos.find(v => v.highlight_id === h.id) || null; }
  async function loadVideos() {
    try { videos = await API.videos(id); } catch (_) { videos = { videos: [], long: [] }; }
    videos.long = videos.long || [];
    renderLongList();
    $('btn-export').style.display = videos.videos.length ? '' : 'none';
    $('btn-cine').style.display = videos.videos.length ? '' : 'none';
    if (videos.export_folder) $('btn-export').title = `copy all clips to ${videos.export_folder}`;
  }
  function renderLongList() {
    $('long-list').innerHTML = videos.long.map((l, i) => `<span class="pill" title="${esc(l.file)}">${ic('film')} ${esc(l.description)} · ${l.seconds}s
      <button class="small act-long-play" data-i="${i}" title="watch">${ic('play')}</button>
      <a class="small" href="${API.videoUrl(id, l.file)}" download title="download">${ic('download')}</a>
      <button class="small ghost act-long-del" data-i="${i}" title="delete this long video">${ic('x')}</button></span>`).join('');
  }
  $('long-list').addEventListener('click', async (e) => {
    const b = e.target.closest('button'); if (!b) return;
    const l = videos.long[+b.dataset.i]; if (!l) return;
    if (b.classList.contains('act-long-play')) openVideo(l);
    if (b.classList.contains('act-long-del') && confirm(`Delete ${l.file}? (the copy in Downloads stays)`)) {
      try { await API.releaseVideo(l.file); await API.deleteLongVideo(id, l.file); await loadVideos(); } catch (err) { toast(esc(err.message)); }
    }
  });
  function openVideo(v) {
    if (!v) return;
    $('video-title').textContent = `${v.description} (${v.seconds}s)`;
    $('video-el').src = API.videoUrl(id, v.file);
    $('video-dl').href = API.videoUrl(id, v.file);
    $('video-modal').style.display = '';
  }
  $('video-close').onclick = () => { $('video-modal').style.display = 'none'; $('video-el').pause(); $('video-el').removeAttribute('src'); $('video-el').load(); };
  $('video-modal').addEventListener('click', (e) => { if (e.target === $('video-modal')) $('video-close').click(); });
  function recStatusText(st) {
    const el = $('recstatus');
    el.classList.toggle('err', st.status === 'error');
    if (!st || st.status === 'idle') { el.innerHTML = ''; return; }
    const pct = Math.round((st.progress || 0) * 100);
    let txt = `${st.stage || st.status}`;
    if (st.status === 'error') txt = `failed: ${st.error}`;
    if (st.status === 'done') txt = `done: ${st.outputs.length} clip(s) recorded` + (st.exported ? ` · saved to ${st.exported}` : '') + (st.warning ? ` · ${st.warning}` : '');
    el.innerHTML = `${esc(txt)}${st.status === 'recording' || st.status === 'encoding' || st.status === 'preparing' ? `<span class="progress"><div style="width:${pct}%"></div></span>${st.hint ? `<div>${esc(st.hint)}</div>` : ''}` : ''}`;
    $('btn-rec-cancel').style.display = (st.status === 'recording' || st.status === 'preparing') ? '' : 'none';
  }
  async function pollRecord() {
    try {
      const st = await API.recordStatus();
      recStatusText(st);
      if (st.status === 'done' || st.status === 'error' || st.status === 'idle') {
        clearInterval(recPoll); recPoll = null;
        if (st.status === 'done') { await loadVideos(); renderHighlights(); }
      }
    } catch (_) { /* ignore */ }
  }
  async function startRecording(ids) {
    if (!ids.length) return;
    if (tools && !tools.ready) {
      toast(tools.windows ? 'Recording needs HLAE + FFmpeg. Click "Install HLAE + FFmpeg" first.' : 'Recording only works when the app runs on Windows with CS2 installed.');
      return;
    }
    $('rec-n').textContent = ids.length;
    $('rec-modal').style.display = '';
    $('rec-go').onclick = async () => {
      $('rec-modal').style.display = 'none';
      try {
        recStatusText(await API.record(id, ids));
        if (!recPoll) recPoll = setInterval(pollRecord, 2000);
      } catch (e) { toast(`<b>Could not start recording:</b> ${esc(e.message)}`); }
    };
  }
  $('rec-close').onclick = $('rec-cancel-btn').onclick = () => $('rec-modal').style.display = 'none';
  $('btn-select-all').onclick = () => {
    const ids = listed.map(h => h.id);
    const all = ids.every(id => selected.has(id));
    for (const id of ids) { if (all) selected.delete(id); else selected.add(id); }
    renderHighlights();
  };
  $('btn-record-sel').onclick = () => startRecording(listed.filter(h => selected.has(h.id) && !videoFor(h)).map(h => h.id));
  $('btn-rec-cancel').onclick = async () => { await API.recordCancel(); };
  // ---- montage of the selected highlights: one song over the whole video, or the recorded clips as they are ----
  $('btn-long').onclick = async () => {
    const sel = selectedHighlights().sort((a, b) => a.start_tick - b.start_tick);
    if (sel.length < 2) return;
    const withVideo = sel.filter(h => videoFor(h));
    const total = withVideo.reduce((s, h) => s + (videoFor(h).seconds || 0), 0);
    $('long-clips').innerHTML = sel.map(h => `<div>round ${h.round} · ${esc(h.player_name)} · ${esc(h.description)} ${videoFor(h) ? `(${videoFor(h).seconds}s)` : '<span class="badge">to record</span>'}</div>`).join('')
      + `<div style="margin-top:4px"><b>${sel.length} plays</b>${withVideo.length ? ` · ${withVideo.length} recorded, about ${Math.round(total)}s as recorded` : ''}</div>`;
    const anyCine = withVideo.some(h => videoFor(h).cine);
    const music = $('long-music'), cur = music.value;
    try {
      const m = await API.music();
      music.innerHTML = '<option value="">random track</option>' + m.files.map(f => `<option value="${esc(f)}">${esc(f.replace(/\.[^.]+$/, ''))}</option>`).join('');
      music.value = m.files.includes(cur) ? cur : '';
      $('long-music-hint').textContent = m.files.length ? '' : 'no music yet: add tracks in Cinematic → music';
    } catch (_) { /* keep the list as it was */ }
    const sync = () => {
      const song = $('long-song').checked;
      $('long-song-row').style.display = song ? '' : 'none';
      $('long-cine-field').style.display = !song && anyCine ? '' : 'none';
      const skipped = song ? 0 : sel.length - withVideo.length;
      $('long-skip').style.display = skipped ? '' : 'none';
      $('long-skip').textContent = skipped ? `${skipped} selected highlight(s) have no recorded clip yet and will be left out. Record them first, or pick "Cinematic with one song", which records them.` : '';
      $('long-go').disabled = !song && withVideo.length < 2;
    };
    $('long-song').onchange = $('long-plain').onchange = sync;
    sync();
    $('long-msg').textContent = '';
    $('long-modal').style.display = '';
    $('long-go').onclick = async () => {
      $('long-go').disabled = true;
      if ($('long-song').checked) {
        $('long-msg').textContent = 'starting…';
        try {
          const r = await API.squadReel(id, { cinematic: true, highlight_ids: sel.map(h => h.id), music: music.value });
          $('long-modal').style.display = 'none';
          toast(`${ic('film')} Making the montage: ${r.total} plays. ${sel.length > withVideo.length ? 'Missing clips are recorded first; ' : ''}progress shows above the highlights.`, 7000);
          startSquadPoll();
        } catch (e) { $('long-msg').textContent = e.message; $('long-go').disabled = false; }
        return;
      }
      $('long-msg').textContent = 'joining clips…';
      try {
        const r = await API.longVideo(id, withVideo.map(h => h.id), anyCine && $('long-cine').checked);
        $('long-modal').style.display = 'none';
        await loadVideos();
        toast(`<b>${esc(r.file)}</b> created (${r.seconds}s)` + (r.exported ? `<br>saved to ${esc(r.exported)}` : ''), 8000);
        openVideo(r);
      } catch (e) { $('long-msg').textContent = e.message; $('long-go').disabled = false; }
    };
  };
  $('long-close').onclick = () => $('long-modal').style.display = 'none';
  // ---- delete recorded clips of the selected highlights ----------------------------------------------
  $('btn-del-clips').onclick = () => {
    const withVideo = selectedHighlights().filter(h => videoFor(h)).sort((a, b) => a.start_tick - b.start_tick);
    if (!withVideo.length) return;
    $('del-clips').innerHTML = withVideo.map(h => `<div>round ${h.round} · ${esc(h.player_name)} · ${esc(h.description)}${videoFor(h).cine ? ' <span class="muted">(+ cinematic)</span>' : ''}</div>`).join('');
    $('del-msg').textContent = '';
    $('del-go').disabled = false;
    $('del-modal').style.display = '';
    $('del-go').onclick = async () => {
      $('del-go').disabled = true;
      try {
        const r = await API.deleteClips(id, withVideo.map(h => h.id), $('del-downloads').checked);
        $('del-modal').style.display = 'none';
        for (const hid of r.deleted) selected.delete(hid);
        await loadVideos(); renderHighlights();
        toast(`${r.deleted.length} clip(s) deleted`, 4000);
      } catch (e) { $('del-msg').textContent = e.message; $('del-go').disabled = false; }
    };
  };
  $('del-close').onclick = $('del-cancel').onclick = () => $('del-modal').style.display = 'none';
  $('del-modal').addEventListener('click', (e) => { if (e.target === $('del-modal')) $('del-modal').style.display = 'none'; });
  $('long-modal').addEventListener('click', (e) => { if (e.target === $('long-modal')) $('long-modal').style.display = 'none'; });
  // keyboard activation for the clickable cards (Enter / Space act like a click on the card itself)
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const t = e.target; if (!(t instanceof HTMLElement) || t.getAttribute('role') !== 'button' || t.tagName === 'BUTTON') return;
    e.preventDefault(); t.click();
  });
  // ---- cinematic (slow-motion + music) -------------------------------------------------------------
  let cinePoll = null;
  const CINE_NUM = ['cine_slow', 'cine_game_volume', 'cine_music_volume', 'cine_trim_lead', 'cine_fast', 'cine_voice_volume'];
  const CINE_STR = ['cine_kills', 'cine_music', 'cine_trim'];
  let voicePins = { start: [], kill: [], final: [] };   // cine_voice_pick: pinned lines per slot (empty = random)
  const CINE_BOOL = ['cine_smooth', 'cine_music_sync', 'cine_skip_waits', 'cine_ramp', 'cine_impact', 'cine_look', 'cine_fade', 'cine_title', 'cine_counter', 'cine_card', 'cine_voice'];
  const VOICE_SLOTS = ['start', 'kill', 'final'];
  const VOICE_SUB_LABEL = { ace: 'ace', clutch: 'clutch', multikill: 'multikill', knife: 'knife', ninja_defuse: 'ninja', 2: '2nd kill', 3: '3rd kill', 4: '4th kill', 5: '5th kill' };
  let cineInfo = null;
  function cineRangeLabel(el) {
    const lab = $(el.id + '-val'); if (!lab) return;
    const v = parseFloat(el.value);
    lab.textContent = el.dataset.fmt === 'pct' ? `${Math.round(v * 100)}%` : String(v);
  }
  function cinePaceVis() {
    const t = $('cine_trim').value;
    document.querySelectorAll('.cine-trim-opt').forEach(el => { el.style.display = t === 'off' ? 'none' : ''; });
    document.querySelectorAll('.cine-fast-opt').forEach(el => { el.style.display = t === 'fast' ? '' : 'none'; });
  }
  function cineCounts() {
    const nAll = videos.videos.filter(v => !v.cine).length;
    const nSel = selectedHighlights().filter(h => videoFor(h)).length;
    const nCine = videos.videos.filter(v => v.cine).length;
    $('cine-n-all').textContent = nAll; $('cine-n-sel').textContent = nSel; $('cine-n-join').textContent = nCine;
    $('cine-join').disabled = nCine < 2;
    $('cine-which-all').disabled = nAll === 0; $('cine-which-sel').disabled = nSel === 0;
    $('cine-which-sel').checked = nSel > 0; $('cine-which-all').checked = nSel === 0;
    return { nAll, nSel };
  }
  function cineSummary() {
    const on = (k) => $(k).checked;
    const parts = [];
    parts.push(`slow motion ${Math.round(parseFloat($('cine_slow').value) * 100)}% on ${$('cine_kills').value === 'all' ? 'every kill' : 'the final kill'}`);
    const trim = $('cine_trim').value; parts.push(trim === 'fast' ? `dead time fast-forwarded ${$('cine_fast').value}×${on('cine_skip_waits') ? ', long waits skipped' : ''}` : trim === 'cut' ? 'dead time cut out' : 'full length');
    const fx = [on('cine_impact') && 'kill punch', on('cine_look') && 'cinematic look', on('cine_fade') && 'fades'].filter(Boolean); if (fx.length) parts.push(fx.join(', '));
    const txt = [on('cine_title') && 'title card', on('cine_counter') && 'kill counter', on('cine_card') && 'player card'].filter(Boolean); if (txt.length) parts.push(txt.join(', '));
    const nMusic = $('cine_music').options.length - 1; parts.push(nMusic ? ($('cine_music').value ? `music: ${$('cine_music').value}` : 'music: random track') : 'no music yet');
    const pinned = Object.values(voicePins).reduce((a, l) => a + l.length, 0);
    parts.push(!on('cine_voice') ? 'casters off' : `real caster lines${pinned ? ` (${pinned} ticked)` : ' (random)'}`);
    $('cine-summary').innerHTML = parts.map(x => `<span class="pill">${esc(x)}</span>`).join('');
  }
  function fillCine(info) {
    const st = info.settings || {};
    for (const k of CINE_NUM) if (st[k] != null) { $(k).value = st[k]; }
    for (const k of CINE_STR) if (st[k] != null) $(k).value = st[k];
    for (const k of CINE_BOOL) if (st[k] != null) $(k).checked = !!st[k];
    document.querySelectorAll('#music-modal input[type=range]').forEach(cineRangeLabel);
    const fontOk = info.font !== false;
    $('cine-font-warn').style.display = fontOk ? 'none' : '';
    for (const k of ['cine_title', 'cine_counter', 'cine_card']) { $(k).disabled = !fontOk; $(k + '-opt').classList.toggle('off', !fontOk); }
    cinePaceVis();
    voiceVis();
    cineSummary();
  }
  $('cine-advanced-btn').onclick = () => {
    const adv = $('cine-advanced'); const open = adv.style.display === 'none';
    adv.style.display = open ? '' : 'none';
    $('cine-advanced-btn').innerHTML = open ? 'Hide the options' : `${ic('settings')} Edit cinematic options`;
    if (!open) cineSummary();
  };
  $('music-modal').addEventListener('change', cineSummary);
  function renderMusicList(files, dir) {
    $('music-dir').textContent = dir || '';
    $('music-list').innerHTML = files.length ? files.map(f => `<span class="pill" title="${esc(f)}"><span class="pill-name">${esc(f)}</span><button class="small ghost del-music" data-name="${esc(f)}" title="remove" aria-label="Remove ${esc(f)}">${ic('x')}</button></span>`).join('') : '<span class="muted">none yet — slow motion only</span>';
    const sel = $('cine_music'); const cur = sel.value;
    sel.innerHTML = '<option value="">random track</option>' + files.map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join('');
    sel.value = files.includes(cur) ? cur : '';
  }
  async function renderMusic() {
    try { const m = await API.music(); renderMusicList(m.files, m.dir); } catch (_) { /* ignore */ }
  }
  function renderVoice(v) {
    const slots = v?.slots || {};
    for (const slot of VOICE_SLOTS) {
      const files = slots[slot] || [];
      const label = (f) => { const i = f.indexOf('/'); return i < 0 ? esc(f) : `<b class="voice-tag">${esc(VOICE_SUB_LABEL[f.slice(0, i)] || f.slice(0, i))}</b> ${esc(f.slice(i + 1))}`; };
      voicePins[slot] = (voicePins[slot] || []).filter(n => files.includes(n));
      $('voice-list-' + slot).innerHTML = files.length ? files.map(f => `<span class="pill voice-pill ${voicePins[slot].includes(f) ? 'pinned' : ''}" data-slot="${slot}" data-name="${esc(f)}" title="${esc(f)} — tick to use only this line (and other ticked ones); none ticked = any line of the slot"><input type="checkbox" class="voice-use" ${voicePins[slot].includes(f) ? 'checked' : ''} aria-label="Use ${esc(f)}"><span class="pill-name">${label(f)}</span>
        <button class="small ghost voice-play" data-slot="${slot}" data-name="${esc(f)}" title="preview" aria-label="Play ${esc(f)}">${ic('play')}</button><button class="small ghost voice-del" data-slot="${slot}" data-name="${esc(f)}" title="delete" aria-label="Delete ${esc(f)}">${ic('x')}</button></span>`).join('') : '<span class="muted">none</span>';
    }
    $('voice-dir').textContent = v?.dir || '';
  }
  async function reloadVoice() {
    try { const v = await API.voice(); if (cineInfo) cineInfo.voice = v; renderVoice(v); } catch (err) { toast(esc(err.message)); }
  }
  document.querySelectorAll('#music-modal input[type=range]').forEach(el => el.addEventListener('input', () => cineRangeLabel(el)));
  $('cine_trim').addEventListener('change', cinePaceVis);
  const voiceVis = () => { const on = $('cine_voice').checked; $('voice-body').classList.toggle('off', !on); $('cine_voice_volume').disabled = !on; $('voice-off-hint').style.display = on ? 'none' : ''; };
  $('cine_voice').addEventListener('change', voiceVis);
  let cineFor = null;   // null = this match's clips; 'squad' = the dialog sets up the squad montage instead
  function setCineMode(mode) {
    cineFor = mode;
    $('cine-which-field').style.display = mode === 'squad' ? 'none' : '';
    $('cine-go').innerHTML = mode === 'squad' ? `${ic('users')} Create squad montage` : `${ic('sparkles')} Create cinematic`;
    document.querySelector('#music-modal .modal-head').firstChild.textContent = mode === 'squad' ? 'Squad montage: cinematic settings ' : 'Cinematic clips ';
  }
  $('btn-cine').onclick = async () => {
    setCineMode(null);
    $('cine-msg').textContent = '';
    $('music-modal').style.display = '';
    $('cine-advanced').style.display = 'none'; $('cine-advanced-btn').innerHTML = `${ic('settings')} Edit cinematic options`;
    cineCounts();
    try {
      cineInfo = await API.cineSettings();
      try { const pk = JSON.parse(cineInfo.settings?.cine_voice_pick || '{}'); voicePins = { start: pk.start || [], kill: pk.kill || [], final: pk.final || [] }; } catch (_) { voicePins = { start: [], kill: [], final: [] }; }
      renderMusicList(cineInfo.music || [], cineInfo.music_dir);
      fillCine(cineInfo);
      renderVoice(cineInfo.voice);
    } catch (e) { $('cine-msg').textContent = e.message; renderMusic(); }
  };
  $('music-close').onclick = () => $('music-modal').style.display = 'none';
  $('music-modal').addEventListener('click', async (e) => {
    if (e.target === $('music-modal')) $('music-modal').style.display = 'none';
    if (e.target.classList?.contains('voice-use')) e.preventDefault();   // the pill click below does the toggle
    const d = e.target.closest('.del-music'); if (d) { if (!confirm(`Remove the track "${d.dataset.name}"?`)) return; try { await API.deleteMusic(d.dataset.name); } catch (err) { toast(esc(err.message)); } renderMusic(); return; }
    const pl = e.target.closest('.voice-play'); if (pl) { new Audio(API.voiceUrl(pl.dataset.slot, pl.dataset.name)).play().catch(err => toast(esc(err.message))); return; }
    const vd = e.target.closest('.voice-del'); if (vd) { if (!confirm(`Delete the voice line "${vd.dataset.name}"?`)) return; try { await API.deleteVoice(vd.dataset.slot, vd.dataset.name); } catch (err) { toast(esc(err.message)); } reloadVoice(); return; }
    const vp = e.target.closest('.voice-pill'); if (vp) {
      const { slot, name } = vp.dataset; const arr = voicePins[slot] || (voicePins[slot] = []);
      const i = arr.indexOf(name); if (i < 0) arr.push(name); else arr.splice(i, 1);
      renderVoice(cineInfo?.voice); cineSummary();
    }
  });
  $('music-file').addEventListener('change', async () => {
    for (const f of $('music-file').files) { try { await API.uploadMusic(f); } catch (err) { toast(esc(err.message)); } }
    $('music-file').value = ''; renderMusic();
  });
  document.querySelectorAll('.voice-file').forEach(inp => inp.addEventListener('change', async () => {
    for (const f of inp.files) { try { await API.uploadVoice(inp.dataset.slot, f, $('voice-sub').value); } catch (err) { toast(esc(err.message)); } }
    inp.value = ''; reloadVoice();
  }));
  function collectCine() {
    const settings = {};
    for (const k of CINE_NUM) { const v = parseFloat($(k).value); if (!Number.isNaN(v)) settings[k] = v; }
    for (const k of CINE_STR) settings[k] = String($(k).value);
    for (const k of CINE_BOOL) settings[k] = !!$(k).checked;
    settings.cine_voice_pick = JSON.stringify(voicePins);
    return settings;
  }
  $('cine-go').onclick = async () => {
    if (cineFor === 'squad') {
      $('cine-go').disabled = true; $('cine-msg').textContent = 'starting…';
      try { await startSquad({ cinematic: true, settings: collectCine() }); $('music-modal').style.display = 'none'; $('cine-msg').textContent = ''; }
      catch (e) { $('cine-msg').textContent = e.message; }
      $('cine-go').disabled = false; return;
    }
    try {
      const which = $('cine-which-sel').checked ? 'sel' : 'all';
      const ids = which === 'sel' ? selectedHighlights().filter(h => videoFor(h)).map(h => h.id) : null;
      if (ids && !ids.length) { $('cine-msg').textContent = 'select at least one recorded highlight'; return; }
      if (!ids && !videos.videos.some(v => !v.cine)) { $('cine-msg').textContent = 'every clip already has a cinematic version'; return; }
      $('cine-go').disabled = true; $('cine-msg').textContent = 'starting…';
      const r = await API.cinematic(id, ids, collectCine());
      $('cine-go').disabled = false; $('cine-msg').textContent = '';
      $('music-modal').style.display = 'none';
      toast(r.queued ? `${ic('sparkles')} Added to the queue (#${r.queued}): it starts by itself when the clip being made now is done. You can open other matches meanwhile.`
        : `${ic('sparkles')} Working on your cinematic video… about a minute per clip. You can open other matches meanwhile; it keeps going.`, 6000);
      startCinePoll();
    } catch (e) { $('cine-go').disabled = false; $('cine-msg').textContent = e.message; }
  };
  // cinematic jobs run in the app, not the page: they go on when you open another match, and a job started
  // while another runs waits in a queue (any match). Every match page shows the progress.
  let cineSeen = null;   // {started, status} of the job last seen, to reload this match's clips when one of its jobs ends
  function startCinePoll() {
      if (cinePoll) clearInterval(cinePoll);
      const tick = async () => {
        const st = await API.recordStatus().catch(() => null); const c = st?.cinematic;
        if (!c) { clearInterval(cinePoll); cinePoll = null; return; }
        const queue = c.queued || [];
        const mine = queue.findIndex(q => q.demo_id === id);
        const here = c.demo_id === id;
        const wait = queue.length ? ` · ${queue.length} more waiting` : '';
        $('recstatus').classList.toggle('err', here && c.status === 'error');
        if (c.status === 'running') {
          const pct = Math.round(100 * (c.progress ?? (c.done / c.total)));
          const stage = c.stage === 'encoding' ? `encoding ${Math.round(100 * Math.max(0, ((c.clip_progress ?? 0) - 0.15) / 0.85))}%` : (c.stage || 'working');
          const eta = c.eta == null ? '' : c.eta < 5 ? ' · almost done' : c.eta < 60 ? ` · about ${Math.round(c.eta / 5) * 5} s left` : ` · about ${Math.ceil(c.eta / 60)} min left`;
          const what = here ? `${ic('sparkles')} Working on your cinematic video… ${c.total > 1 ? `clip ${c.done + 1}/${c.total} · ` : ''}${esc(stage)}${eta}`
            : `${ic('sparkles')} ${mine >= 0 ? `Your clips here are ${mine === 0 ? 'next' : `#${mine + 1} in line`}. Now making` : 'Making'} a cinematic clip from another match… ${esc(stage)}${eta}`;
          $('recstatus').innerHTML = `${what}${wait}
            <button class="small ghost" id="cine-cancel" title="stop the clip being made now; the queue goes on">${ic('x')} Cancel</button>${queue.length ? `<button class="small ghost" id="cine-cancel-all" title="stop it and empty the queue">${ic('x')} Cancel all</button>` : ''}
            <span class="progress live"><div style="width:${Math.max(2, pct)}%"></div></span>`;
          $('cine-cancel').onclick = async () => { try { await API.cineCancel(false); } catch (_) { /* ignore */ } };
          if ($('cine-cancel-all')) $('cine-cancel-all').onclick = async () => { try { await API.cineCancel(true); } catch (_) { /* ignore */ } };
        } else if (here) {
          $('recstatus').innerHTML = (c.status === 'error' ? `cinematic failed: ${esc(c.error)}` : `${ic('sparkles')} Cinematic done: ${c.total} clip(s)`) + wait;
        } else if (cineSeen) {
          $('recstatus').innerHTML = mine >= 0 ? `${ic('sparkles')} Your clips here are next in line${wait}` : '';
        }
        if (cineSeen && cineSeen.started === c.started && cineSeen.status === 'running' && c.status !== 'running' && here) {
          await loadVideos(); renderHighlights();
        }
        cineSeen = { started: c.started, status: c.status };
        if (c.status !== 'running' && !queue.length) { clearInterval(cinePoll); cinePoll = null; }
      };
      cinePoll = setInterval(tick, 1000); tick();
  }
  // ---- squad montage: every 3K+ of the squad in this match -> record what is missing -> cinematic -> one video ----
  let squad = [], squadPoll = null;
  function renderSquad() {
    $('squad-members').innerHTML = squad.map((m, i) => `<div class="squad-row" data-i="${i}">
      <input type="text" class="sq-name" value="${esc(m.name)}" placeholder="nickname" maxlength="40" aria-label="Nickname" autocomplete="off" spellcheck="false">
      <input type="text" class="sq-sid" value="${esc(m.steamid)}" placeholder="Steam64 ID (optional)" maxlength="20" inputmode="numeric" aria-label="Steam64 ID" autocomplete="off" spellcheck="false">
      <button class="small ghost sq-del" aria-label="Remove ${esc(m.name)}">${ic('x')}</button></div>`).join('') || '<span class="muted">nobody yet</span>';
  }
  const readSquad = () => [...document.querySelectorAll('#squad-members .squad-row')].map(r => ({ name: r.querySelector('.sq-name').value.trim(), steamid: r.querySelector('.sq-sid').value.trim() }));
  async function squadPreview() {
    try {
      await API.saveSquad(readSquad());
      const r = await API.squadPreview(id, $('squad-min').value);
      const n = r.highlights.length, rec = r.highlights.filter(h => h.recorded).length;
      $('squad-count').textContent = n ? `${n} play${n > 1 ? 's' : ''}${n - rec ? ` · ${n - rec} to record first` : ' · all recorded'}` : 'no plays like that in this match';
      $('squad-list').innerHTML = r.highlights.map(h => `<li>${esc(h.description)} ${h.cinematic ? '<span class="badge ok">cinematic</span>' : h.recorded ? '<span class="badge">recorded</span>' : '<span class="badge">to record</span>'}</li>`).join('');
      $('squad-go').disabled = !n; $('squad-settings').disabled = !n || !$('squad-cine').checked;
    } catch (e) { $('squad-count').textContent = e.message; }
  }
  $('btn-squad').onclick = async () => {
    $('squad-msg').textContent = ''; $('squad-modal').style.display = '';
    try { const s = await API.squad(); squad = s.members; $('squad-team').value = s.team || ''; $('squad-intro').checked = s.intro !== false; } catch (_) { squad = []; }
    renderSquad(); squadPreview();
  };
  $('squad-close').onclick = () => $('squad-modal').style.display = 'none';
  $('squad-modal').addEventListener('click', (e) => {
    if (e.target === $('squad-modal')) { $('squad-modal').style.display = 'none'; return; }
    const del = e.target.closest('.sq-del'); if (del) { squad = readSquad(); squad.splice(+del.closest('.squad-row').dataset.i, 1); renderSquad(); squadPreview(); }
  });
  $('squad-members').addEventListener('change', squadPreview);
  $('squad-min').addEventListener('change', squadPreview);
  $('squad-add').onclick = () => { squad = readSquad(); squad.push({ name: '', steamid: '' }); renderSquad(); $('squad-members').querySelector('.squad-row:last-child .sq-name').focus(); };
  ['squad-cine', 'squad-plain'].forEach(k => $(k).addEventListener('change', () => { $('squad-settings').disabled = !$('squad-cine').checked; }));
  $('squad-settings').onclick = async () => { await API.saveSquad(readSquad()).catch(() => {}); $('squad-modal').style.display = 'none'; await $('btn-cine').onclick(); setCineMode('squad'); };
  async function startSquad(extra) {
    const r = await API.squadReel(id, { min_kills: +$('squad-min').value, ...extra });
    toast(`${ic('users')} Making the squad montage: ${r.total} play${r.total > 1 ? 's' : ''}. Recording and the cinematic versions run first; progress shows above the highlights.`, 7000);
    startSquadPoll();
  }
  $('squad-go').onclick = async () => {
    $('squad-go').disabled = true; $('squad-msg').textContent = 'starting…';
    try { await API.saveSquad(readSquad()); await API.saveTeam($('squad-team').value, $('squad-intro').checked); await startSquad({ cinematic: $('squad-cine').checked }); $('squad-modal').style.display = 'none'; $('squad-msg').textContent = ''; }
    catch (e) { $('squad-msg').textContent = e.message; }
    $('squad-go').disabled = false;
  };
  const fmtEta = (s) => s == null ? '' : s < 10 ? ' · almost done' : s < 60 ? ` · about ${Math.round(s / 5) * 5} s left` : ` · about ${Math.ceil(s / 60)} min left`;
  function startSquadPoll() {
    if (squadPoll) clearInterval(squadPoll);
    let wasRunning = false;
    const tick = async () => {
      const s = await API.squadStatus().catch(() => null);
      if (!s || s.status === 'idle' || s.demo_id !== id) { $('squadstatus').innerHTML = ''; clearInterval(squadPoll); squadPoll = null; return; }
      const el = $('squadstatus'); el.classList.toggle('err', s.status === 'error');
      const who = s.picked ? `${ic('film')} Montage` : `${ic('users')} Squad montage`;
      if (s.status === 'running') {
        wasRunning = true;
        const step = { starting: 'starting', recording: 'recording the missing clips in CS2', cinematic: 'making the plays cinematic', joining: 'joining the plays and laying the song' }[s.stage] || s.stage;
        const pct = Math.round(100 * (s.progress || 0));
        el.innerHTML = `${who} (${s.total} plays): <b>${pct}%</b> · ${esc(step)}${s.detail ? ` · ${esc(s.detail)}` : ''}${fmtEta(s.eta)} <button class="small ghost" id="squad-cancel">${ic('stop')} Stop</button>
          <span class="progress live"><div style="width:${Math.max(2, pct)}%"></div></span>`;
        $('squad-cancel').onclick = async () => { await API.squadCancel().catch(() => {}); };
        if (s.stage === 'recording' && !recPoll) recPoll = setInterval(pollRecord, 2000);
        return;
      }
      clearInterval(squadPoll); squadPoll = null;
      if (s.status === 'error') { el.innerHTML = `${who} stopped: ${esc(s.error)}`; return; }
      const r = s.result || {};
      el.innerHTML = `${who} ready: <b>${esc(r.description || '')}</b>${r.seconds ? ` (${r.seconds}s)` : ''} <button class="small primary" id="squad-watch">${ic('play')} Watch</button>`
        + `${r.exported && !String(r.exported).startsWith('export failed') ? ` <span class="muted">saved to ${esc(r.exported)}</span>` : ''} <a class="small" href="/#highlights">open the Highlights tab</a>`;
      $('squad-watch').onclick = () => openVideo(r);
      await loadVideos(); renderHighlights();
      if (wasRunning) {   // it just finished while this page was open
        const t = toast(`${who} is ready: <b>${esc(r.description || '')}</b> <button class="small primary">${ic('play')} Watch now</button>`, 15000);
        t.querySelector('button').onclick = () => { t.remove(); openVideo(r); };
      }
    };
    squadPoll = setInterval(tick, 1500); tick();
  }
  $('btn-export').onclick = async () => {
    try { const r = await API.exportVideos(id); toast(`${r.count} file(s) saved to <b>${esc(r.folder)}</b>`, 6000); }
    catch (e) { toast(`<b>Export failed:</b> ${esc(e.message)}`); }
  };
  $('btn-install-tools').onclick = async () => {
    $('btn-install-tools').disabled = true;
    await API.installTools();
    const t = setInterval(async () => {
      tools = await API.recordTools();
      const inst = tools.installing;
      $('recstatus').textContent = inst ? `${inst.stage} ${Math.round((inst.progress || 0) * 100)}%` : '';
      if (tools.ready || (inst && inst.stage === 'error')) { clearInterval(t); $('btn-install-tools').style.display = tools.ready ? 'none' : ''; $('btn-install-tools').disabled = false; if (inst?.error) toast(esc(inst.error)); }
    }, 1500);
  };
  (async () => {
    try { tools = await API.recordTools(); } catch (_) { tools = null; }
    if (tools) {
      $('btn-install-tools').style.display = tools.windows && !tools.ready && !tools.missing_module ? '' : 'none';
      if (!tools.windows || tools.missing_module) $('btn-record-sel').title = tools.missing_module ? 'recorder module not installed' : 'recording only works on Windows';
    }
    await loadVideos();
    const st = await API.recordStatus().catch(() => null);
    if (st && (st.status === 'recording' || st.status === 'encoding' || st.status === 'preparing')) { recStatusText(st); recPoll = setInterval(pollRecord, 2000); }
    else if (st?.cinematic && (st.cinematic.status === 'running' || st.cinematic.queued?.length)) startCinePoll();
    API.squadStatus().then(s => { if (s.demo_id === id && s.status === 'running') startSquadPoll(); }).catch(() => {});
    renderHighlights();
  })();

  renderHighlights();
  replay._resize();
  replay.draw();
  renderScoreboard(replay.state(), replay.roundAt(replay.tick));
  if (!me) setTimeout(openWhoAmI, 400);
})();
