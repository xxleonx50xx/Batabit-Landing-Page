/*
 * overlay.js — Animación de ruta + pin (estilo Google Maps) superpuesta a un <video> con SVG.
 *
 * Lee route.json (geometría, colores, tiempos) y tracking.json (movimiento de cámara por fotograma)
 * y redibuja el overlay en cada fotograma en función de video.currentTime, así la interpolación es
 * continua y el pin queda anclado al edificio aunque el dron se mueva.
 *
 * La geometría y los easings son idénticos a los de render.py: lo que ves aquí es lo que exporta el script.
 */
(async function () {
  const $ = (id) => document.getElementById(id);
  const video = $('video');
  const svg = $('overlay');
  const stage = $('stage');

  // ---------------------------------------------------------------- carga de configuración
  const cfg = await (await fetch('./route.json')).json();
  let tracking = null;
  if (cfg.tracking) {
    try {
      tracking = await (await fetch(cfg.tracking)).json();
    } catch (e) {
      console.warn('Sin tracking.json: el overlay usará cámara fija.', e);
    }
  }
  const W = cfg.video.width, H = cfg.video.height;
  const FPS = (tracking && tracking.video && tracking.video.fps) || cfg.video.fps || 30;
  const mats = tracking ? tracking.matrices.map((m) => m.map(Number)) : null;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

  // ---------------------------------------------------------------- geometría (igual que render.py)
  function catmullRom(points, samples, alpha = 0.5) {
    let P = points.map((p) => [p[0], p[1]]);
    if (P.length < 2) return P;
    if (P.length === 2) {
      const out = [];
      for (let i = 0; i < samples; i++) {
        const u = i / (samples - 1);
        out.push([P[0][0] + (P[1][0] - P[0][0]) * u, P[0][1] + (P[1][1] - P[0][1]) * u]);
      }
      return out;
    }
    const first = [P[0][0] - (P[1][0] - P[0][0]), P[0][1] - (P[1][1] - P[0][1])];
    const n = P.length;
    const last = [P[n - 1][0] + (P[n - 1][0] - P[n - 2][0]), P[n - 1][1] + (P[n - 1][1] - P[n - 2][1])];
    P = [first, ...P, last];
    const nseg = P.length - 3;
    const per = Math.max(4, Math.floor(samples / nseg));
    const dist = (a, b) => Math.max(Math.hypot(b[0] - a[0], b[1] - a[1]), 1e-6) ** alpha;
    const lerp = (a, b, w) => [a[0] * (1 - w) + b[0] * w, a[1] * (1 - w) + b[1] * w];
    const out = [];
    for (let i = 0; i < nseg; i++) {
      const [p0, p1, p2, p3] = [P[i], P[i + 1], P[i + 2], P[i + 3]];
      const t0 = 0, t1 = t0 + dist(p0, p1), t2 = t1 + dist(p1, p2), t3 = t2 + dist(p2, p3);
      const isLast = i === nseg - 1;
      // igual que np.linspace(t1, t2, per, endpoint=isLast)
      for (let j = 0; j < per; j++) {
        const t = t1 + ((t2 - t1) * j) / (isLast ? per - 1 : per);
        const a1 = lerp(p0, p1, (t - t0) / (t1 - t0));
        const a2 = lerp(p1, p2, (t - t1) / (t2 - t1));
        const a3 = lerp(p2, p3, (t - t2) / (t3 - t2));
        const b1 = lerp(a1, a2, (t - t0) / (t2 - t0));
        const b2 = lerp(a2, a3, (t - t1) / (t3 - t1));
        out.push(lerp(b1, b2, (t - t1) / (t2 - t1)));
      }
    }
    return out;
  }

  function resampleByLength(poly, n) {
    const cum = [0];
    for (let i = 1; i < poly.length; i++) cum.push(cum[i - 1] + Math.hypot(poly[i][0] - poly[i - 1][0], poly[i][1] - poly[i - 1][1]));
    const L = cum[cum.length - 1];
    const out = [];
    let k = 0;
    for (let i = 0; i < n; i++) {
      const target = (L * i) / (n - 1);
      while (k < cum.length - 2 && cum[k + 1] < target) k++;
      const span = cum[k + 1] - cum[k] || 1;
      const u = Math.min(1, Math.max(0, (target - cum[k]) / span));
      out.push([poly[k][0] + (poly[k + 1][0] - poly[k][0]) * u, poly[k][1] + (poly[k + 1][1] - poly[k][1]) * u]);
    }
    return out;
  }

  function partial(poly, progress) {
    if (progress <= 0) return [poly[0]];
    if (progress >= 1) return poly;
    const f = progress * (poly.length - 1);
    const i = Math.floor(f), u = f - i;
    const head = [poly[i][0] + (poly[i + 1][0] - poly[i][0]) * u, poly[i][1] + (poly[i + 1][1] - poly[i][1]) * u];
    return poly.slice(0, i + 1).concat([head]);
  }

  function applyH(M, p) {
    const x = M[0] * p[0] + M[1] * p[1] + M[2];
    const y = M[3] * p[0] + M[4] * p[1] + M[5];
    const w = M[6] * p[0] + M[7] * p[1] + M[8];
    return [x / w, y / w];
  }

  function matrixAt(t) {
    if (!mats) return [1, 0, 0, 0, 1, 0, 0, 0, 1];
    const f = t * FPS;
    const i = Math.max(0, Math.min(mats.length - 1, Math.floor(f)));
    const j = Math.min(mats.length - 1, i + 1);
    const u = Math.min(1, Math.max(0, f - i));
    return mats[i].map((v, k) => v * (1 - u) + mats[j][k] * u);
  }

  // ---------------------------------------------------------------- easings
  const clamp01 = (x) => Math.min(1, Math.max(0, x));
  const EASINGS = {
    linear: (u) => u,
    easeOutCubic: (u) => 1 - Math.pow(1 - u, 3),
    easeInOutCubic: (u) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2),
  };
  const easeOutBack = (u, s = 1.70158) => 1 + (s + 1) * Math.pow(u - 1, 3) + s * Math.pow(u - 1, 2);
  const easeOutBounce = (u) => {
    const n1 = 7.5625, d1 = 2.75;
    if (u < 1 / d1) return n1 * u * u;
    if (u < 2 / d1) return n1 * (u -= 1.5 / d1) * u + 0.75;
    if (u < 2.5 / d1) return n1 * (u -= 2.25 / d1) * u + 0.9375;
    return n1 * (u -= 2.625 / d1) * u + 0.984375;
  };

  function animState(t) {
    const ln = cfg.line, pin = cfg.pin;
    const ease = EASINGS[ln.easing] || EASINGS.easeInOutCubic;
    const progress = ease(clamp01((t - ln.start) / Math.max(ln.end - ln.start, 1e-6)));
    const pinT0 = ln.end + (pin.delay || 0);
    if (t < pinT0 - 1e-9) return { progress, scale: 0, dy: 0, visible: false, u: 0 };
    const u = clamp01((t - pinT0) / Math.max(pin.duration || 0.6, 1e-6));
    if (pin.animation === 'drop') {
      return { progress, scale: 1, dy: -(pin.drop_height || 400) * (1 - easeOutBounce(u)), visible: true, u };
    }
    const s0 = pin.start_scale ?? 0.15;
    return { progress, scale: s0 + (1 - s0) * easeOutBack(u, pin.overshoot ?? 1.70158), dy: 0, visible: true, u };
  }

  // ---------------------------------------------------------------- construcción del SVG a partir de route.json
  const NS = 'http://www.w3.org/2000/svg';
  const el = (name, attrs = {}, parent = svg) => {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    parent.appendChild(node);
    return node;
  };
  const c = cfg.colors, ln = cfg.line, pin = cfg.pin;
  svg.innerHTML = '';
  const defs = el('defs');
  const f1 = el('filter', { id: 'route-shadow', x: '-20%', y: '-20%', width: '140%', height: '140%' }, defs);
  el('feDropShadow', {
    dx: (ln.shadow_offset || [0, 0])[0], dy: (ln.shadow_offset || [0, 0])[1],
    stdDeviation: ln.shadow_blur || 0, 'flood-color': c.shadow || '#000', 'flood-opacity': ln.shadow_opacity ?? 0.3,
  }, f1);
  const f2 = el('filter', { id: 'pin-shadow', x: '-50%', y: '-50%', width: '200%', height: '200%' }, defs);
  el('feGaussianBlur', { stdDeviation: pin.shadow_blur || 0 }, f2);

  const routeG = el('g', { filter: 'url(#route-shadow)' });
  const casing = el('path', { fill: 'none', stroke: c.line_casing, 'stroke-width': ln.casing_width, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, routeG);
  const line = el('path', { fill: 'none', stroke: c.line, 'stroke-width': ln.width, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, routeG);

  // pin con la punta en (0,0); se escala alrededor de la punta
  const R = pin.head_radius, d = pin.height - R, cw = pin.casing_width || 0;
  const teardrop = (r) => {
    const a = Math.acos(r / d);
    const tx = (r * Math.sin(a)).toFixed(2), ty = (-d + r * Math.cos(a)).toFixed(2);
    const tipY = (r === R ? 0 : (d * r) / R - d).toFixed(2);
    return `M0,${tipY} L${tx},${ty} A${r},${r} 0 1 0 -${tx},${ty} Z`;
  };
  const pinG = el('g', { style: 'display:none' });
  const pinShadow = el('ellipse', { cx: 0, cy: 6, rx: 70, ry: 26, fill: c.shadow || '#000', opacity: pin.shadow_opacity ?? 0.3, filter: 'url(#pin-shadow)' }, pinG);
  const pinBody = el('g', {}, pinG);
  if (cw > 0) el('path', { d: teardrop(R + cw), fill: c.pin_casing }, pinBody);
  el('path', { d: teardrop(R), fill: c.pin }, pinBody);
  if (pin.hole_radius > 0) el('circle', { cx: 0, cy: -d, r: pin.hole_radius, fill: c.pin_hole }, pinBody);

  let ripple = null;
  if (pin.ripple) ripple = el('circle', { cx: 0, cy: 0, r: 0, fill: 'none', stroke: c.pin, 'stroke-width': 10, opacity: 0 }, pinG);

  const samples = ln.samples || 600;
  const poly0 = resampleByLength(catmullRom(cfg.route.points, samples * 2), samples);

  // ---------------------------------------------------------------- render por instante
  function renderAt(t) {
    const st = animState(t);
    const M = matrixAt(t);
    const poly = poly0.map((p) => applyH(M, p));
    const vis = partial(poly, st.progress);
    const dAttr = st.progress > 0 ? 'M' + vis.map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' L') : '';
    casing.setAttribute('d', dAttr);
    line.setAttribute('d', dAttr);
    const [ax, ay] = poly[poly.length - 1];
    if (st.visible && st.scale > 0.001) {
      pinG.style.display = '';
      pinShadow.setAttribute('transform', `translate(${ax.toFixed(1)},${ay.toFixed(1)}) scale(${st.scale.toFixed(3)})`);
      pinBody.setAttribute('transform', `translate(${ax.toFixed(1)},${(ay + st.dy).toFixed(1)}) scale(${st.scale.toFixed(3)})`);
      if (ripple) {
        const ru = clamp01((t - (ln.end + (pin.delay || 0))) / 0.9);
        ripple.setAttribute('transform', `translate(${ax.toFixed(1)},${ay.toFixed(1)})`);
        ripple.setAttribute('r', (40 + 260 * ru).toFixed(1));
        ripple.setAttribute('opacity', (0.7 * (1 - ru)).toFixed(3));
      }
    } else {
      pinG.style.display = 'none';
    }
    return st;
  }

  // ---------------------------------------------------------------- sincronización con el video
  const timeEl = $('time'), scrub = $('scrub'), playBtn = $('play');
  let last = -1;
  function tick() {
    const t = video.currentTime;
    if (t !== last) {
      last = t;
      renderAt(t);
      if (timeEl) timeEl.textContent = `${t.toFixed(2)} s · fotograma ${Math.round(t * FPS)}`;
      if (scrub && document.activeElement !== scrub) scrub.value = t;
    }
    requestAnimationFrame(tick);
  }
  video.addEventListener('loadedmetadata', () => {
    if (scrub) { scrub.max = video.duration; scrub.step = 1 / FPS; }
  });
  if (scrub) scrub.addEventListener('input', () => { video.pause(); video.currentTime = Number(scrub.value); });
  if (playBtn) {
    playBtn.addEventListener('click', () => (video.paused ? video.play() : video.pause()));
    video.addEventListener('play', () => (playBtn.textContent = 'Pausa'));
    video.addEventListener('pause', () => (playBtn.textContent = 'Reproducir'));
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === ' ') { e.preventDefault(); video.paused ? video.play() : video.pause(); }
    if (e.key === 'ArrowRight') { video.pause(); video.currentTime = Math.min(video.duration, video.currentTime + 1 / FPS); }
    if (e.key === 'ArrowLeft') { video.pause(); video.currentTime = Math.max(0, video.currentTime - 1 / FPS); }
  });

  // clic sobre el video -> coordenadas nativas (para editar route.json)
  const coords = $('coords');
  stage.addEventListener('click', (e) => {
    const r = stage.getBoundingClientRect();
    const x = Math.round(((e.clientX - r.left) / r.width) * W);
    const y = Math.round(((e.clientY - r.top) / r.height) * H);
    const msg = `[${x}, ${y}]  (coordenadas nativas en el fotograma actual)`;
    if (coords) coords.textContent = msg;
    console.log(msg);
  });

  requestAnimationFrame(tick);
  renderAt(0);
  window.overlay = { renderAt, animState, cfg, tracking, poly0 };
})();
