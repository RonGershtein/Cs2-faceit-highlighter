// Library page, Highlights tab: every saved video of every match (kept after the demo is deleted), a player,
// and the montage tray that joins clips of different matches into one video.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const KIND = { clip: 'Original', cinematic: 'Cinematic', reel: 'Reel 9:16', long: 'Long video', montage: 'Montage' };
  const key = (c) => `${c.demo_id}/${c.file}`;
  const fmtLen = (s) => { s = Math.round(+s || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const fmtDay = (d) => { if (!d) return ''; const x = new Date(d); return isNaN(x) ? String(d).slice(0, 10) : x.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }); };
  let clips = [], kind = '', mapFilter = '', picking = false, picked = [], loaded = false;
  let killMin = 0, favOnly = false;   // with both: the plays of killMin+ kills plus the favorites

  // ---- tabs ---------------------------------------------------------------------------------------
  const TABS = { matches: ['tab-matches', 'view-matches'], highlights: ['tab-clips', 'view-clips'], squad: ['tab-squad', 'view-squad'] };
  function showTab(name) {
    if (!TABS[name]) name = 'matches';
    for (const [k, [tab, view]] of Object.entries(TABS)) { $(tab).setAttribute('aria-selected', String(k === name)); $(view).hidden = k !== name; }
    const clipsOn = name === 'highlights';
    $('tray').hidden = !(clipsOn && picking);
    if (clipsOn) load();
    if (name === 'squad') document.dispatchEvent(new Event('squad-tab'));
    history.replaceState(null, '', name === 'matches' ? location.pathname : '#' + name);
  }
  $('tab-matches').onclick = () => showTab('matches');
  $('tab-clips').onclick = () => showTab('highlights');
  $('tab-squad').onclick = () => showTab('squad');
  document.querySelector('.lib-tabs').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const order = ['tab-matches', 'tab-clips', 'tab-squad'];
    const i = order.indexOf(document.activeElement.id); if (i < 0) return;
    const next = $(order[(i + (e.key === 'ArrowRight' ? 1 : order.length - 1)) % order.length]);
    next.focus(); next.click();
  });

  // ---- list ---------------------------------------------------------------------------------------
  async function load() {
    try { clips = await API.clips(); loaded = true; render(); document.dispatchEvent(new CustomEvent('clips', { detail: clips })); }
    catch (e) { $('clips').innerHTML = `<div class="card empty">failed to load: ${esc(e.message)}</div>`; }
  }
  function visible() {
    const big = (c) => (c.kills || 0) >= killMin;
    return clips.filter(c => (!kind || (kind === 'joined' ? ['long', 'montage'].includes(c.kind) : c.kind === kind))
      && (!mapFilter || c.map === mapFilter || (c.maps || []).includes(mapFilter))
      && (killMin && favOnly ? big(c) || c.fav : killMin ? big(c) : favOnly ? c.fav : true));
  }
  function render() {
    $('clip-count').textContent = clips.length ? String(clips.length) : '';
    const maps = [...new Set(clips.flatMap(c => [c.map, ...(c.maps || [])]).filter(Boolean))].sort();
    const sel = $('clip-map-filter');
    sel.innerHTML = '<option value="">All maps</option>' + maps.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
    sel.value = maps.includes(mapFilter) ? mapFilter : ''; mapFilter = sel.value;
    const list = visible();
    if (!list.length) {
      $('clips').innerHTML = `<div class="card empty start"><div class="start-title">${clips.length ? 'Nothing here with these filters' : 'No highlights saved yet'}</div>
        ${clips.length ? '' : '<p class="muted" style="margin:0">Open a match, record a highlight and make it cinematic: every clip shows up here and stays, even after you delete the match.</p>'}</div>`;
      return;
    }
    $('clips').innerHTML = list.map((c, i) => {
      const n = picked.indexOf(key(c));
      const where = c.kind === 'montage' ? (c.maps || []).join(' · ') : [c.map, fmtDay(c.date)].filter(Boolean).join(' · ');
      const sub = [c.player, c.round ? `round ${c.round}` : ''].filter(Boolean).join(' · ');
      const url = API.videoUrl(c.demo_id, c.file);
      return `<article class="card clip ${n >= 0 ? 'picked' : ''}" data-k="${esc(key(c))}" draggable="true" style="--i:${Math.min(i, 11)}">
        <div class="thumb" role="button" tabindex="0" aria-label="${picking ? 'Select' : 'Play'}: ${esc(c.description)}">
          <video src="${url}#t=1.5" preload="metadata" muted playsinline tabindex="-1"></video>
          <span class="kind k-${c.kind}">${KIND[c.kind] || c.kind}${c.kills >= 3 ? ` · ${c.kills === 5 ? 'Ace' : c.kills + 'K'}` : ''}</span>
          ${c.fav ? `<span class="fav-mark" title="favorite">${ic('star')}</span>` : ''}
          <span class="len">${fmtLen(c.seconds)}</span>
          ${picking ? `<span class="pick" aria-hidden="true">${n >= 0 ? n + 1 : ''}</span>` : `<span class="play" aria-hidden="true">${ic('play')}</span>`}
        </div>
        <div class="clip-body">
          <div class="clip-title" title="${esc(c.description)}">${esc(c.description)}</div>
          <div class="clip-meta">${esc(where)}${sub ? ` <span class="dot">·</span> ${esc(sub)}` : ''}</div>
          <div class="clip-foot">
            ${c.demo_exists ? `<a href="/match.html?id=${esc(c.demo_id)}" class="small-link">open match</a>` : (c.kind === 'montage' ? '' : '<span class="muted gone" title="the demo was deleted; the clip is kept">match deleted</span>')}
            <span class="spacer"></span>
            <button class="icon-btn fav-btn${c.fav ? ' on' : ''}" aria-pressed="${!!c.fav}" title="${c.fav ? 'remove from favorites' : 'add to favorites'}" aria-label="Favorite: ${esc(c.description)}">${ic(c.fav ? 'star' : 'star-o')}</button>
            <a class="icon-btn" href="${url}" download="${esc(c.file)}" title="download" aria-label="Download ${esc(c.description)}">${ic('download')}</a>
            <button class="small ghost del-clip" title="delete this video" aria-label="Delete ${esc(c.description)}">${ic('x')}</button>
          </div>
        </div></article>`;
    }).join('');
  }
  const byKey = (k) => clips.find(c => key(c) === k);

  $('kind-seg').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-kind]'); if (!b) return;
    kind = b.dataset.kind;
    b.parentElement.querySelectorAll('button').forEach(x => x.classList.toggle('active', x === b));
    render();
  });
  function setKillFilter(k, fav) {
    killMin = k; favOnly = fav;
    $('kill-seg').querySelectorAll('button').forEach(x => x.classList.toggle('active', +x.dataset.k === k));
    $('fav-filter').classList.toggle('active', fav); $('fav-filter').setAttribute('aria-pressed', String(fav));
  }
  $('kill-seg').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-k]'); if (!b) return;
    setKillFilter(+b.dataset.k, favOnly); render();
  });
  $('fav-filter').onclick = () => { setKillFilter(killMin, !favOnly); render(); };
  $('clip-map-filter').addEventListener('change', (e) => { mapFilter = e.target.value; render(); });

  $('clips').addEventListener('click', async (e) => {
    const card = e.target.closest('.clip'); if (!card) return;
    const c = byKey(card.dataset.k); if (!c) return;
    if (e.target.closest('.fav-btn')) {
      const on = !c.fav;
      markFav(card, c, on);   // at once: the request can wait behind the thumbnails loading
      try { await API.favorite(c.demo_id, c.file, on); } catch (err) { markFav(card, c, !on); alert(err.message); }
      return;
    }
    if (e.target.closest('.del-clip')) {
      const extra = c.kind === 'clip' && clips.some(x => x.kind === 'cinematic' && x.demo_id === c.demo_id && x.highlight_id === c.highlight_id)
        ? `\n\nIts cinematic version is deleted too${(cineOf(c) || {}).fav ? ', and that one is a favorite' : ''}.` : '';
      if (!confirm(`Delete "${c.description}" (${KIND[c.kind] || c.kind})?${c.fav ? '\n\nIt is one of your favorites.' : ''}${extra}\n\nCopies already in your Downloads folder stay.`)) return;
      try { await API.releaseVideo(c.file); await API.deleteClip(c.demo_id, c.file); picked = picked.filter(k => byKey(k) && k !== key(c)); await load(); renderTray(); }
      catch (err) { alert(err.message); }
      return;
    }
    if (!e.target.closest('.thumb')) return;
    if (picking) { togglePick(key(c)); return; }
    openVideo(c);
  });
  function markFav(card, c, on) {
    c.fav = on;
    const b = card.querySelector('.fav-btn');
    b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); b.title = on ? 'remove from favorites' : 'add to favorites';
    b.innerHTML = ic(on ? 'star' : 'star-o');
    const mark = card.querySelector('.fav-mark');
    if (on && !mark) card.querySelector('.kind').insertAdjacentHTML('afterend', `<span class="fav-mark" title="favorite">${ic('star')}</span>`);
    if (!on && mark) mark.remove();
    card.hidden = !visible().includes(c);   // unstarred while only favorites are shown
  }
  $('clips').addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.classList?.contains('thumb')) { e.preventDefault(); e.target.click(); }
  });
  // hover preview: the clip plays muted under the pointer (fine pointers only)
  if (matchMedia('(hover: hover) and (pointer: fine)').matches) {
    $('clips').addEventListener('pointerover', (e) => { const v = e.target.closest('.thumb')?.querySelector('video'); if (v && v.paused) v.play().catch(() => {}); });
    $('clips').addEventListener('pointerout', (e) => {
      const t = e.target.closest('.thumb'); if (!t || t.contains(e.relatedTarget)) return;
      const v = t.querySelector('video'); v.pause(); v.currentTime = 1.5;
    });
  }

  // ---- player -------------------------------------------------------------------------------------
  function openVideo(c) {
    $('video-title').textContent = `${c.description} (${fmtLen(c.seconds)})`;
    $('video-el').src = API.videoUrl(c.demo_id, c.file);
    $('video-dl').href = API.videoUrl(c.demo_id, c.file); $('video-dl').setAttribute('download', c.file);
    window.setShareTarget && window.setShareTarget({ demo_id: c.demo_id, file: c.file, description: c.description });
    $('video-modal').style.display = '';
  }
  $('video-close').onclick = () => { $('video-modal').style.display = 'none'; const v = $('video-el'); v.pause(); v.removeAttribute('src'); v.load(); };
  $('video-modal').addEventListener('click', (e) => { if (e.target === $('video-modal')) $('video-close').click(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('video-modal').style.display === '') $('video-close').click(); });

  // ---- montage ------------------------------------------------------------------------------------
  function setPicking(on) {
    picking = on;
    $('montage-mode').classList.toggle('active', on);
    $('view-clips').classList.toggle('picking', on);
    $('tray').hidden = !on;
    render(); renderTray();
  }
  // ticking only updates the badges: re-rendering the grid would reload every thumbnail video
  function markPicks() {
    document.querySelectorAll('#clips .clip').forEach(el => {
      const n = picked.indexOf(el.dataset.k);
      el.classList.toggle('picked', n >= 0);
      const b = el.querySelector('.pick'); if (b) b.textContent = n >= 0 ? String(n + 1) : '';
    });
  }
  function togglePick(k) {
    const i = picked.indexOf(k);
    if (i >= 0) picked.splice(i, 1); else picked.push(k);
    markPicks(); renderTray();
  }
  function renderTray() {
    const items = picked.map(byKey).filter(Boolean);
    const total = items.reduce((a, c) => a + (+c.seconds || 0), 0);
    const nMatches = new Set(items.map(c => c.demo_id)).size;
    $('tray-info').textContent = items.length ? `${items.length} selected · ${fmtLen(total)} · from ${nMatches} match${nMatches > 1 ? 'es' : ''}` : 'click or drag clips here; for a video, the order below is the play order';
    $('tray-note').textContent = montageRunning ? 'a montage is being made; wait for it to finish' : items.length === 1 ? 'pick at least one more' : '';
    $('tray-go').disabled = items.length < 2 || montageRunning;
    $('tray-del').disabled = !items.length;
    $('tray-del').innerHTML = `${ic('trash')} Delete${items.length ? ' ' + items.length : ''}`;
    $('tray-list').innerHTML = items.map((c, i) => `<li data-k="${esc(key(c))}" draggable="true" title="drag to change the order"><span class="grip" aria-hidden="true">⠿</span><span class="n">${i + 1}</span><span class="tt" title="${esc(c.description)}">${esc(c.description)}</span>
      <span class="muted">${esc(c.map || '')}</span>
      <button class="small ghost tr-up" ${i ? '' : 'disabled'} aria-label="Move earlier">↑</button><button class="small ghost tr-down" ${i < items.length - 1 ? '' : 'disabled'} aria-label="Move later">↓</button><button class="small ghost tr-x" aria-label="Unselect">${ic('x')}</button></li>`).join('');
  }
  $('tray-list').addEventListener('click', (e) => {
    const li = e.target.closest('li'); if (!li) return;
    const i = picked.indexOf(li.dataset.k); if (i < 0) return;
    if (e.target.closest('.tr-x')) picked.splice(i, 1);
    else if (e.target.closest('.tr-up') && i > 0) [picked[i - 1], picked[i]] = [picked[i], picked[i - 1]];
    else if (e.target.closest('.tr-down') && i < picked.length - 1) [picked[i + 1], picked[i]] = [picked[i], picked[i + 1]];
    else return;
    markPicks(); renderTray();
  });
  $('montage-mode').onclick = () => setPicking(!picking);
  $('tray-close').onclick = () => setPicking(false);
  $('tray-clear').onclick = () => { picked = []; markPicks(); renderTray(); };
  $('tray-all').onclick = () => {   // everything the filters show, in the order shown, after what is already picked
    for (const c of visible()) if (!picked.includes(key(c))) picked.push(key(c));
    markPicks(); renderTray();
  };
  // the cinematic version of an original (deleting an original takes it along)
  const cineOf = (c) => c.kind === 'clip' ? clips.find(x => x.kind === 'cinematic' && x.demo_id === c.demo_id && x.highlight_id === c.highlight_id) : null;
  // with favorites among the selected: delete them too, or keep them (and the originals of starred cinematic versions)
  function askDelete(items) {
    const keep = items.filter(c => !c.fav && !(cineOf(c) || {}).fav);
    const favs = items.filter(c => c.fav).length, saved = items.length - favs - keep.length;
    const s = (n) => n > 1 ? 's' : '';
    $('del-text').textContent = `${favs} of the ${items.length} selected video${s(items.length)} ${favs > 1 ? 'are favorites' : 'is a favorite'}`
      + (saved ? ` and ${saved} ${saved > 1 ? 'are originals' : 'is an original'} of a starred cinematic version` : '')
      + `. Delete ${favs + saved > 1 ? 'them' : 'it'} too, or keep ${favs + saved > 1 ? 'them' : 'it'}? Copies already in your Downloads folder stay.`;
    $('del-all').textContent = `Delete all ${items.length}`;
    $('del-keep').textContent = keep.length ? `Keep favorites, delete ${keep.length}` : 'Keep favorites';
    $('del-modal').style.display = '';
    return new Promise(res => {
      const done = (v) => { $('del-modal').style.display = 'none'; res(v); };
      $('del-all').onclick = () => done(items);
      $('del-keep').onclick = () => done(keep);
      $('del-cancel').onclick = $('del-close').onclick = () => done(null);
      $('del-modal').onclick = (e) => { if (e.target === $('del-modal')) done(null); };
    });
  }
  $('tray-del').onclick = async () => {
    let items = picked.map(byKey).filter(Boolean);
    if (!items.length) return;
    if (items.some(c => c.fav || (cineOf(c) || {}).fav)) {
      items = await askDelete(items);
      if (!items || !items.length) return;
    } else {
      const withCine = items.filter(c => cineOf(c) && !picked.includes(key(cineOf(c)))).length;
      if (!confirm(`Delete ${items.length} video${items.length > 1 ? 's' : ''}?${withCine ? `\n\n${withCine} of them ${withCine > 1 ? 'are originals whose' : 'is an original whose'} cinematic version is deleted too.` : ''}\n\nCopies already in your Downloads folder stay.`)) return;
    }
    const btn = $('tray-del'); btn.disabled = true; btn.textContent = 'deleting…';
    const failed = [];
    // originals first would take their cinematic versions with them; a "no such clip" afterwards is fine
    for (const c of items) { try { await API.releaseVideo(c.file); await API.deleteClip(c.demo_id, c.file); } catch (err) { if (!/no such clip/i.test(err.message)) failed.push(`${c.description}: ${err.message}`); } }
    picked = []; await load(); renderTray();
    if (failed.length) alert('Some videos could not be deleted:\n' + failed.join('\n'));
  };

  // drag and drop: drag any clip card into the tray (it opens while you drag) and drop it where it should play;
  // drag the rows of the tray to change the order
  let dragKey = null;
  $('clips').addEventListener('dragstart', (e) => {
    const card = e.target.closest?.('.clip'); if (!card) return;
    dragKey = card.dataset.k;
    e.dataTransfer.effectAllowed = 'copyMove'; e.dataTransfer.setData('text/plain', dragKey);
    card.classList.add('dragging');
    if (!picking) setPicking(true);
    $('tray').classList.add('drop-ready');
  });
  $('tray-list').addEventListener('dragstart', (e) => {
    const li = e.target.closest('li'); if (!li) return;
    dragKey = li.dataset.k; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', dragKey);
    li.classList.add('dragging');
  });
  document.addEventListener('dragend', () => {
    dragKey = null;
    document.querySelectorAll('.dragging').forEach(el => el.classList.remove('dragging'));
    $('tray').classList.remove('drop-ready', 'drop-over'); dropMarker(-1);
  });
  function dropIndex(y) {
    const rows = [...$('tray-list').querySelectorAll('li')];
    const i = rows.findIndex(li => { const r = li.getBoundingClientRect(); return y < r.top + r.height / 2; });
    return i < 0 ? rows.length : i;
  }
  function dropMarker(i) {
    const rows = [...$('tray-list').querySelectorAll('li')];
    rows.forEach((li, j) => { li.classList.toggle('drop-before', j === i); li.classList.toggle('drop-after', i === rows.length && j === rows.length - 1); });
  }
  $('tray').addEventListener('dragover', (e) => {
    if (!dragKey) return;
    e.preventDefault(); e.dataTransfer.dropEffect = picked.includes(dragKey) ? 'move' : 'copy';
    $('tray').classList.add('drop-over'); dropMarker(dropIndex(e.clientY));
  });
  $('tray').addEventListener('dragleave', (e) => { if (!$('tray').contains(e.relatedTarget)) { $('tray').classList.remove('drop-over'); dropMarker(-1); } });
  $('tray').addEventListener('drop', (e) => {
    e.preventDefault(); if (!dragKey) return;
    let at = dropIndex(e.clientY);
    const was = picked.indexOf(dragKey);
    if (was >= 0) { picked.splice(was, 1); if (was < at) at -= 1; }
    picked.splice(at, 0, dragKey);
    $('tray').classList.remove('drop-over'); dropMarker(-1);
    markPicks(); renderTray();
  });

  // the montage dialog of the Matches tab (song, order, slow motion, casters, colour…) for the picked videos
  $('tray-go').onclick = () => {
    const items = picked.map(byKey).filter(Boolean);
    if (items.length < 2) return;
    window.openClipMontage(items, async (joined) => {
      picked = []; setPicking(false);
      if (!joined) return;            // cinematic: a background job, the status line above the matches follows it
      showJoined(); await load();
      const made = clips.find(c => c.kind === 'montage' && c.file === joined.file);
      if (made) openVideo(made);
    });
  };
  function showJoined() {
    kind = 'joined'; $('kind-seg').querySelectorAll('button').forEach(x => x.classList.toggle('active', x.dataset.kind === 'joined'));
    setKillFilter(0, false);   // montages have no kill count: they would stay hidden
  }

  // ---- cinematic montage job (runs in the background; the same job slot as a match's montage) ----------
  let montageRunning = false, montagePoll = null;
  const jobName = (s) => ({ Recap: `${ic('calendar')} Recap`, 'Squad montage': `${ic('users')} Squad montage` }[s.label] || `${ic('film')} Montage`);
  const fmtEta = (s) => s == null ? '' : s < 10 ? ' · almost done' : s < 60 ? ` · about ${Math.round(s / 5) * 5} s left` : ` · about ${Math.ceil(s / 60)} min left`;
  function startMontagePoll() {
    if (montagePoll) clearInterval(montagePoll);
    let wasRunning = false;
    const tick = async () => {
      const s = await API.squadStatus().catch(() => null);
      const el = $('montage-status');
      if (!s || s.status === 'idle' || s.demo_id !== 'montages') { el.innerHTML = ''; montageRunning = false; clearInterval(montagePoll); montagePoll = null; return; }
      el.classList.toggle('err', s.status === 'error');
      if (s.status === 'running') {
        wasRunning = montageRunning = true;
        const step = { starting: 'starting', recording: 'recording the missing plays in CS2', cinematic: 'making the plays cinematic', joining: 'joining' }[s.stage] || s.stage;
        const pct = Math.round(100 * (s.progress || 0));
        el.innerHTML = `${jobName(s)} (${s.total} plays): <b>${pct}%</b> · ${esc(step)}${s.detail ? ` · ${esc(s.detail)}` : ''}${fmtEta(s.eta)}${s.missing ? ` · <span style="color:var(--danger)">${esc(s.missing)}</span>` : ''} <button class="small ghost" id="montage-cancel">${ic('stop')} Stop</button>
          <span class="progress live"><div style="width:${Math.max(2, pct)}%"></div></span>`;
        $('montage-cancel').onclick = async () => { await API.squadCancel().catch(() => {}); };
        return;
      }
      montageRunning = false; clearInterval(montagePoll); montagePoll = null; renderTray();
      if (s.status === 'error') { el.innerHTML = `${jobName(s)} stopped: ${esc(s.error)}`; return; }
      if (!wasRunning) { el.innerHTML = ''; return; }   // finished before this page was opened: the grid shows it
      const r = s.result || {};
      el.innerHTML = `${jobName(s)} ready: <b>${esc(r.description || '')}</b>${r.seconds ? ` (${fmtLen(r.seconds)})` : ''} <button class="small primary" id="montage-watch">${ic('play')} Watch</button>`
        + (r.exported && !String(r.exported).startsWith('export failed') ? ` <span class="muted">saved to ${esc(r.exported)}</span>` : '')
        + (r.warning ? `<div style="margin-top:6px;color:var(--danger)">${esc(r.warning)}. Open those matches and record them, then make the montage again.</div>` : '');
      showJoined(); await load();
      const made = clips.find(c => c.kind === 'montage' && c.file === r.file);
      $('montage-watch').onclick = () => openVideo(made || { ...r, demo_id: 'montages' });
    };
    montagePoll = setInterval(tick, 1500); tick();
  }
  // ---- recap: the squad's plays of a month / a year ------------------------------------------------
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  function recapRange() {
    const now = new Date(), y = now.getFullYear(), m = now.getMonth();
    switch ($('recap-period').value) {
      case 'month': return [iso(new Date(y, m, 1)), iso(new Date(y, m + 1, 0))];
      case 'lastmonth': return [iso(new Date(y, m - 1, 1)), iso(new Date(y, m, 0))];
      case 'year': return [`${y}-01-01`, `${y}-12-31`];
      case 'lastyear': return [`${y - 1}-01-01`, `${y - 1}-12-31`];
      default: return [$('recap-from').value, $('recap-to').value];
    }
  }
  let recapSeq = 0;
  async function recapPreview() {
    const custom = $('recap-period').value === 'custom';
    if (!custom) { const [f, t] = recapRange(); $('recap-from').value = f; $('recap-to').value = t; }
    $('recap-from').disabled = $('recap-to').disabled = !custom;
    const [date_from, date_to] = recapRange();
    if (!date_from || !date_to || date_from > date_to) { $('recap-preview').textContent = 'pick the dates'; $('recap-go').disabled = true; return; }
    const seq = ++recapSeq;
    try {
      const q = { date_from, date_to, min_kills: $('recap-min').value };
      if ($('recap-best').value) q.best_of = $('recap-best').value;
      const r = await API.recapPreview(q);
      if (seq !== recapSeq) return;
      const rows = r.matches.map(m => `<div>${esc(m.map || 'match')} · ${esc(fmtDay(m.date))} · ${m.plays} play${m.plays > 1 ? 's' : ''}</div>`).join('');
      $('recap-preview').innerHTML = r.plays
        ? `${rows}<div style="margin-top:4px"><b>${r.plays} plays from ${r.matches.length} match${r.matches.length > 1 ? 'es' : ''} · about ${fmtLen(r.seconds)} before slow motion</b></div>`
        : 'No recorded squad plays in this period yet.';
      if (r.missing.length) {
        const byMatch = {}; r.missing.forEach(x => { (byMatch[x.demo_id] ||= { ...x, n: 0 }).n++; });
        $('recap-preview').innerHTML += `<div style="margin-top:6px">Not recorded yet, so left out: ${Object.values(byMatch).map(x => `<a href="/match.html?id=${esc(x.demo_id)}">${esc(x.map || 'match')} ${esc(fmtDay(x.date))}</a> (${x.n})`).join(', ')}. Open the match and use Squad montage or Record to add them.</div>`;
      }
      $('recap-go').disabled = !r.plays || montageRunning;
    } catch (e) { $('recap-preview').textContent = e.message; $('recap-go').disabled = true; }
  }
  $('recap-open').onclick = () => { $('recap-msg').textContent = ''; $('recap-modal').style.display = ''; recapPreview(); };
  $('recap-close').onclick = () => $('recap-modal').style.display = 'none';
  $('recap-modal').addEventListener('click', (e) => { if (e.target === $('recap-modal')) $('recap-modal').style.display = 'none'; });
  ['recap-period', 'recap-min', 'recap-best', 'recap-from', 'recap-to'].forEach(k => $(k).addEventListener('change', recapPreview));
  $('recap-go').onclick = async () => {
    const [date_from, date_to] = recapRange();
    $('recap-go').disabled = true; $('recap-msg').textContent = 'starting…';
    try {
      await API.recap({ date_from, date_to, min_kills: +$('recap-min').value, best_of: +$('recap-best').value || null, title: $('recap-name').value });
      $('recap-modal').style.display = 'none'; $('recap-msg').textContent = ''; $('recap-name').value = '';
      startMontagePoll();
    } catch (e) { $('recap-msg').textContent = e.message; $('recap-go').disabled = false; }
  };

  window.startMontagePoll = startMontagePoll;
  window.openClip = openVideo;   // the library's first screen plays the latest montage with it
  window.showTab = showTab;   // the Matches tab starts squad montages too
  startMontagePoll();   // picks up a montage that is still being made

  // first paint: the Highlights tab when the URL asks for it, and the count on the tab either way
  if (location.hash === '#highlights' || location.hash === '#squad') showTab(location.hash.slice(1));
  else API.clips().then(c => { if (!loaded) { clips = c; $('clip-count').textContent = c.length ? String(c.length) : ''; } document.dispatchEvent(new CustomEvent('clips', { detail: c })); }).catch(() => {});
})();
