// Thin API client. All URLs relative so the frontend can be served from anywhere.
const API = (() => {
  async function json(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
      let msg = r.statusText;
      try { const j = await r.json(); msg = j.detail || JSON.stringify(j); } catch (_) { /* ignore */ }
      throw new Error(msg);
    }
    return r.json();
  }
  const get = (url) => json(url);
  const post = (url, body) => json(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return {
    listDemos: () => json('/api/demos'),
    getDemo: (id) => json(`/api/demos/${id}`),
    status: (id) => json(`/api/demos/${id}/status`),
    events: (id) => json(`/api/demos/${id}/events`),
    frames: (id) => json(`/api/demos/${id}/frames`),
    highlights: (id, player) => json(`/api/demos/${id}/highlights${player ? `?player=${player}` : ''}`),
    deleteDemo: (id) => json(`/api/demos/${id}`, { method: 'DELETE' }),
    clips: () => json('/api/clips'),
    deleteClip: (demoId, name) => json(`/api/clips/${demoId}/${encodeURIComponent(name)}`, { method: 'DELETE' }),
    montage: (clips, title, opts = {}) => post('/api/clips/montage', { clips, title, ...opts }),
    reprocess: (id) => post(`/api/demos/${id}/reprocess`, {}),
    launch: (id, tick, player_name, player_steamid) => post(`/api/demos/${id}/launch`, { tick, player_name, player_steamid }),
    csdmUrl: (id, player) => `/api/demos/${id}/csdm-sequences${player ? `?player=${player}` : ''}`,
    radar: (map) => json(`/api/radars/${map}.json`),
    recordTools: () => json('/api/record/tools'),
    installTools: () => post('/api/record/tools/install', {}),
    recordStatus: () => json('/api/record/status'),
    recordCancel: () => post('/api/record/cancel', {}),
    record: (id, highlight_ids) => post(`/api/demos/${id}/record`, { highlight_ids }),
    videos: (id) => json(`/api/demos/${id}/videos`),
    exportVideos: (id) => post(`/api/demos/${id}/videos/export`, {}),
    deleteClips: (id, highlight_ids, downloads) => post(`/api/demos/${id}/videos/delete`, { highlight_ids, downloads }),
    longVideo: (id, highlight_ids, cinematic) => post(`/api/demos/${id}/videos/long`, { highlight_ids, cinematic }),
    deleteLongVideo: (id, name) => json(`/api/demos/${id}/videos/long/${encodeURIComponent(name)}`, { method: 'DELETE' }),
    cinematic: (id, highlight_ids, settings) => post(`/api/demos/${id}/videos/cinematic`, { highlight_ids, settings }),
    cineSettings: () => get('/api/cinematic/settings'),
    squad: () => json('/api/squad'),
    saveSquad: (members) => json('/api/squad', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ members }) }),
    squadPreview: (id, minKills) => json(`/api/demos/${id}/squad?min_kills=${minKills}`),
    squadReel: (id, body) => post(`/api/demos/${id}/squad`, body),
    squadStatus: () => json('/api/squad/status'),
    recapPreview: (q) => json('/api/clips/recap?' + new URLSearchParams(q)),
    recap: (body) => post('/api/clips/recap', body),
    squadPlayers: () => json('/api/squad/players'),
    saveSquadPlayers: (members) => json('/api/squad/players', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ members }) }),
    uploadPlayerPhoto(blob, steamid, name) { const fd = new FormData(); fd.append('file', blob, 'photo.png'); fd.append('steamid', steamid); fd.append('name', name); return fetch('/api/squad/players/photo', { method: 'POST', body: fd }).then(r => r.ok ? r.json() : r.json().then(j => { throw new Error(j.detail || 'upload failed'); })); },
    deletePlayerPhoto: (steamid, name) => json(`/api/squad/players/photo?steamid=${encodeURIComponent(steamid)}&name=${encodeURIComponent(name)}`, { method: 'DELETE' }),
    playerPhotoUrl: (f) => `/api/players/photo/${encodeURIComponent(f)}?v=${Date.now()}`,
    saveTeam: (team, intro) => json('/api/squad', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ team, intro }) }),
    multiPreview: (demo_ids, min_kills) => post('/api/squad/multi/preview', { demo_ids, min_kills }),
    multiSquad: (body) => post('/api/squad/multi', body),
    squadCancel: () => post('/api/squad/cancel', {}),
    cineCancel: (all) => post(`/api/cinematic/cancel${all ? '?all=true' : ''}`, {}),
    music: () => json('/api/music'),
    deleteMusic: (name) => json(`/api/music/${encodeURIComponent(name)}`, { method: 'DELETE' }),
    uploadMusic(file) { const fd = new FormData(); fd.append('file', file, file.name); return fetch('/api/music', { method: 'POST', body: fd }).then(r => r.ok ? r.json() : r.json().then(j => { throw new Error(j.detail || 'upload failed'); })); },
    voice: () => json('/api/voice'),
    deleteVoice: (slot, name) => json(`/api/voice/${encodeURIComponent(slot)}/${name.split('/').map(encodeURIComponent).join('/')}`, { method: 'DELETE' }),
    uploadVoice(slot, file, sub) { const fd = new FormData(); fd.append('file', file, file.name); fd.append('sub', sub || ''); return fetch(`/api/voice/${encodeURIComponent(slot)}`, { method: 'POST', body: fd }).then(r => r.ok ? r.json() : r.json().then(j => { throw new Error(j.detail || 'upload failed'); })); },
    voiceUrl: (slot, name) => `/api/voice/${encodeURIComponent(slot)}/${name.split('/').map(encodeURIComponent).join('/')}`,
    // Windows cannot delete a file a player still streams: stop every <video> showing it before deleting
    releaseVideo(name) {
      const enc = encodeURIComponent(name); let n = 0;
      document.querySelectorAll('video').forEach(v => { const src = v.getAttribute('src') || ''; if (src.includes(enc) || src.includes(name)) { v.pause(); v.removeAttribute('src'); v.load(); n++; } });
      return new Promise(res => setTimeout(res, n ? 400 : 0));
    },
    videoUrl: (id, name) => `/api/demos/${id}/videos/${encodeURIComponent(name)}`,
    getConfig: () => json('/api/config'),
    pickFolder: (start, title) => post('/api/pick-folder', { start, title }),
    setConfig: (c) => post('/api/config', c),
    upload(file, onProgress) {
      return new Promise((resolve, reject) => {
        const fd = new FormData();
        fd.append('file', file, file.name);
        fd.append('last_modified', String(file.lastModified || ''));
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/demos');
        xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
          else { let m = xhr.statusText; try { m = JSON.parse(xhr.responseText).detail || m; } catch (_) { /* */ } reject(new Error(m)); }
        };
        xhr.onerror = () => reject(new Error('network error'));
        xhr.send(fd);
      });
    },
  };
})();
