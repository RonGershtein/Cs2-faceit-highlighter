// Library page, Highlights tab: bring in a video filmed outside the app (Medal, phone...), cut it to the part
// that matters (start / end, previewed from the local file) and name the player. The server cuts it with FFmpeg.
(() => {
  const $ = (id) => document.getElementById(id);
  const fmt = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`;
  const v = $('imp-video');
  let url = null, file = null, playing = false;

  const start = () => +$('imp-start').value, end = () => +$('imp-end').value;
  function sync(from) {   // the sliders never cross: dragging one pushes the other
    if (from === 'start' && start() > end() - 0.5) $('imp-end').value = Math.min(v.duration, start() + 0.5);
    if (from === 'end' && end() < start() + 0.5) $('imp-start').value = Math.max(0, end() - 0.5);
    $('imp-start-t').textContent = fmt(start()); $('imp-end-t').textContent = fmt(end());
    $('imp-len').textContent = `cut: ${(end() - start()).toFixed(1)} s of ${v.duration.toFixed(1)} s`;
  }
  function reset() {
    v.pause(); v.removeAttribute('src'); v.load();
    if (url) URL.revokeObjectURL(url);
    url = file = null; playing = false;
    $('imp-file').value = ''; $('imp-editor').hidden = true; $('imp-go').disabled = true; $('imp-msg').textContent = '';
    $('imp-text').value = ''; $('imp-fail').checked = false;
  }
  const close = () => { $('import-modal').style.display = 'none'; reset(); };
  async function loadSquad() {   // the player is always one of the squad (the name each one has on the video)
    const sel = $('imp-player');
    try {
      const { members } = await API.squadPlayers();
      const names = members.map(m => (m.card_name || m.name || '').trim()).filter(Boolean);
      sel.innerHTML = names.map(n => `<option>${n.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))}</option>`).join('');
      $('imp-player-hint').textContent = names.length ? '' : 'No squad yet: add your teammates in the Squad tab to pick them here.';
    } catch (e) { $('imp-player-hint').textContent = e.message; }
  }
  $('import-open').onclick = () => { reset(); $('import-modal').style.display = ''; loadSquad(); };
  $('imp-close').onclick = $('imp-cancel').onclick = close;
  $('import-modal').addEventListener('click', (e) => { if (e.target === $('import-modal')) close(); });

  $('imp-file').onchange = (e) => {
    file = e.target.files[0]; if (!file) return;
    if (url) URL.revokeObjectURL(url);
    url = URL.createObjectURL(file); v.src = url;
    $('imp-msg').textContent = 'loading the video…';
  };
  v.onloadedmetadata = () => {
    for (const id of ['imp-start', 'imp-end']) $(id).max = v.duration;
    $('imp-start').value = 0; $('imp-end').value = v.duration;
    $('imp-editor').hidden = false; $('imp-go').disabled = false; $('imp-msg').textContent = '';
    sync();
  };
  v.onerror = () => { if (file) $('imp-msg').textContent = 'the browser cannot play this file, so it cannot be cut here'; };
  $('imp-start').oninput = () => { sync('start'); v.currentTime = start(); };
  $('imp-end').oninput = () => { sync('end'); v.currentTime = Math.max(start(), end() - 1); };
  $('imp-set-start').onclick = () => { $('imp-start').value = v.currentTime; sync('start'); };
  $('imp-set-end').onclick = () => { $('imp-end').value = v.currentTime; sync('end'); };
  $('imp-play').onclick = () => { playing = true; v.currentTime = start(); v.play(); };
  v.ontimeupdate = () => { if (playing && v.currentTime >= end()) { v.pause(); playing = false; } };
  v.onpause = () => { if (v.currentTime < end()) playing = false; };

  $('imp-go').onclick = async () => {
    $('imp-go').disabled = true; $('imp-msg').textContent = 'cutting and saving… (a few seconds)';
    try {
      await API.importClip(file, start(), end() >= v.duration - 0.05 ? 0 : end(), $('imp-player').value, $('imp-text').value, $('imp-fail').checked);
      close(); window.showTab('highlights');
    } catch (err) { $('imp-msg').textContent = err.message; $('imp-go').disabled = false; }
  };
})();
