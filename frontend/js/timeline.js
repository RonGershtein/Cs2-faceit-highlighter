// Timeline strip: rounds (colored by winner), kills, highlights, current position. Click/drag to seek.
class Timeline {
  constructor(canvas, replay, highlights, me) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.replay = replay;
    this.highlights = highlights || [];
    this.me = me;
    this.first = replay.first; this.last = replay.last;
    this._resize();
    window.addEventListener('resize', () => { this._resize(); this.draw(); });
    let dragging = false;
    const seekEv = (e) => {
      const rect = canvas.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width;
      replay.seek(this.first + x * (this.last - this.first));
    };
    canvas.addEventListener('pointerdown', (e) => { dragging = true; canvas.setPointerCapture(e.pointerId); seekEv(e); });
    canvas.addEventListener('pointermove', (e) => { if (dragging) seekEv(e); this._hover(e); });
    canvas.addEventListener('pointerup', () => { dragging = false; });
    canvas.addEventListener('pointerleave', () => { canvas.title = ''; });
  }
  _resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.round(rect.width * dpr);
    this.canvas.height = Math.round(44 * dpr);
    this.dpr = dpr;
  }
  x(tick) { return (tick - this.first) / (this.last - this.first) * this.canvas.width; }
  _hover(e) {
    const rect = this.canvas.getBoundingClientRect();
    const tick = this.first + (e.clientX - rect.left) / rect.width * (this.last - this.first);
    const r = this.replay.roundAt(tick);
    this.canvas.title = `round ${r.num} (${r.winner} win, ${r.reason})`;
  }
  setHighlights(hs) { this.highlights = hs; this.draw(); }
  draw() {
    const ctx = this.ctx, W = this.canvas.width, H = this.canvas.height, d = this.dpr;
    ctx.clearRect(0, 0, W, H);
    const meta = this.replay.meta;
    // rounds
    for (const r of meta.rounds) {
      const x0 = this.x(r.start), x1 = this.x(r.official_end);
      ctx.fillStyle = r.winner === 'CT' ? 'rgba(77,166,255,0.28)' : 'rgba(240,160,48,0.28)';
      ctx.fillRect(x0, 0, x1 - x0, H);
      ctx.fillStyle = 'rgba(0,0,0,0.5)'; ctx.fillRect(x1 - 1, 0, 1, H);
      if (x1 - x0 > 14 * d) {
        ctx.fillStyle = 'rgba(255,255,255,0.55)'; ctx.font = `${10 * d}px system-ui`; ctx.textBaseline = 'top'; ctx.textAlign = 'left';
        ctx.fillText(String(r.num), x0 + 3 * d, 3 * d);
      }
    }
    // kills
    for (const k of this.replay.kills) {
      const x = this.x(k.tick);
      const mine = this.me && (k.attacker === this.me);
      const died = this.me && (k.victim === this.me);
      ctx.fillStyle = mine ? '#4ad37a' : died ? '#ff5a5a' : 'rgba(255,255,255,0.35)';
      ctx.fillRect(x - 0.5 * d, H - 14 * d, Math.max(1, d), 12 * d);
    }
    // highlights
    for (const h of this.highlights) {
      if (h.type === 'kill') continue;
      const x0 = this.x(h.start_tick), x1 = this.x(h.end_tick);
      ctx.fillStyle = h.type === 'clutch' ? '#ffd166' : h.type === 'ace' ? '#ff7ab6' : h.type === 'ninja_defuse' ? '#a78bfa' : '#4ad37a';
      ctx.fillRect(x0, H / 2 - 3 * d, Math.max(2 * d, x1 - x0), 6 * d);
    }
    // cursor
    const cx = this.x(this.replay.tick);
    ctx.fillStyle = '#fff'; ctx.fillRect(cx - d, 0, 2 * d, H);
  }
}
window.Timeline = Timeline;
