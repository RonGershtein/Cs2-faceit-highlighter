// Settings → Music: the song editor (the waveform with its beats, bars and drops; drops can be moved, added and
// removed, and are saved per song) and the sound-effect slots (built-in or a file of your own).
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmt = (t) => { t = Math.max(0, t || 0); return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`; };
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ---- the song editor ---------------------------------------------------------------------------------------
  const modal = $('song-modal'), canvas = $('song-wave'), audio = $('song-audio'), tip = $('song-tip');
  let song = null;          // the analysis from the server
  let drops = [];           // the drops being edited (seconds)
  let saved = [];           // what the server has
  let drag = null;          // {i, moved, x0}
  let hover = null;         // seconds under the pointer
  let raf = 0;

  const dirty = () => drops.length !== saved.length || drops.some((d, i) => Math.abs(d - saved[i]) > 0.001);
  function setMsg(t) { $('song-msg').textContent = t || ''; }
  function refreshButtons() {
    $('song-save').disabled = !dirty();
    $('song-reset').disabled = !song || (!song.edited && !dirty());
    $('song-state').textContent = song ? (dirty() ? 'not saved' : song.edited ? 'your drops' : 'detected') : '';
  }
  function meta() {
    if (!song) return;
    const used = (song.pieces || []).reduce((a, [x, y]) => a + (y - x), 0);
    $('song-meta').innerHTML = `<b>${song.bpm ? Math.round(song.bpm) : '?'} BPM</b> · ${drops.length} drop${drops.length === 1 ? '' : 's'}`
      + ` · ${fmt(song.duration)} long, ${fmt(used)} of it played in montages`
      + (Math.abs(song.gain_db) >= 0.5 ? ` · ${song.gain_db > 0 ? 'turned up' : 'turned down'} ${Math.abs(song.gain_db).toFixed(1)} dB to match the other songs` : '');
    $('song-random').checked = song.random !== false;
    const p = song.punch;
    $('song-punch').textContent = p == null ? '' : `(hits ${p >= 0.6 ? 'hard: often opens a montage' : p >= 0.35 ? 'medium: picked now and then' : 'softly: rarely picked'})`;
  }
  $('song-random').addEventListener('change', async () => {
    if (!song) return;
    try {
      const r = await API.setRandom(song.name, $('song-random').checked);
      song.random = r.random;
      setMsg(r.random ? 'It can be picked when no song is chosen.' : 'Only used when you choose it.');
      if (typeof window.renderSongs === 'function') window.renderSongs();
    } catch (e) { setMsg(e.message); $('song-random').checked = song.random !== false; }
  });

  async function open(name) {
    modal.style.display = '';
    $('song-title').textContent = name.replace(/\.[^.]+$/, '');
    $('song-meta').textContent = 'Finding the beats and drops…';
    song = null; drops = []; saved = []; setMsg(''); refreshButtons();
    audio.pause(); audio.src = API.songUrl(name); audio.load();
    size(); draw();
    try {
      song = await API.songAnalysis(name);
      drops = [...song.drops]; saved = [...song.drops];
      meta(); refreshButtons(); size(); draw();
    } catch (e) { $('song-meta').textContent = e.message; }
  }
  function close() {
    if (dirty() && !confirm('Leave without saving the drops?')) return;
    audio.pause(); audio.removeAttribute('src'); audio.load();
    modal.style.display = 'none'; cancelAnimationFrame(raf);
  }
  $('song-close').onclick = close;
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  document.addEventListener('keydown', (e) => {
    if (modal.style.display === 'none') return;
    if (e.key === 'Escape') { e.stopPropagation(); close(); }
    else if (e.key === ' ' && document.activeElement === canvas) { e.preventDefault(); toggle(); }
  });

  // geometry
  const PAD = 10;
  function size() {
    const r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(r.width * dpr)); canvas.height = Math.max(1, Math.round(r.height * dpr));
  }
  window.addEventListener('resize', () => { if (modal.style.display !== 'none') { size(); draw(); } });
  const W = () => canvas.width, H = () => canvas.height;
  const dur = () => (song && song.duration) || audio.duration || 1;
  const xOf = (t) => PAD + (W() - 2 * PAD) * (t / dur());
  const tOf = (px) => Math.min(dur(), Math.max(0, (px - PAD) / (W() - 2 * PAD) * dur()));
  const pxOfEvent = (e) => { const r = canvas.getBoundingClientRect(); return (e.clientX - r.left) * (canvas.width / r.width); };
  function snap(t, free) {
    if (free || !song || !song.beats || !song.beats.length) return Math.round(t * 1000) / 1000;
    let best = song.beats[0];
    for (const b of song.beats) if (Math.abs(b - t) < Math.abs(best - t)) best = b;
    return best;
  }
  function dropAt(px) {
    const dpr = window.devicePixelRatio || 1;
    let hit = -1, bd = 9 * dpr;
    drops.forEach((d, i) => { const dd = Math.abs(xOf(d) - px); if (dd < bd) { bd = dd; hit = i; } });
    return hit;
  }

  function draw() {
    const g = canvas.getContext('2d'), w = W(), h = H(), dpr = window.devicePixelRatio || 1;
    g.clearRect(0, 0, w, h);
    g.fillStyle = css('--bg-2'); g.fillRect(0, 0, w, h);
    if (!song) return;
    const top = 22 * dpr, base = h - 16 * dpr, mid = top + (base - top) / 2, amp = (base - top) / 2;
    // the parts a montage plays
    g.fillStyle = css('--bg-4');
    for (const [a, b] of song.pieces || []) g.fillRect(xOf(a), top, xOf(b) - xOf(a), base - top);
    // bars
    g.fillStyle = css('--bg-5');
    for (const d of song.downbeats || []) g.fillRect(Math.round(xOf(d)), base, Math.max(1, dpr), 6 * dpr);
    // waveform
    const wave = song.wave || [], step = song.wave_step || 0.1;
    g.fillStyle = css('--text-2');
    const cols = Math.max(1, Math.floor((w - 2 * PAD) / dpr / 2));
    for (let c = 0; c < cols; c++) {
      const t0 = c / cols * dur(), t1 = (c + 1) / cols * dur();
      let v = 0;
      for (let k = Math.floor(t0 / step); k <= Math.min(wave.length - 1, Math.floor(t1 / step)); k++) v = Math.max(v, wave[k] || 0);
      const x = PAD + c * 2 * dpr, hh = Math.max(dpr, v * amp);
      g.fillRect(x, mid - hh, Math.max(1, dpr), hh * 2);
    }
    // the detected drops (dashed) when the drops are the user's own or being edited
    const accent = css('--accent');
    if (song.edited || dirty()) {
      g.strokeStyle = css('--muted'); g.lineWidth = dpr; g.setLineDash([3 * dpr, 4 * dpr]);
      for (const d of song.auto_drops || []) { g.beginPath(); g.moveTo(xOf(d) + 0.5, top); g.lineTo(xOf(d) + 0.5, base); g.stroke(); }
      g.setLineDash([]);
    }
    // drops
    drops.forEach((d, i) => {
      const x = Math.round(xOf(d)), on = drag && drag.i === i;
      g.fillStyle = accent; g.globalAlpha = on ? 1 : 0.9;
      g.fillRect(x - dpr, top, 2 * dpr, base - top);
      g.beginPath(); g.moveTo(x - 7 * dpr, 4 * dpr); g.lineTo(x + 7 * dpr, 4 * dpr); g.lineTo(x, top - 2 * dpr); g.closePath(); g.fill();
      g.globalAlpha = 1;
    });
    // hover and playhead
    if (hover != null && !drag) {
      g.fillStyle = css('--text'); g.globalAlpha = 0.35; g.fillRect(Math.round(xOf(hover)), top, dpr, base - top); g.globalAlpha = 1;
    }
    if (audio.src && !isNaN(audio.currentTime)) {
      g.fillStyle = css('--text'); g.fillRect(Math.round(xOf(audio.currentTime)), 0, 2 * dpr, h);
    }
  }
  function loop() { draw(); $('song-time').textContent = `${fmt(audio.currentTime)} / ${fmt(dur())}`; if (!audio.paused) raf = requestAnimationFrame(loop); }
  function playFrom(t) { audio.currentTime = Math.max(0, t); audio.play().catch(() => {}); }
  function toggle() { if (audio.paused) audio.play().catch(() => {}); else audio.pause(); }
  audio.addEventListener('play', () => { $('song-play').querySelector('span').textContent = 'Pause'; $('song-play').querySelector('use').setAttribute('href', '#i-pause'); cancelAnimationFrame(raf); raf = requestAnimationFrame(loop); });
  audio.addEventListener('pause', () => { $('song-play').querySelector('span').textContent = 'Play'; $('song-play').querySelector('use').setAttribute('href', '#i-play'); loop(); });
  audio.addEventListener('loadedmetadata', () => loop());
  $('song-play').onclick = toggle;

  canvas.addEventListener('pointerdown', (e) => {
    if (!song || e.button !== 0) return;
    const px = pxOfEvent(e), i = dropAt(px);
    canvas.setPointerCapture(e.pointerId);
    drag = i >= 0 ? { i, moved: false, x0: px } : { i: -1, moved: false, x0: px };
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!song) return;
    const px = pxOfEvent(e);
    hover = tOf(px);
    canvas.style.cursor = drag && drag.i >= 0 ? 'grabbing' : dropAt(px) >= 0 ? 'grab' : 'pointer';
    if (drag && drag.i >= 0 && (drag.moved || Math.abs(px - drag.x0) > 4)) {
      drag.moved = true;
      drops[drag.i] = snap(tOf(px), e.altKey);
      refreshButtons();
    }
    const shown = drag && drag.i >= 0 && drag.moved ? drops[drag.i] : hover;
    tip.hidden = false; tip.textContent = fmt(shown) + (drag && drag.moved ? '' : '.' + String(Math.floor((shown % 1) * 10)));
    const r = canvas.getBoundingClientRect();
    tip.style.left = `${Math.min(r.width - 40, Math.max(0, e.clientX - r.left - 18))}px`;
    draw();
  });
  canvas.addEventListener('pointerleave', () => { hover = null; tip.hidden = true; draw(); });
  canvas.addEventListener('pointerup', (e) => {
    if (!song || !drag) return;
    const d = drag; drag = null;
    if (d.i >= 0 && d.moved) {
      drops.sort((a, b) => a - b); drops = drops.filter((x, k) => k === 0 || Math.abs(x - drops[k - 1]) > 0.05);
      meta(); refreshButtons(); draw(); return;
    }
    // a double click adds a drop (told apart here: the pointer capture keeps the browser's dblclick from firing)
    const px = pxOfEvent(e), now = performance.now();
    if (lastTap && now - lastTap.at < 400 && Math.abs(px - lastTap.px) < 8 * (window.devicePixelRatio || 1)) {
      lastTap = null;
      addDrop(snap(tOf(px), e.altKey));
      return;
    }
    lastTap = { at: now, px };
    if (d.i >= 0) playFrom(drops[d.i] - 4);     // the build-up into it
    else playFrom(tOf(px));
    draw();
  });
  let lastTap = null;
  function addDrop(t) {
    if (drops.some(d => Math.abs(d - t) < 0.05)) return;
    drops = [...drops, t].sort((a, b) => a - b);
    meta(); refreshButtons(); draw();
  }
  canvas.addEventListener('contextmenu', (e) => {
    const i = song ? dropAt(pxOfEvent(e)) : -1;
    if (i < 0) return;
    e.preventDefault();
    drops = drops.filter((_, k) => k !== i);
    meta(); refreshButtons(); draw();
  });

  $('song-save').onclick = async () => {
    try {
      song = await API.setDrops(song.name, drops);
      drops = [...song.drops]; saved = [...song.drops];
      setMsg('Saved: the next videos with this song use these drops.'); meta(); refreshButtons(); draw();
    } catch (e) { setMsg(e.message); }
  };
  $('song-reset').onclick = async () => {
    if (!confirm('Go back to the drops the app found?')) return;
    try {
      song = await API.setDrops(song.name, null);
      drops = [...song.drops]; saved = [...song.drops];
      setMsg('Back to the detected drops.'); meta(); refreshButtons(); draw();
    } catch (e) { setMsg(e.message); }
  };

  // the song list in Settings opens the editor
  $('mu-list').addEventListener('click', (e) => {
    const b = e.target.closest('[data-song]');
    if (b) open(b.dataset.song);
  });

  // ---- sound effect slots ------------------------------------------------------------------------------------
  const preview = new Audio();
  async function renderFx() {
    try {
      const r = await API.sfx();
      $('fx-slots').innerHTML = r.slots.map(s => `<div class="fx-slot" data-slot="${esc(s.slot)}" tabindex="0" role="button" aria-label="${esc(s.label)}: drop a file to replace it">
          <div class="fx-text"><b>${esc(s.label)}</b><span>${esc(s.what)}</span></div>
          <span class="fx-file${s.file ? ' mine' : ''}" title="${esc(s.file || '')}">${s.file ? esc(s.file) : 'built-in'}</span>
          <button class="small ghost fx-play" data-slot="${esc(s.slot)}" title="listen" aria-label="Listen to ${esc(s.label)}">${ic('play')}</button>
          ${s.file ? `<button class="small ghost fx-reset" data-slot="${esc(s.slot)}" title="use the built-in one" aria-label="Use the built-in ${esc(s.label)}">${ic('x')}</button>` : '<span></span>'}
          <input type="file" class="fx-file-in" accept=".wav,.mp3,.m4a,.aac,.ogg,.flac,.opus" hidden aria-hidden="true"></div>`).join('');
    } catch (e) { $('fx-slots').innerHTML = `<span class="muted">${esc(e.message)}</span>`; }
  }
  async function putFx(slot, file) {
    if (!file) return;
    try { await API.uploadSfx(slot, file); } catch (e) { alert(e.message); }
    renderFx();
  }
  const slots = $('fx-slots');
  slots.addEventListener('click', async (e) => {
    const play = e.target.closest('.fx-play'), reset = e.target.closest('.fx-reset'), row = e.target.closest('.fx-slot');
    if (play) { preview.src = API.sfxUrl(play.dataset.slot); preview.play().catch(() => {}); return; }
    if (reset) { try { await API.resetSfx(reset.dataset.slot); } catch (_) { /* ignore */ } renderFx(); return; }
    if (row) row.querySelector('.fx-file-in').click();
  });
  slots.addEventListener('keydown', (e) => {
    const row = e.target.closest('.fx-slot');
    if (row && e.target === row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); row.querySelector('.fx-file-in').click(); }
  });
  slots.addEventListener('change', (e) => {
    const inp = e.target.closest('.fx-file-in'); if (!inp) return;
    putFx(inp.closest('.fx-slot').dataset.slot, inp.files[0]); inp.value = '';
  });
  ['dragenter', 'dragover'].forEach(n => slots.addEventListener(n, (e) => { const row = e.target.closest('.fx-slot'); if (row) { e.preventDefault(); row.classList.add('over'); } }));
  ['dragleave', 'drop'].forEach(n => slots.addEventListener(n, (e) => { const row = e.target.closest('.fx-slot'); if (row) { e.preventDefault(); row.classList.remove('over'); } }));
  slots.addEventListener('drop', (e) => { const row = e.target.closest('.fx-slot'); if (row) putFx(row.dataset.slot, e.dataTransfer.files[0]); });
  $('btn-settings').addEventListener('click', renderFx);

  window.SongEditor = { open };
})();
