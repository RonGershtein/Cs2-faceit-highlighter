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
    music: () => json('/api/music'),
    deleteMusic: (name) => json(`/api/music/${encodeURIComponent(name)}`, { method: 'DELETE' }),
    uploadMusic(file) { const fd = new FormData(); fd.append('file', file, file.name); return fetch('/api/music', { method: 'POST', body: fd }).then(r => r.ok ? r.json() : r.json().then(j => { throw new Error(j.detail || 'upload failed'); })); },
    voice: () => json('/api/voice'),
    deleteVoice: (slot, name) => json(`/api/voice/${encodeURIComponent(slot)}/${name.split('/').map(encodeURIComponent).join('/')}`, { method: 'DELETE' }),
    uploadVoice(slot, file, sub) { const fd = new FormData(); fd.append('file', file, file.name); fd.append('sub', sub || ''); return fetch(`/api/voice/${encodeURIComponent(slot)}`, { method: 'POST', body: fd }).then(r => r.ok ? r.json() : r.json().then(j => { throw new Error(j.detail || 'upload failed'); })); },
    voiceTts: (text, slot, sub, voice) => post('/api/voice/tts', { text, slot, sub: sub || '', voice: voice || '' }),
    voiceUrl: (slot, name) => `/api/voice/${encodeURIComponent(slot)}/${name.split('/').map(encodeURIComponent).join('/')}`,
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
