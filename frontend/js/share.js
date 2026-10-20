// Share a video: upload it to YouTube by hand (unlisted), keep the link, email it to yourself or send it on WhatsApp.
// Adds a Share button to the page's video player; the player tells it what is playing with window.setShareTarget().
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  let target = null;   // {demo_id, file, description}
  let info = null;

  document.body.insertAdjacentHTML('beforeend', `
<div class="modal" role="dialog" aria-modal="true" aria-labelledby="share-title" id="share-modal" style="display:none"><div class="modal-box sq">
  <div class="modal-head" id="share-title">${ic('share')} Share <span class="spacer"></span><button class="small ghost" id="share-close" aria-label="Close">${ic('x')}</button></div>
  <p class="sq-lead" id="share-what"></p>
  <div class="sq-block">
    <div class="sq-label">1. Upload it to YouTube</div>
    <div class="share-row"><button id="share-yt">${ic('external')} Open YouTube upload</button><button id="share-reveal">${ic('folder')} Show the file</button></div>
    <p class="sq-note">Drag the file onto the upload page and choose <b>Unlisted</b>: only people with the link can watch.</p>
    <div class="share-copy"><label class="sq-k" for="share-t">Title</label><input type="text" id="share-t" readonly><button class="small" data-copy="share-t" aria-label="Copy the title">${ic('copy')} Copy</button></div>
    <div class="share-copy"><label class="sq-k" for="share-d">Description</label><textarea id="share-d" rows="4" readonly></textarea><button class="small" data-copy="share-d" aria-label="Copy the description">${ic('copy')} Copy</button></div>
  </div>
  <div class="sq-block">
    <div class="sq-label"><label for="share-url">2. Paste the YouTube link</label></div>
    <div class="share-link"><input type="text" id="share-url" placeholder="https://youtu.be/…" autocomplete="off" spellcheck="false"><button id="share-save">Save</button></div>
  </div>
  <div class="sq-block">
    <div class="sq-label">3. Send it</div>
    <div class="share-row"><button class="primary" id="share-mail" disabled>${ic('mail')} Email it to me</button><button id="share-wa" disabled>${ic('chat')} Send on WhatsApp</button><button id="share-copy" disabled>${ic('copy')} Copy link</button></div>
    <p class="sq-note" id="share-msg" role="status" aria-live="polite"></p>
  </div>
</div></div>`);

  const msg = (t) => { $('share-msg').textContent = t; };
  const linked = (link) => {
    for (const id of ['share-mail', 'share-wa', 'share-copy']) $(id).disabled = !link;
    if (link) $('share-url').value = link;
  };
  async function open() {
    if (!target) return;
    info = null; msg(''); linked(null); $('share-url').value = '';
    $('share-what').textContent = target.description || target.file;
    $('share-modal').style.display = '';
    try {
      info = await API.shareInfo(target.demo_id, target.file);
      $('share-t').value = info.title; $('share-d').value = info.description;
      linked(info.link);
      if (info.link) msg('Already on YouTube: send it again whenever you like.');
    } catch (e) { msg(e.message); }
  }
  const close = () => { $('share-modal').style.display = 'none'; };
  $('share-close').onclick = close;
  $('share-modal').addEventListener('click', (e) => { if (e.target === $('share-modal')) close(); });
  $('share-yt').onclick = () => window.open('https://www.youtube.com/upload', '_blank', 'noopener');
  $('share-reveal').onclick = async () => {
    try { await API.shareReveal(target.demo_id, target.file); msg('The file is selected in Explorer: drag it onto the YouTube page.'); } catch (e) { msg(e.message); }
  };
  $('share-modal').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-copy]'); if (!b) return;
    try { await navigator.clipboard.writeText($(b.dataset.copy).value); msg('Copied.'); } catch (_) { $(b.dataset.copy).select(); msg('Press Ctrl+C to copy.'); }
  });
  const save = async () => {
    try { const r = await API.shareLink(target.demo_id, target.file, $('share-url').value); linked(r.link); if (info) info.link = r.link; msg('Link saved.'); } catch (e) { msg(e.message); }
  };
  $('share-save').onclick = save;
  $('share-url').addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
  $('share-mail').onclick = async () => {
    $('share-mail').disabled = true; msg('Sending…');
    try { const r = await API.shareEmail(target.demo_id, target.file); msg(`Sent to ${r.to}.`); } catch (e) { msg(e.message); }
    $('share-mail').disabled = false;
  };
  $('share-wa').onclick = () => {
    const link = $('share-url').value;
    window.open(`https://wa.me/?text=${encodeURIComponent(`${info?.title || target.description || ''}\n${link}`)}`, '_blank', 'noopener');
  };
  $('share-copy').onclick = async () => {
    try { await navigator.clipboard.writeText($('share-url').value); msg('Link copied.'); } catch (_) { $('share-url').select(); msg('Press Ctrl+C to copy.'); }
  };

  // the Share button in the page's video player
  const dl = $('video-dl');
  if (dl) {
    dl.insertAdjacentHTML('beforebegin', `<button class="small" id="video-share">${ic('share')} Share</button> `);
    $('video-share').onclick = () => { if (target) { $('video-close')?.click(); open(); } };
  }
  window.setShareTarget = (t) => { target = t; };
  window.openShare = (t) => { target = t; open(); };
})();
