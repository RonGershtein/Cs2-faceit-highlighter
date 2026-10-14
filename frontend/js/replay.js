// 2D replay renderer + playback engine. Frames are sampled every `step` ticks; positions are interpolated.
class Replay {
  constructor({ canvas, meta, frames, events, mapInfo, radarImg, lowerImg }) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.meta = meta;
    this.frames = frames;
    this.events = events;
    this.map = mapInfo;
    this.radarImg = radarImg;
    this.lowerImg = lowerImg;
    this.ticks = new Int32Array(frames.map(f => f.t));
    this.tickrate = meta.tickrate || 64;
    this.first = meta.first_tick;
    this.last = meta.last_tick;
    this.tick = meta.rounds.length ? meta.rounds[0].freeze_end : this.first;
    this.playing = false;
    this.speed = 1;
    this.stopAt = null;
    this.me = null;
    this.showLower = false;
    this.showLabels = true;
    this.showCones = true;
    this.onTick = null;
    this.onClipEnd = null;
    this._last = 0;
    this._raf = null;
    this.F = meta.flags;
    this.view = this._contentBounds(radarImg, lowerImg);
    this.kills = events.kills.slice().sort((a, b) => a.tick - b.tick);
    this.killTicks = this.kills.map(k => k.tick);
    this._resize();
    window.addEventListener('resize', () => { this._resize(); this.draw(); });
    this._loop = this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }

  // ---- geometry ---------------------------------------------------------------
  _resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    const size = Math.max(200, Math.min(rect.width, rect.height) || 1024);
    this.canvas.width = Math.round(size * dpr);
    this.canvas.height = Math.round(size * dpr);
    this.s = (size * dpr) / 1024; // radar px -> canvas px
    this.dpr = dpr;
  }
  // bounding box of non-transparent radar pixels so the map fills the canvas
  _contentBounds(...imgs) {
    let x0 = 1024, y0 = 1024, x1 = 0, y1 = 0, any = false;
    for (const img of imgs) {
      if (!img) continue;
      const c = document.createElement('canvas'); c.width = c.height = 256;
      const cx = c.getContext('2d'); cx.drawImage(img, 0, 0, 256, 256);
      const d = cx.getImageData(0, 0, 256, 256).data;
      for (let y = 0; y < 256; y++) for (let x = 0; x < 256; x++) {
        if (d[(y * 256 + x) * 4 + 3] > 8) { any = true; x0 = Math.min(x0, x * 4); y0 = Math.min(y0, y * 4); x1 = Math.max(x1, x * 4 + 4); y1 = Math.max(y1, y * 4 + 4); }
      }
    }
    if (!any) return { x0: 0, y0: 0, size: 1024 };
    const pad = 20;
    const size = Math.max(x1 - x0, y1 - y0) + pad * 2;
    return { x0: (x0 + x1) / 2 - size / 2, y0: (y0 + y1) / 2 - size / 2, size: Math.min(1024, size) };
  }
  toPx(x, y) {
    const m = this.map, v = this.view;
    const rx = (x - m.pos_x) / m.scale, ry = (m.pos_y - y) / m.scale;
    return [(rx - v.x0) / v.size * 1024 * this.s, (ry - v.y0) / v.size * 1024 * this.s];
  }
  unitsToPx(u) { return (u / this.map.scale) / this.view.size * 1024 * this.s; }
  isLower(z) { return this.map.has_lower && z < this.map.threshold_z; }

  // ---- playback -----------------------------------------------------------------
  play() { this.playing = true; this._last = performance.now(); }
  pause() { this.playing = false; this.stopAt = null; }
  toggle() { this.playing ? this.pause() : this.play(); }
  setSpeed(s) { this.speed = s; }
  seek(tick, keepStop = false) {
    this.tick = Math.max(this.first, Math.min(this.last, tick));
    if (!keepStop) this.stopAt = null;
    this.draw();
  }
  playClip(start, end) {
    this.seek(start);
    this.stopAt = end;
    this.play();
  }
  stepFrames(n) {
    this.pause();
    const i = this._index(this.tick);
    const j = Math.max(0, Math.min(this.frames.length - 1, i + n));
    this.seek(this.frames[j].t);
  }
  roundAt(tick) {
    const rs = this.meta.rounds;
    for (let i = 0; i < rs.length; i++) if (tick <= rs[i].official_end) return rs[i];
    return rs[rs.length - 1];
  }
  seekRound(num) {
    const r = this.meta.rounds[num - 1];
    if (r) this.seek(r.freeze_end - 2 * this.tickrate);
  }
  nextRound() { const r = this.roundAt(this.tick); this.seekRound(Math.min(this.meta.rounds.length, r.num + 1)); }
  prevRound() {
    const r = this.roundAt(this.tick);
    const back = this.tick - (r.freeze_end - 2 * this.tickrate) > 3 * this.tickrate ? r.num : r.num - 1;
    this.seekRound(Math.max(1, back));
  }
  nextKill() {
    const k = this.kills.find(k => k.tick > this.tick + 2);
    if (k) this.seek(k.tick - 2 * this.tickrate);
  }
  prevKill() {
    const ks = this.kills.filter(k => k.tick < this.tick - 2.5 * this.tickrate);
    const k = ks[ks.length - 1];
    if (k) this.seek(k.tick - 2 * this.tickrate);
  }
  _loop(now) {
    if (this.playing) {
      const dt = Math.min(0.25, (now - this._last) / 1000);
      this._last = now;
      this.tick += dt * this.tickrate * this.speed;
      if (this.stopAt != null && this.tick >= this.stopAt) {
        this.tick = this.stopAt;
        this.pause();
        if (this.onClipEnd) this.onClipEnd();
      }
      if (this.tick >= this.last) { this.tick = this.last; this.pause(); }
      this.draw();
    }
    this._raf = requestAnimationFrame(this._loop);
  }

  // ---- frame lookup ---------------------------------------------------------------
  _index(tick) {
    let lo = 0, hi = this.ticks.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.ticks[mid] <= tick) lo = mid; else hi = mid - 1;
    }
    return lo;
  }
  state(tick = this.tick) {
    const i = this._index(tick);
    const f0 = this.frames[i], f1 = this.frames[Math.min(i + 1, this.frames.length - 1)];
    const span = f1.t - f0.t;
    const a = span > 0 ? Math.max(0, Math.min(1, (tick - f0.t) / span)) : 0;
    const next = new Map();
    for (const p of f1.p) next.set(p[0], p);
    const players = [];
    for (const p of f0.p) {
      const q = next.get(p[0]);
      let x = p[1], y = p[2], z = p[3], yaw = p[4];
      if (q && p[7] && q[7] && a > 0) {
        x += (q[1] - x) * a; y += (q[2] - y) * a; z += (q[3] - z) * a;
        let d = ((q[4] - yaw + 540) % 360) - 180;
        yaw += d * a;
      }
      players.push({ idx: p[0], x, y, z, yaw, hp: p[5], armor: p[6], alive: !!p[7], team: p[8], weapon: this.meta.weapons[p[9]] || '', flags: p[10], money: p[11] });
    }
    return { tick, frame: f0, players, grenades: f0.g || [] };
  }
  bombState(tick) {
    const r = this.roundAt(tick);
    let planted = null, ended = null;
    for (const b of this.events.bomb) {
      if (b.round !== r.num) continue;
      if (b.type === 'planted' && b.tick <= tick) planted = b;
      if ((b.type === 'defused' || b.type === 'exploded') && b.tick <= tick) ended = b;
    }
    if (planted && (!ended || ended.tick < planted.tick)) return { planted, ended: null };
    return planted ? { planted, ended } : null;
  }
  clock(tick) {
    const r = this.roundAt(tick);
    const tr = this.tickrate;
    if (tick < r.start) return { round: r.num, text: '—' };
    if (tick < r.freeze_end) return { round: r.num, text: `freeze ${Math.max(0, Math.ceil((r.freeze_end - tick) / tr))}` };
    if (tick > r.end) return { round: r.num, text: 'round over' };
    const bomb = this.bombState(tick);
    let rem;
    if (bomb && !bomb.ended && bomb.planted.tick <= tick) rem = 40 - (tick - bomb.planted.tick) / tr;
    else rem = 115 - (tick - r.freeze_end) / tr;
    rem = Math.max(0, rem);
    const m = Math.floor(rem / 60), s = Math.floor(rem % 60);
    return { round: r.num, text: `${m}:${String(s).padStart(2, '0')}`, bomb: bomb && !bomb.ended };
  }
  scoreAt(tick) {
    let a = 0, b = 0;
    const teamA = new Set(this.meta.team_a.players);
    for (const r of this.meta.rounds) {
      if (r.end > tick) break;
      // which side did team A play this round?
      let sideA = null;
      for (const sid of teamA) { if (r.teams[sid]) { sideA = r.teams[sid]; break; } }
      if (r.winner === sideA) a++; else b++;
    }
    return { a, b };
  }

  // ---- drawing -------------------------------------------------------------------------
  draw() {
    const ctx = this.ctx, tick = this.tick, s = this.s, tr = this.tickrate;
    const W = this.canvas.width;
    ctx.clearRect(0, 0, W, W);
    const img = this.showLower && this.lowerImg ? this.lowerImg : this.radarImg;
    const v = this.view, zoom = 1024 / v.size;
    if (img) ctx.drawImage(img, -v.x0 * zoom * this.s, -v.y0 * zoom * this.s, W * zoom, W * zoom); else { ctx.fillStyle = '#1a1d24'; ctx.fillRect(0, 0, W, W); }
    ctx.lineJoin = 'round';
    const st = this.state(tick);
    const round = this.roundAt(tick);

    // smokes / fires
    for (const sm of this.events.smokes) {
      if (sm.start <= tick && tick <= sm.end) {
        const [x, y] = this.toPx(sm.x, sm.y);
        const age = (tick - sm.start) / tr;
        const r = this.unitsToPx(144) * Math.min(1, 0.3 + age / 1.2);
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fillStyle = this.isLower(sm.z) === this.showLower ? 'rgba(210,210,215,0.55)' : 'rgba(210,210,215,0.2)';
        ctx.fill();
      }
    }
    for (const f of this.events.fires) {
      if (f.start <= tick && tick <= f.end) {
        const [x, y] = this.toPx(f.x, f.y);
        ctx.beginPath(); ctx.arc(x, y, this.unitsToPx(120), 0, Math.PI * 2);
        ctx.fillStyle = this.isLower(f.z) === this.showLower ? 'rgba(255,110,20,0.4)' : 'rgba(255,110,20,0.15)';
        ctx.fill();
      }
    }
    // flashes / HE pops (short)
    const pop = (list, color, maxR) => {
      for (const p of list) {
        const age = (tick - p.tick) / tr;
        if (age < 0 || age > 0.5) continue;
        const [x, y] = this.toPx(p.x, p.y);
        ctx.beginPath(); ctx.arc(x, y, maxR * (0.3 + age * 1.4), 0, Math.PI * 2);
        ctx.strokeStyle = color.replace('A', String(1 - age * 2)); ctx.lineWidth = 3 * s; ctx.stroke();
      }
    };
    pop(this.events.flashes, 'rgba(255,255,255,A)', 14 * s);
    pop(this.events.hes, 'rgba(255,90,60,A)', 14 * s);

    // grenades in flight
    const gcol = { smoke: '#d8d8dc', he: '#ff6a4a', molotov: '#ff9a2a', flash: '#fff4a0', decoy: '#8be58b' };
    for (const g of st.grenades) {
      const [x, y] = this.toPx(g[1], g[2]);
      ctx.beginPath(); ctx.arc(x, y, 3.2 * s, 0, Math.PI * 2);
      ctx.fillStyle = gcol[g[0]] || '#fff'; ctx.globalAlpha = this.isLower(g[3]) === this.showLower ? 1 : 0.4; ctx.fill();
      ctx.globalAlpha = 1;
    }

    // planted bomb
    const bomb = this.bombState(tick);
    if (bomb && !bomb.ended && bomb.planted.pos) {
      const [x, y] = this.toPx(bomb.planted.pos[0], bomb.planted.pos[1]);
      const blink = Math.floor(tick / (tr / 2)) % 2 === 0;
      ctx.fillStyle = blink ? '#ff3b3b' : '#b31d1d';
      ctx.fillRect(x - 6 * s, y - 6 * s, 12 * s, 12 * s);
      ctx.fillStyle = '#fff'; ctx.font = `${9 * s}px sans-serif`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText('C4', x, y);
    }

    // dead bodies this round
    for (const k of this.kills) {
      if (k.round !== round.num || k.tick > tick || !k.vpos) continue;
      const [x, y] = this.toPx(k.vpos[0], k.vpos[1]);
      ctx.strokeStyle = k.victim_team === 'CT' ? 'rgba(77,166,255,0.7)' : 'rgba(240,160,48,0.7)';
      ctx.lineWidth = 2.5 * s;
      const r = 5 * s;
      ctx.beginPath(); ctx.moveTo(x - r, y - r); ctx.lineTo(x + r, y + r); ctx.moveTo(x - r, y + r); ctx.lineTo(x + r, y - r); ctx.stroke();
    }

    // kill lines (fading 1.5 s)
    for (const k of this.kills) {
      const age = (tick - k.tick) / tr;
      if (age < 0 || age > 1.5 || !k.apos || !k.vpos) continue;
      const [ax, ay] = this.toPx(k.apos[0], k.apos[1]);
      const [vx, vy] = this.toPx(k.vpos[0], k.vpos[1]);
      ctx.strokeStyle = `rgba(255,60,60,${1 - age / 1.5})`; ctx.lineWidth = 2 * s;
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(vx, vy); ctx.stroke();
    }

    // players
    const F = this.F;
    const sorted = st.players.slice().sort((a, b) => (a.idx === this.meIdx ? 1 : 0) - (b.idx === this.meIdx ? 1 : 0));
    for (const p of sorted) {
      if (!p.alive) continue;
      const [x, y] = this.toPx(p.x, p.y);
      const onLevel = this.isLower(p.z) === this.showLower || !this.map.has_lower;
      ctx.globalAlpha = onLevel ? 1 : 0.4;
      const col = p.team === 'CT' ? '#4da6ff' : '#f0a030';
      const r = (p.flags & F.ducking ? 7 : 9) * s;
      if (this.showCones) {
        const ang = -p.yaw * Math.PI / 180;
        const len = 42 * s, half = 25 * Math.PI / 180;
        ctx.beginPath(); ctx.moveTo(x, y);
        ctx.arc(x, y, len, ang - half, ang + half); ctx.closePath();
        ctx.fillStyle = p.team === 'CT' ? 'rgba(77,166,255,0.22)' : 'rgba(240,160,48,0.22)'; ctx.fill();
      }
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = p.flags & F.blind ? '#ffffff' : col; ctx.fill();
      ctx.lineWidth = 2 * s; ctx.strokeStyle = onLevel ? '#0b0d12' : '#ffffff';
      if (!onLevel) ctx.setLineDash([3 * s, 3 * s]);
      ctx.stroke(); ctx.setLineDash([]);
      // hp arc
      if (p.hp < 100) {
        ctx.beginPath(); ctx.arc(x, y, r + 2.5 * s, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * p.hp / 100);
        ctx.strokeStyle = p.hp > 50 ? '#4ad37a' : p.hp > 25 ? '#ffd166' : '#ff5a5a'; ctx.lineWidth = 2 * s; ctx.stroke();
      }
      if (p.idx === this.meIdx) {
        ctx.beginPath(); ctx.arc(x, y, r + 5 * s, 0, Math.PI * 2); ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5 * s; ctx.stroke();
      }
      if (p.flags & F.bomb) { ctx.fillStyle = '#ff3b3b'; ctx.fillRect(x + r * 0.5, y - r * 1.4, 6 * s, 6 * s); }
      if (p.flags & F.defuser) { ctx.fillStyle = '#4ad37a'; ctx.fillRect(x - r * 1.4, y - r * 1.4, 5 * s, 5 * s); }
      if (this.showLabels) {
        const name = this.meta.players[p.idx]?.name || '';
        ctx.font = `${11 * s}px system-ui, sans-serif`; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        ctx.lineWidth = 3 * s; ctx.strokeStyle = 'rgba(0,0,0,0.8)'; ctx.strokeText(name, x, y + r + 2 * s);
        ctx.fillStyle = '#fff'; ctx.fillText(name, x, y + r + 2 * s);
      }
      ctx.globalAlpha = 1;
    }
    if (this.onTick) this.onTick(tick, st, round);
  }

  get meIdx() {
    if (this.me == null) return -1;
    if (this._meIdxCache?.sid !== this.me) {
      this._meIdxCache = { sid: this.me, idx: this.meta.players.findIndex(p => p.steamid === this.me) };
    }
    return this._meIdxCache.idx;
  }
}
window.Replay = Replay;
