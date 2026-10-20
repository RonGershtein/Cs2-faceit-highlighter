// Library page, Squad tab: the teammates whose plays go into squad montages (in-game name / Steam ID), the name
// and photo each one gets on the player card in videos, and the team name of the intro card.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  let members = [], loaded = false, dirty = false;

  async function load() {
    try {
      const r = await API.squadPlayers();
      members = r.members.map(m => ({ ...m }));
      $('sq-team').value = r.team || ''; $('sq-intro').checked = r.intro !== false;
      loaded = true; dirty = false; render();
    } catch (e) { $('sq-list').innerHTML = `<div class="empty">failed to load: ${esc(e.message)}</div>`; }
  }
  const initials = (m) => (m.card_name || m.name || '?').replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase() || '?';
  function face(m) {
    return m.photo ? `<div class="face"><img src="${API.playerPhotoUrl(m.photo)}" alt=""></div>` : `<div class="face" aria-hidden="true">${esc(initials(m))}</div>`;
  }
  function render() {
    $('squad-count').textContent = members.length ? String(members.length) : '';
    if (!members.length) { $('sq-list').innerHTML = '<div class="empty start">No teammates yet. Add the players whose plays should go into squad montages.</div>'; return; }
    $('sq-list').innerHTML = members.map((m, i) => `<div class="sq-card" data-i="${i}" style="animation-delay:${Math.min(i, 8) * 40}ms">
      <div class="sq-photo">${face(m)}
        <div class="row"><button class="small sq-pick" title="choose a photo and adjust it">${m.photo ? 'Change' : 'Add photo'}</button>
          ${m.photo ? `<button class="small ghost sq-unphoto" title="remove the photo" aria-label="Remove ${esc(m.name)}'s photo">${ic('x')}</button>` : ''}</div></div>
      <div class="sq-fields">
        <div class="sq-head"><span class="muted" style="font-size:var(--fs-s)">Teammate ${i + 1}</span><span class="spacer"></span>
          <button class="small ghost sq-del" title="remove from the squad" aria-label="Remove ${esc(m.name || 'this teammate')}">${ic('trash')} Remove</button></div>
        <div class="two">
          <div class="field"><label>In-game name</label><input type="text" class="sq-name" value="${esc(m.name)}" maxlength="40" autocomplete="off" spellcheck="false"></div>
          <div class="field"><label>Steam ID</label><input type="text" class="sq-sid" value="${esc(m.steamid)}" inputmode="numeric" maxlength="20" autocomplete="off" spellcheck="false" placeholder="7656119…"></div>
        </div>
        <div class="field"><label>Name on the video</label><input type="text" class="sq-card-name" value="${esc(m.card_name)}" maxlength="32" autocomplete="off" placeholder="${esc(m.name || 'same as in-game')}" ${m.steamid ? '' : 'disabled title="add the Steam ID to give a different name on the video"'}></div>
        <div class="sq-preview" aria-label="How the player card looks in the video"><span class="pv-face" style="${m.photo ? `background-image:url('${API.playerPhotoUrl(m.photo)}')` : ''}"></span>
          <span class="pv-name">${esc(m.card_name || m.name || '?')}</span><span class="pv-note">on the video</span></div>
      </div></div>`).join('');
  }
  function readInputs() {
    document.querySelectorAll('#sq-list .sq-card').forEach(el => {
      const m = members[+el.dataset.i]; if (!m) return;
      m.name = el.querySelector('.sq-name').value.trim();
      m.steamid = el.querySelector('.sq-sid').value.replace(/\D/g, '');
      m.card_name = el.querySelector('.sq-card-name').value.trim();
    });
  }
  async function save(quiet) {
    readInputs();
    const keep = members.filter(m => m.name || m.steamid);
    try {
      const r = await API.saveSquadPlayers(keep);
      await API.saveTeam($('sq-team').value, $('sq-intro').checked);
      members = r.members.map(m => ({ ...m })); dirty = false; render();
      if (!quiet) { $('sq-msg').textContent = 'Saved'; setTimeout(() => { if (!dirty) $('sq-msg').textContent = ''; }, 2500); }
      document.dispatchEvent(new CustomEvent('clips', { detail: null }));   // the first screen shows the new team name
    } catch (e) { $('sq-msg').textContent = e.message; }
  }
  const markDirty = () => { dirty = true; $('sq-msg').textContent = 'Not saved yet'; };
  $('sq-list').addEventListener('input', (e) => {
    const card = e.target.closest('.sq-card'); if (!card) return;
    markDirty(); readInputs();
    const m = members[+card.dataset.i];
    card.querySelector('.pv-name').textContent = m.card_name || m.name || '?';
    const cn = card.querySelector('.sq-card-name');
    cn.disabled = !m.steamid; cn.placeholder = m.name || 'same as in-game';
  });
  $('sq-team').addEventListener('input', markDirty);
  $('sq-intro').addEventListener('change', markDirty);
  $('sq-add').onclick = () => {
    readInputs(); members.push({ name: '', steamid: '', card_name: '', photo: null }); render();
    const cards = document.querySelectorAll('#sq-list .sq-card'); cards[cards.length - 1]?.querySelector('.sq-name').focus();
    markDirty();
  };
  $('sq-save').onclick = () => save(false);
  $('sq-list').addEventListener('click', async (e) => {
    const card = e.target.closest('.sq-card'); if (!card) return;
    const i = +card.dataset.i; readInputs(); const m = members[i];
    if (e.target.closest('.sq-del')) {
      if (!confirm(`Remove ${m.name || 'this teammate'} from the squad?\n\nTheir plays stop counting as squad plays. Recorded clips and their photo are kept.`)) return;
      members.splice(i, 1); render(); markDirty(); return;
    }
    if (e.target.closest('.sq-unphoto')) {
      try { await API.deletePlayerPhoto(m.steamid, m.name); m.photo = null; render(); } catch (err) { alert(err.message); }
      return;
    }
    if (e.target.closest('.sq-pick')) {
      if (!m.name && !m.steamid) { alert('Type the in-game name or the Steam ID first.'); return; }
      pickPhoto(m);
    }
  });
  document.addEventListener('squad-tab', () => { if (!loaded) load(); });
  if (location.hash === '#squad') load();
  else API.squadPlayers().then(r => { $('squad-count').textContent = r.members.length ? String(r.members.length) : ''; }).catch(() => {});
  window.addEventListener('beforeunload', (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

  // ---- photo: choose a file, drag and zoom it into the round frame, save a 512 px square -------------------
  const cv = $('crop-canvas'), cx = cv.getContext('2d');
  let img = null, zoom = 1, ox = 0, oy = 0, target = null;
  const S = cv.width;
  function draw() {
    cx.fillStyle = '#000'; cx.fillRect(0, 0, S, S);
    if (!img) return;
    const base = Math.max(S / img.width, S / img.height), w = img.width * base * zoom, h = img.height * base * zoom;
    ox = Math.min(0, Math.max(S - w, ox)); oy = Math.min(0, Math.max(S - h, oy));   // the picture always covers the frame
    cx.drawImage(img, ox, oy, w, h);
    cx.save(); cx.fillStyle = 'rgba(0,0,0,.6)'; cx.beginPath(); cx.rect(0, 0, S, S); cx.arc(S / 2, S / 2, S / 2 - 6, 0, Math.PI * 2, true); cx.fill('evenodd'); cx.restore();
    cx.strokeStyle = '#f5a623'; cx.lineWidth = 2; cx.beginPath(); cx.arc(S / 2, S / 2, S / 2 - 6, 0, Math.PI * 2); cx.stroke();
  }
  function setZoom(z, ax = S / 2, ay = S / 2) {   // zoom around a point so it stays under the pointer
    const nz = Math.max(1, Math.min(4, z)), k = nz / zoom;
    ox = ax - (ax - ox) * k; oy = ay - (ay - oy) * k; zoom = nz; $('crop-zoom').value = String(nz); draw();
  }
  function pickPhoto(m) {
    const inp = document.createElement('input'); inp.type = 'file'; inp.accept = 'image/png,image/jpeg,image/webp';
    inp.onchange = () => {
      const f = inp.files[0]; if (!f) return;
      const url = URL.createObjectURL(f), im = new Image();
      im.onload = () => {
        img = im; target = m; zoom = 1; $('crop-zoom').value = '1';
        const base = Math.max(S / im.width, S / im.height);
        ox = (S - im.width * base) / 2; oy = (S - im.height * base) / 2;
        $('crop-msg').textContent = ''; $('crop-modal').style.display = ''; draw();
      };
      im.onerror = () => alert('That file could not be opened as a picture.');
      im.src = url;
    };
    inp.click();
  }
  let drag = null;
  cv.addEventListener('pointerdown', (e) => { cv.setPointerCapture(e.pointerId); drag = { x: e.clientX, y: e.clientY, ox, oy }; });
  cv.addEventListener('pointermove', (e) => { if (!drag) return; ox = drag.ox + (e.clientX - drag.x); oy = drag.oy + (e.clientY - drag.y); draw(); });
  cv.addEventListener('pointerup', () => { drag = null; });
  cv.addEventListener('wheel', (e) => { e.preventDefault(); const r = cv.getBoundingClientRect(); setZoom(zoom * (e.deltaY < 0 ? 1.08 : 1 / 1.08), e.clientX - r.left, e.clientY - r.top); }, { passive: false });
  $('crop-zoom').addEventListener('input', (e) => setZoom(+e.target.value));
  $('crop-close').onclick = () => { $('crop-modal').style.display = 'none'; };
  $('crop-modal').addEventListener('click', (e) => { if (e.target === $('crop-modal')) $('crop-modal').style.display = 'none'; });
  $('crop-save').onclick = async () => {
    if (!img || !target) return;
    const out = document.createElement('canvas'); out.width = out.height = 512;
    const k = 512 / S, base = Math.max(S / img.width, S / img.height);
    out.getContext('2d').drawImage(img, ox * k, oy * k, img.width * base * zoom * k, img.height * base * zoom * k);
    $('crop-save').disabled = true; $('crop-msg').textContent = 'saving…';
    try {
      readInputs();
      if (dirty) await save(true);   // the photo belongs to the saved name / Steam ID
      const blob = await new Promise(res => out.toBlob(res, 'image/png'));
      const t = members.find(x => (target.steamid && x.steamid === target.steamid) || x.name === target.name) || target;
      const r = await API.uploadPlayerPhoto(blob, t.steamid || '', t.name || '');
      t.photo = r.photo; $('crop-modal').style.display = 'none'; render();
    } catch (e) { $('crop-msg').textContent = e.message; }
    $('crop-save').disabled = false;
  };
})();
