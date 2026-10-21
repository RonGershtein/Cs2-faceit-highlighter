(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  let demos = [];
  let config = {};
  let mapFilter = '';
  let selecting = false, mpicked = [];

  const fmtDate = (iso) => { if (!iso) return ''; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) + ' · ' + d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }); };
  const myScore = (d) => { if (!d.me) return null; const mine = d.me.team_start === 'CT' ? d.score_a : d.score_b; const theirs = d.me.team_start === 'CT' ? d.score_b : d.score_a; return { mine, theirs, result: mine > theirs ? 'win' : mine < theirs ? 'loss' : 'tie' }; };

  function renderStats() {
    const done = demos.filter(d => d.status === 'done');
    const withMe = done.filter(d => d.me);
    $('st-matches').textContent = done.length;
    $('st-matches-s').textContent = withMe.length ? `${withMe.length} with you` : (config.my_steamid ? '' : 'set your Steam ID in settings');
    if (withMe.length) {
      const wins = withMe.filter(d => myScore(d).result === 'win').length, ties = withMe.filter(d => myScore(d).result === 'tie').length;
      $('st-win').textContent = Math.round(100 * wins / withMe.length) + '%';
      $('st-win-s').textContent = `${wins}W · ${withMe.length - wins - ties}L${ties ? ` · ${ties}T` : ''}`;
      const avg = (f) => withMe.reduce((a, d) => a + f(d), 0) / withMe.length;
      $('st-rating').textContent = avg(d => d.me.rating).toFixed(2);
      $('st-rating-s').textContent = `ADR ${avg(d => d.me.adr).toFixed(0)} · KAST ${avg(d => d.me.kast).toFixed(0)}%`;
      const k = withMe.reduce((a, d) => a + d.me.kills, 0), dd = withMe.reduce((a, d) => a + d.me.deaths, 0);
      $('st-kd').textContent = (k / Math.max(1, dd)).toFixed(2);
      $('st-kd-s').textContent = `${k} kills · ${dd} deaths`;
    } else { for (const id of ['st-win', 'st-rating', 'st-kd']) $(id).textContent = '–'; for (const id of ['st-win-s', 'st-rating-s', 'st-kd-s']) $(id).textContent = ''; }
  }

  function renderMatches() {
    const maps = [...new Set(demos.map(d => d.map).filter(Boolean))].sort();
    const sel = $('map-filter');
    const cur = sel.value;
    sel.innerHTML = '<option value="">All maps</option>' + maps.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
    sel.value = maps.includes(cur) ? cur : '';
    mapFilter = sel.value;
    const list = demos.filter(d => !mapFilter || d.map === mapFilter);
    $('match-count').textContent = demos.length ? `${demos.length}` : '';
    $('matches').setAttribute('aria-busy', 'false');
    if (!list.length) {
      $('matches').innerHTML = mapFilter ? '<div class="card empty">No matches on this map yet.</div>' : `<div class="card empty start">
        <div class="start-title">No matches yet</div>
        <ol class="start-steps">
          <li><b>Drop a demo</b> from FACEIT above, or point the watch folder at your downloads in Settings.</li>
          <li><b>Open the match</b> to watch the 2D replay and see your highlights ranked.</li>
          <li><b>Record a highlight</b> as an MP4 with CS2 and make it cinematic: slow motion, music, caster voice lines.</li>
        </ol></div>`;
      return;
    }
    $('matches').innerHTML = list.map((d, i) => {
      const done = d.status === 'done';
      const ms = myScore(d);
      const me = d.me;
      let status = '';
      if (d.status === 'error') status = `<span class="badge err" title="${esc(d.error)}">error</span><span class="muted" style="font-size:12px">${esc((d.error || '').slice(0, 60))}</span>`;
      else if (!done) status = `<span class="progress" style="width:140px"><div style="width:${Math.round((d.progress || 0) * 100)}%"></div></span><span class="muted" style="font-size:12px">${esc(d.stage || d.status)}</span>`;
      const hs = me && me.kills ? Math.round(100 * me.headshots / me.kills) : 0;
      const pk = mpicked.indexOf(d.id);
      return `<div class="card match ${done ? '' : 'pending'} ${pk >= 0 ? 'mpicked' : ''}" data-id="${d.id}" data-done="${done ? 1 : 0}" style="--i:${Math.min(i, 9)}">
        ${done ? `<span class="mpick" aria-hidden="true">${ic('select')}</span>` : ''}
        ${done ? `<a class="link" href="/match.html?id=${esc(d.id)}" aria-label="Open ${esc(d.map || 'match')} ${fmtDate(d.date)}"></a>` : ''}
        ${d.map ? `<div class="radar" style="background-image:url('/api/radars/${esc(d.map)}.png')"></div>` : ''}
        <div class="body">
          <div class="head"><span class="map">${esc(d.map || 'parsing…')}</span><span class="spacer"></span><span class="date">${fmtDate(d.date)}</span></div>
          ${done ? `<div class="score"><span class="${ms && ms.result === 'win' ? '' : ''}">${ms ? ms.mine : d.score_a}</span><span class="sep">:</span><span>${ms ? ms.theirs : d.score_b}</span>${ms ? `<span class="pill ${ms.result}">${ms.result[0].toUpperCase() + ms.result.slice(1)}</span>` : ''}</div>
          <div class="teams"><span>${esc(ms ? (me.team_start === 'CT' ? d.team_a : d.team_b) : d.team_a)}</span><span class="vs">vs</span><span>${esc(ms ? (me.team_start === 'CT' ? d.team_b : d.team_a) : d.team_b)}</span></div>` : `<div class="teams">${esc(d.filename)}</div>`}
          ${me ? `<div class="me"><div><div class="k">K / D / A</div><div class="v">${me.kills} / ${me.deaths} / ${me.assists}</div></div><div><div class="k">ADR</div><div class="v">${me.adr}</div></div><div><div class="k">HS</div><div class="v">${hs}%</div></div><div><div class="k">Rating</div><div class="v">${me.rating}</div></div></div>` : ''}
          ${d.top_highlight ? `<div class="top"><b>${ic('star')}</b> ${esc(d.top_highlight.description)}</div>` : ''}
          ${status ? `<div class="foot">${status}</div>` : ''}
        </div>
        <button class="small ghost del" data-id="${d.id}" title="delete demo" aria-label="Remove this match">${ic('x')}</button>
      </div>`;
    }).join('');
  }

  let shown = null;   // the match list as last drawn: the 4 s poll redraws only when something changed
  async function refresh(force = false) {
    try {
      const list = await API.listDemos();
      const key = JSON.stringify(list);
      if (!force && key === shown) return;
      shown = key; demos = list; renderStats(); renderMatches();
    } catch (e) { shown = null; $('matches').innerHTML = `<div class="card empty">failed to load: ${esc(e.message)}</div>`; }
  }
  $('map-filter').addEventListener('change', renderMatches);
  $('matches').addEventListener('click', async (ev) => {
    const del = ev.target.closest('.del');
    if (del) { ev.stopPropagation(); if (!confirm('Remove this match from the app?\n\nDeletes the app\'s copy of the demo and the parsed data. Your original download is kept, and the recorded clips stay in the Highlights tab.')) return; await API.deleteDemo(del.dataset.id); refresh(); return; }
    const card = ev.target.closest('.match');
    if (selecting) {                                    // choosing matches for the squad montage
      ev.preventDefault();
      if (card && card.dataset.done === '1') { const i = mpicked.indexOf(card.dataset.id); if (i >= 0) mpicked.splice(i, 1); else mpicked.push(card.dataset.id); markMatches(); renderBar(); }
      return;
    }
    if (ev.target.closest('a.link')) return;            // the real link handles navigation (Cmd/Ctrl+click works)
    if (card && card.dataset.done === '1') location.href = `/match.html?id=${card.dataset.id}`;
  });
  // ---- first screen: the latest montage, large, playing muted --------------------------------------------
  const fmtLen = (s) => { s = Math.round(+s || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const KIND = { montage: 'Montage', long: 'Long video', cinematic: 'Cinematic', clip: 'Original' };
  let showcased = null;
  async function renderShowcase(clips) {
    const newest = (kinds) => clips.filter(c => kinds.includes(c.kind)).sort((a, b) => b.created - a.created)[0];
    const c = newest(['montage', 'long']) || newest(['cinematic']) || newest(['clip']);
    try { const s = await API.squad(); $('sc-title').textContent = s.team || 'Your highlights'; } catch (_) { /* keep the default */ }
    const key = c ? `${c.demo_id}/${c.file}` : '';
    if (key === showcased) return;
    showcased = key;
    const scr = $('sc-screen');
    if (!c) {
      scr.className = 'screen empty-screen'; scr.innerHTML = '<span class="muted">Your montages play here</span>';
      $('sc-watch').hidden = true; return;
    }
    const where = [c.map || (c.maps || []).join(' · '), c.date ? fmtDate(c.date).split(' · ')[0] : ''].filter(Boolean).join(' · ');
    $('sc-lead').innerHTML = `<b>${esc(c.description)}</b><br>${esc([fmtLen(c.seconds), where].filter(Boolean).join(' · '))}`;
    scr.className = 'screen';
    scr.innerHTML = `<video src="${API.videoUrl(c.demo_id, c.file)}#t=1" muted autoplay loop playsinline preload="metadata" tabindex="-1"></video>
      <span class="screen-tag">${esc(KIND[c.kind] || c.kind)}</span><span class="screen-play">${ic('play')} Watch with sound</span>`;
    const open = () => window.openClip && window.openClip(c);
    scr.onclick = open; scr.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
    $('sc-watch').hidden = false; $('sc-watch').onclick = open;
  }
  let lastClips = [];   // a 'clips' event without a list (the Squad tab saved) only refreshes the team name
  document.addEventListener('clips', (e) => { if (e.detail) lastClips = e.detail; renderShowcase(lastClips); });
  $('sc-new').onclick = () => {
    window.showTab && window.showTab('matches');
    if (!selecting) $('match-select').click();
    $('view-matches').scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  $('sc-recap').onclick = () => $('recap-open').click();

  // ---- squad montage of several matches ---------------------------------------------------------------
  // ticking only updates the card badges: re-rendering would replay the cards' entrance animation
  function markMatches() {
    document.querySelectorAll('#matches .match[data-id]').forEach(el => {
      const on = mpicked.includes(el.dataset.id);
      el.classList.toggle('mpicked', on);
    });
  }
  function renderBar() {
    mpicked = mpicked.filter(id => demos.some(d => d.id === id && d.status === 'done'));
    $('match-bar').hidden = !selecting;
    $('matches').classList.toggle('selecting', selecting);
    $('match-select').classList.toggle('active', selecting);
    $('match-select').innerHTML = selecting ? 'Done' : `${ic('select')} Select matches`;
    $('match-bar-n').textContent = mpicked.length ? `${mpicked.length} match${mpicked.length > 1 ? 'es' : ''} selected` : 'Click the matches to put in the montage';
    $('match-go').disabled = !mpicked.length;
  }
  $('match-select').onclick = () => { selecting = !selecting; if (!selecting) mpicked = []; markMatches(); renderBar(); };
  $('match-all').onclick = () => { mpicked = demos.filter(d => d.status === 'done' && (!mapFilter || d.map === mapFilter)).map(d => d.id); markMatches(); renderBar(); };
  $('match-none').onclick = () => { mpicked = []; markMatches(); renderBar(); };
  let multiSeq = 0;
  async function multiPreview() {
    const seq = ++multiSeq;
    $('multi-list').innerHTML = '<div class="sq-empty">Looking for your squad\'s plays…</div>';
    $('multi-sum').textContent = ''; $('multi-rec').hidden = true;
    try {
      const r = await API.multiPreview(mpicked, +$('multi-min').value);
      if (seq !== multiSeq) return;
      const ms = r.matches;
      const plays = ms.reduce((a, m) => a + m.ids.length, 0), need = ms.reduce((a, m) => a + m.need.length, 0);
      const toRec = ms.filter(m => m.need.length).length, nm = ms.filter(m => m.ids.length).length;
      const mapName = (m) => (m || 'match').replace(/^de_/, '').replace(/^./, c => c.toUpperCase());
      $('multi-list').innerHTML = ms.map(m => `<div class="sq-match${m.ids.length ? '' : ' none'}"><span class="map">${esc(mapName(m.map))}<span>${esc(fmtDate(m.date).split(' · ')[0].replace(/ \d{4}$/, ''))}</span></span>`
        + `<span class="rec">${m.need.length ? `${m.need.length} new` : ''}</span>`
        + `<span class="n">${m.ids.length ? `${m.ids.length} play${m.ids.length > 1 ? 's' : ''}` : 'no plays'}</span></div>`).join('')
        || '<div class="sq-empty">No matches selected</div>';
      $('multi-sum').textContent = plays ? `${plays} play${plays > 1 ? 's' : ''} from ${nm} match${nm > 1 ? 'es' : ''}${need ? '' : ' · all recorded'}` : '';
      $('multi-rec').hidden = !need;
      $('multi-rec').textContent = need ? `CS2 opens once to record ${need} play${need > 1 ? 's' : ''}${toRec > 1 ? ` from ${toRec} matches` : ''} first, then closes by itself.` : '';
      $('multi-go').disabled = !plays;
      $('multi-name').placeholder = 'Squad montage';
    } catch (e) { $('multi-list').innerHTML = `<div class="sq-empty">${esc(e.message)}</div>`; $('multi-go').disabled = true; }
  }
  async function loadMultiMusic() {
    const sel = $('multi-music'), cur = sel.value;
    try {
      const m = await API.music();
      sel.innerHTML = '<option value="">Random: a song you have not heard lately</option>'
        + m.files.map(f => `<option value="${esc(f)}">${esc(f.replace(/\.[^.]+$/, ''))}</option>`).join('');
      sel.value = m.files.includes(cur) ? cur : '';
      if (!m.files.length) sel.innerHTML = '<option value="">No music yet: add tracks in a match\'s Cinematic dialog</option>';
    } catch (_) { /* keep the list */ }
  }
  const syncMultiStyle = () => { $('multi-cine-opts').hidden = !$('multi-cine').checked; };
  const ORDER_HINT = { song: 'Kills land on the song\'s drops, the biggest plays on the biggest drops.', date: 'The plays in the order they happened, oldest match first.' };
  // a row of buttons that works like radio buttons and keeps its value in a hidden input
  function segment(id, input, onChange) {
    const seg = $(id);
    const set = (v) => {
      $(input).value = v;
      seg.querySelectorAll('button').forEach(b => { const on = b.dataset.v === v; b.classList.toggle('active', on); b.setAttribute('aria-checked', on); });
      onChange && onChange(v);
    };
    seg.addEventListener('click', (e) => { const b = e.target.closest('button[data-v]'); if (b) set(b.dataset.v); });
    return set;
  }
  segment('multi-min-seg', 'multi-min', () => multiPreview());
  const setOrder = segment('multi-order-seg', 'multi-order', (v) => { $('multi-order-hint').textContent = ORDER_HINT[v]; });
  setOrder('song');
  const SLOW_HINT = { song: 'Slow motion only where a kill lands on a drop of the song.', all: 'Every kill in slow motion.', none: 'Everything at normal speed.' };
  const setSlow = segment('multi-slow-seg', 'multi-slow', (v) => { $('multi-slow-hint').textContent = SLOW_HINT[v]; });
  setSlow('song');
  async function loadMontageLook() {   // the choices made last time
    try {
      const c = await API.getConfig();
      setSlow(c.montage_slow || 'song');
      $('multi-grade').value = c.cine_look === false ? 'off' : (c.cine_grade || 'teal_orange');
      $('multi-trans').value = c.montage_transition || 'mixed';
      $('multi-outro').checked = c.montage_outro !== false;
      $('multi-swoop').checked = c.montage_swoop !== false;
    } catch (_) { /* defaults */ }
  }
  function montageLook() {
    const g = $('multi-grade').value;
    return { montage_slow: $('multi-slow').value, montage_transition: $('multi-trans').value, montage_outro: $('multi-outro').checked,
      montage_swoop: $('multi-swoop').checked, cine_look: g !== 'off', ...(g !== 'off' ? { cine_grade: g } : {}) };
  }
  $('multi-cine').addEventListener('change', syncMultiStyle);
  $('multi-plain').addEventListener('change', syncMultiStyle);
  $('match-go').onclick = async () => {
    $('multi-msg').textContent = ''; $('multi-modal').style.display = '';
    loadMultiMusic(); syncMultiStyle(); loadMontageLook();
    try { const s = await API.squad(); $('multi-team').value = s.team || ''; $('multi-intro').checked = s.intro !== false; } catch (_) { /* defaults */ }
    multiPreview();
  };
  $('multi-close').onclick = () => $('multi-modal').style.display = 'none';
  $('multi-modal').addEventListener('click', (e) => { if (e.target === $('multi-modal')) $('multi-modal').style.display = 'none'; });
  $('multi-go').onclick = async () => {
    $('multi-go').disabled = true; $('multi-msg').textContent = 'starting…';
    try {
      await API.saveTeam($('multi-team').value, $('multi-intro').checked);
      if ($('multi-cine').checked) await API.setConfig(montageLook());   // kept as the defaults for next time
      await API.multiSquad({ demo_ids: mpicked, min_kills: +$('multi-min').value, title: $('multi-name').value, cinematic: $('multi-cine').checked, music: $('multi-cine').checked ? $('multi-music').value : '', order: $('multi-order').value });
      $('multi-modal').style.display = 'none'; $('multi-msg').textContent = ''; $('multi-name').value = '';
      selecting = false; mpicked = []; renderMatches(); renderBar();
      window.startMontagePoll && window.startMontagePoll();
    } catch (e) { $('multi-msg').textContent = e.message; $('multi-go').disabled = false; }
  };

  // spotlight border: the card's glow follows the pointer (fine pointers only)
  if (matchMedia('(hover: hover) and (pointer: fine)').matches) {
    document.addEventListener('pointermove', (e) => {
      const c = e.target.closest?.('.card'); if (!c) return;
      const r = c.getBoundingClientRect();
      c.style.setProperty('--mx', `${((e.clientX - r.left) / r.width * 100).toFixed(1)}%`);
      c.style.setProperty('--my', `${((e.clientY - r.top) / r.height * 100).toFixed(1)}%`);
    }, { passive: true });
  }

  // ---- uploads --------------------------------------------------------------
  const drop = $('drop'), fileInput = $('file');
  drop.addEventListener('click', (e) => { if (!e.target.closest('.uploads') && !e.target.closest('a')) fileInput.click(); });
  $('pick').addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); fileInput.click(); });
  fileInput.addEventListener('change', () => { uploadFiles([...fileInput.files]); fileInput.value = ''; });
  ['dragenter', 'dragover'].forEach(n => drop.addEventListener(n, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(n => drop.addEventListener(n, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => uploadFiles([...e.dataTransfer.files]));
  function uploadFiles(files) {
    for (const f of files) {
      if (!/\.dem(\.zst|\.gz|\.bz2)?$/i.test(f.name)) { alert(`${f.name}: not a .dem / .dem.zst file`); continue; }
      const el = document.createElement('div'); el.className = 'upload';
      el.innerHTML = `<div class="row"><span>${esc(f.name)}</span><span class="muted st">uploading… ${(f.size / 1e6).toFixed(0)} MB</span></div><span class="progress"><div style="width:0%"></div></span>`;
      $('uploads').appendChild(el);
      const bar = el.querySelector('.progress > div'), st = el.querySelector('.st');
      API.upload(f, p => { bar.style.width = Math.round(p * 100) + '%'; st.textContent = `uploading… ${Math.round(p * 100)}%`; })
        .then(() => { st.textContent = 'queued for parsing'; setTimeout(() => el.remove(), 2500); refresh(); })
        .catch(err => { st.textContent = 'failed: ' + err.message; });
    }
  }

  // ---- settings -----------------------------------------------------------------
  const modal = $('settings-modal');
  $('btn-settings').onclick = () => {
    modal.style.display = '';
    $('cfg-steamid').value = config.my_steamid || ''; $('cfg-watch').value = config.watch_folder || ''; $('cfg-cs2').value = config.cs2_path || '';
    $('cfg-export').value = config.export_folder || ''; $('cfg-export').placeholder = config.default_export_folder || '';
    $('cfg-mail').value = config.share_email || ''; $('cfg-mailpw').value = '';
    $('cfg-enc').value = config.record_encoder || 'auto';
    $('cfg-enc-hint').textContent = config.auto_encoder ? `Auto uses ${config.auto_encoder} on this PC (tested).` : 'Auto tests the graphics card\'s encoder the first time you record; the CPU is used when it does not work.';
    $('cfg-mailpw').placeholder = config.share_password_set ? 'app password saved (type to change)' : 'Gmail app password';
    renderStorage(); renderSongs();
  };
  // ---- settings: music library (drop songs in; drops, beats and loud parts are measured when a video is made) ----
  const MUSIC_RE = /\.(mp3|m4a|aac|wav|ogg|flac|opus)$/i;
  const muDrop = $('mu-drop'), muFile = $('mu-file');
  window.renderSongs = () => renderSongs();
  async function renderSongs() {
    try {
      const m = await API.music();
      $('mu-count').textContent = m.files.length ? `${m.files.length} song${m.files.length === 1 ? '' : 's'}` : '';
      const rate = await API.musicRatings().catch(() => ({}));
      const meter = (p) => p == null ? '' : `<i class="punch" title="hits ${p >= 0.6 ? 'hard' : p >= 0.35 ? 'medium' : 'softly'}: ${p >= 0.6 ? 'often opens a montage' : p >= 0.35 ? 'picked now and then' : 'rarely picked when no song is chosen'}">${[0.15, 0.35, 0.6].map(x => `<b class="${p >= x ? 'on' : ''}"></b>`).join('')}</i>`;
      $('mu-list').innerHTML = m.files.length ? m.files.map(f => `<span class="pill${rate[f] && rate[f].random === false ? ' off' : ''}" title="${esc(f)}${rate[f] && rate[f].random === false ? ' (only when picked)' : ''}">${meter(rate[f] && rate[f].punch)}<button class="pill-name song-open" data-song="${esc(f)}" title="See and edit the drops of ${esc(f)}">${esc(f.replace(/\.[^.]+$/, ''))}</button><button class="small ghost" data-del-song="${esc(f)}" title="remove" aria-label="Remove ${esc(f)}">${ic('x')}</button></span>`).join('')
        : '<span class="muted">No songs yet: cinematic videos get slow motion only.</span>';
    } catch (e) { $('mu-list').innerHTML = `<span class="muted">${esc(e.message)}</span>`; }
  }
  async function addSongs(files) {
    for (const f of files) {
      const el = document.createElement('div'); el.className = 'upload';
      el.innerHTML = `<div class="row"><span>${esc(f.name)}</span><span class="muted st"></span></div>`;
      $('mu-uploads').appendChild(el);
      const st = el.querySelector('.st');
      if (!MUSIC_RE.test(f.name)) { st.textContent = 'not an audio file (mp3, m4a, aac, wav, ogg, flac, opus)'; setTimeout(() => el.remove(), 4000); continue; }
      st.textContent = 'adding…';
      try { await API.uploadMusic(f); el.remove(); } catch (err) { st.textContent = 'failed: ' + err.message; }
    }
    renderSongs(); loadMultiMusic();
  }
  muDrop.addEventListener('click', (e) => { if (!e.target.closest('.uploads')) muFile.click(); });
  muDrop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); muFile.click(); } });
  muFile.addEventListener('change', () => { addSongs([...muFile.files]); muFile.value = ''; });
  ['dragenter', 'dragover'].forEach(n => muDrop.addEventListener(n, e => { e.preventDefault(); muDrop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(n => muDrop.addEventListener(n, e => { e.preventDefault(); muDrop.classList.remove('over'); }));
  muDrop.addEventListener('drop', e => addSongs([...e.dataTransfer.files]));
  // a song dropped next to the zone must not make the browser leave the app to play it
  ['dragover', 'drop'].forEach(n => modal.addEventListener(n, e => e.preventDefault()));
  $('mu-list').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-del-song]'); if (!b) return;
    if (!confirm(`Remove "${b.dataset.delSong}" from the music library?`)) return;
    try { await API.deleteMusic(b.dataset.delSong); renderSongs(); loadMultiMusic(); } catch (err) { $('mu-count').textContent = err.message; }
  });
  modal.addEventListener('click', async (e) => {
    const b = e.target.closest('button.browse'); if (!b) return;
    const input = $(b.dataset.for);
    b.disabled = true; b.textContent = 'choosing…';
    try {
      const r = await API.pickFolder(input.value || input.placeholder || '', b.dataset.title);
      if (r.path) input.value = r.path;
    } catch (err) { $('cfg-msg').textContent = err.message; }
    b.disabled = false; b.textContent = 'Browse…';
  });
  // ---- settings: where the app keeps files, and clearing what loses nothing ------------------------------------
  const fmtBytes = (b) => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${Math.round(b / 1e6)} MB` : b > 0 ? `${Math.max(1, Math.round(b / 1e3))} KB` : 'empty';
  async function renderStorage() {
    try {
      const r = await API.storage();
      $('st-total').textContent = `${fmtBytes(r.total)} in all`;
      $('st-msg').textContent = r.busy ? `Clearing waits: ${r.busy}.` : '';
      $('st-list').innerHTML = r.items.map(i => `<div class="st-row${i.bytes ? '' : ' is-empty'}"><span class="lab">${esc(i.label)}</span><span class="size">${fmtBytes(i.bytes)}</span>`
        + (i.clear ? `<button class="small" data-clear="${esc(i.id)}" data-label="${esc(i.label)}" data-size="${fmtBytes(i.bytes)}" ${!i.bytes || r.busy ? 'disabled' : ''}>Clear</button>` : '<span></span>')
        + `<span class="note">${esc(i.note)}</span><span class="path" title="${esc(i.path)}">${esc(i.path)}</span></div>`).join('');
    } catch (e) { $('st-list').innerHTML = `<div class="sq-empty">${esc(e.message)}</div>`; }
  }
  $('st-list').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-clear]'); if (!b) return;
    if (!confirm(`Clear "${b.dataset.label}" (${b.dataset.size})?`)) return;
    b.disabled = true; b.textContent = 'Clearing…';
    try {
      const r = await API.clearStorage(b.dataset.clear);
      await renderStorage();
      $('st-msg').textContent = `Freed ${fmtBytes(r.freed)} (${r.files} file${r.files === 1 ? '' : 's'}).`;
    } catch (err) { $('st-msg').textContent = err.message; b.disabled = false; b.textContent = 'Clear'; }
  });
  $('settings-close').onclick = () => modal.style.display = 'none';
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.style.display = 'none'; });
  $('cfg-save').onclick = async () => {
    try {
      config = { ...config, ...(await API.setConfig({ my_steamid: $('cfg-steamid').value.trim(), watch_folder: $('cfg-watch').value.trim(), cs2_path: $('cfg-cs2').value.trim(), export_folder: $('cfg-export').value.trim(), share_email: $('cfg-mail').value.trim(), share_app_password: $('cfg-mailpw').value.trim(), record_encoder: $('cfg-enc').value })) };
      $('cfg-msg').textContent = 'saved'; setTimeout(() => { $('cfg-msg').textContent = ''; modal.style.display = 'none'; }, 700);
      refresh(true);   // "mine" stats depend on the config
    } catch (e) { $('cfg-msg').textContent = 'failed: ' + e.message; }
  };
  API.getConfig().then(c => { config = c; $('watch-status').textContent = c.watch_folder ? `watching ${c.watch_folder}` : ''; refresh(); }).catch(() => refresh());
  setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, 4000);
})();
