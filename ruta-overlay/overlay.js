/*
 * overlay.js — Animación de ruta + pin (estilo navegación de Google Maps) superpuesta a un <video> con SVG.
 *
 * Lee route.json (geometría, colores, tiempos) y tracking.json (movimiento de cámara por fotograma)
 * y redibuja el overlay en cada fotograma en función de video.currentTime, así la interpolación es
 * continua y el pin queda anclado al edificio aunque el dron se mueva.
 *
 * Capas (de abajo hacia arriba), iguales a las de render.py:
 *   glow -> borde oscuro con sombra -> trazo con degradado -> núcleo luminoso -> flujo de guiones -> cometa
 *   -> sombra del pin -> ondas de pulso -> pin (borde, cuerpo con degradado y brillo, hueco) -> etiqueta
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
    const n = P.length;
    const first = [P[0][0] - (P[1][0] - P[0][0]), P[0][1] - (P[1][1] - P[0][1])];
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
      for (let j = 0; j < per; j++) {                         // = np.linspace(t1, t2, per, endpoint=isLast)
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
    return { poly: out, length: L };
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
  const easeOutCubic = EASINGS.easeOutCubic;
  const easeOutQuad = (u) => 1 - (1 - u) * (1 - u);
  const easeOutBack = (u, s = 1.70158) => 1 + (s + 1) * Math.pow(u - 1, 3) + s * Math.pow(u - 1, 2);

  // estado de todos los elementos en el instante t (idéntico a anim_state() de render.py)
  function animState(t) {
    const ln = cfg.line, pin = cfg.pin;
    const ease = EASINGS[ln.easing] || EASINGS.easeInOutCubic;
    const progress = ease(clamp01((t - ln.start) / Math.max(ln.end - ln.start, 1e-6)));
    const headFade = (ln.head && ln.head.fade) || 0.3;
    const headAlpha = t < ln.start ? 0 : t <= ln.end ? 1 : 1 - clamp01((t - ln.end) / headFade);
    const st = { progress, headAlpha, pin: null, impact: null };
    const t0 = ln.end + (pin.delay || 0);
    if (t < t0 - 1e-9) return st;
    const dur = Math.max(pin.duration || 0.6, 1e-6);
    const u = clamp01((t - t0) / dur);
    if ((pin.animation || 'drop') === 'drop') {
      const fall = pin.fall_fraction ?? 0.5, dropH = pin.drop_height ?? 700, s0 = pin.drop_start_scale ?? 0.8;
      if (u < fall) {
        const uf = u / fall;
        const sc = s0 + (1 - s0) * easeOutQuad(uf);
        st.pin = { sx: sc, sy: sc, dy: -dropH * (1 - uf * uf), hf: 1 - uf * uf };
      } else {
        const v = (u - fall) / Math.max(1 - fall, 1e-6);
        const bounce = (pin.bounce_height ?? 70) * 4 * v * (1 - v);
        const pulse = Math.max(0, 1 - Math.abs(u - fall) / Math.max(pin.squash_width ?? 0.1, 1e-6));
        const sq = (pin.squash ?? 0.16) * pulse;
        st.pin = { sx: 1 + sq, sy: 1 - sq, dy: -bounce, hf: bounce / dropH };
      }
      st.impact = t0 + fall * dur;
    } else {
      const s0 = pin.start_scale ?? 0.15;
      const sc = s0 + (1 - s0) * easeOutBack(u, pin.overshoot ?? 1.70158);
      st.pin = { sx: sc, sy: sc, dy: 0, hf: 0 };
      st.impact = t0;
    }
    return st;
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
  const head = ln.head || {}, flow = ln.flow || {}, ripple = pin.ripple || {}, label = pin.label || {};
  svg.innerHTML = '';
  const defs = el('defs');

  const routeGrad = el('linearGradient', { id: 'route-grad', gradientUnits: 'userSpaceOnUse', x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
  el('stop', { offset: '0', 'stop-color': c.line }, routeGrad);
  el('stop', { offset: '1', 'stop-color': c.line_bright || c.line }, routeGrad);
  const pinGrad = el('linearGradient', { id: 'pin-grad', x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
  el('stop', { offset: '0', 'stop-color': c.pin_top || c.pin }, pinGrad);
  el('stop', { offset: '1', 'stop-color': c.pin_bottom || c.pin }, pinGrad);
  const hlGrad = el('radialGradient', { id: 'pin-hl' }, defs);
  el('stop', { offset: '0', 'stop-color': '#ffffff', 'stop-opacity': 0.45 }, hlGrad);
  el('stop', { offset: '1', 'stop-color': '#ffffff', 'stop-opacity': 0 }, hlGrad);

  const fShadow = el('filter', { id: 'f-shadow', x: '-20%', y: '-20%', width: '140%', height: '140%' }, defs);
  el('feDropShadow', { dx: (ln.shadow_offset || [0, 0])[0], dy: (ln.shadow_offset || [0, 0])[1], stdDeviation: (ln.shadow_blur || 0) * 0.6,
    'flood-color': c.shadow || '#000', 'flood-opacity': ln.shadow_opacity ?? 0.35 }, fShadow);
  const fGlow = el('filter', { id: 'f-glow', x: '-30%', y: '-30%', width: '160%', height: '160%' }, defs);
  el('feGaussianBlur', { stdDeviation: ln.glow_blur || 0 }, fGlow);
  const fHead = el('filter', { id: 'f-headglow', x: '-100%', y: '-100%', width: '300%', height: '300%' }, defs);
  el('feGaussianBlur', { stdDeviation: (head.glow_radius || 90) * 0.5 }, fHead);
  const fSoft = el('filter', { id: 'f-soft', x: '-50%', y: '-50%', width: '200%', height: '200%' }, defs);
  el('feGaussianBlur', { stdDeviation: (pin.shadow_blur || 16) * 0.6 }, fSoft);
  const fLabel = el('filter', { id: 'f-label', x: '-20%', y: '-40%', width: '140%', height: '200%' }, defs);
  el('feDropShadow', { dx: 0, dy: 6, stdDeviation: 10, 'flood-color': c.shadow || '#000', 'flood-opacity': 0.3 }, fLabel);

  // pin con la punta en (0,0); se escala alrededor de la punta
  const R = pin.head_radius, d = pin.height - R, cw = pin.casing_width || 0;
  const teardrop = (r) => {
    const a = Math.acos(r / d);
    const tx = (r * Math.sin(a)).toFixed(2), ty = (-d + r * Math.cos(a)).toFixed(2);
    const tipY = (r === R ? 0 : (d * r) / R - d).toFixed(2);
    return `M0,${tipY} L${tx},${ty} A${r},${r} 0 1 0 -${tx},${ty} Z`;
  };
  const clip = el('clipPath', { id: 'pin-clip' }, defs);
  el('path', { d: teardrop(R) }, clip);

  const strokeBase = { fill: 'none', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' };
  const routeG = el('g', {});
  const glowP = el('path', { ...strokeBase, stroke: c.glow || c.line, 'stroke-width': ln.glow_width || 0, opacity: ln.glow_opacity ?? 0.45, filter: 'url(#f-glow)' }, routeG);
  const casingP = el('path', { ...strokeBase, stroke: c.line_casing, 'stroke-width': ln.casing_width, filter: 'url(#f-shadow)' }, routeG);
  const mainP = el('path', { ...strokeBase, stroke: 'url(#route-grad)', 'stroke-width': ln.width }, routeG);
  const coreP = el('path', { ...strokeBase, stroke: c.line_core || '#fff', 'stroke-width': ln.core_width || 0, opacity: ln.core_opacity ?? 0 }, routeG);
  const flowP = el('path', { ...strokeBase, stroke: c.flow || '#fff', 'stroke-width': flow.width || 0, opacity: flow.opacity ?? 0,
    'stroke-dasharray': `${flow.dash || 70} ${flow.gap || 150}` }, routeG);
  const headG = el('g', { style: 'display:none' }, routeG);
  const headGlow = el('circle', { r: head.glow_radius || 90, fill: c.glow || c.line, opacity: 0.9, filter: 'url(#f-headglow)' }, headG);
  const headCore = el('circle', { r: head.radius || 30, fill: c.head || '#fff' }, headG);

  const pinG = el('g', { style: 'display:none' });
  const pinShadow = el('ellipse', { fill: c.shadow || '#000', filter: 'url(#f-soft)' }, pinG);
  const ripplesG = el('g', {}, pinG);
  const rippleEls = [];
  for (let i = 0; i < (ripple.count || 0); i++) {
    rippleEls.push(el('ellipse', { fill: 'none', stroke: c.ripple || c.line, style: 'display:none' }, ripplesG));
  }
  const pinBody = el('g', {}, pinG);
  if (cw > 0) el('path', { d: teardrop(R + cw), fill: c.pin_casing }, pinBody);
  el('path', { d: teardrop(R), fill: 'url(#pin-grad)' }, pinBody);
  el('ellipse', { cx: -0.3 * R, cy: -d - 0.35 * R, rx: 0.55 * R, ry: 0.5 * R, fill: 'url(#pin-hl)', 'clip-path': 'url(#pin-clip)' }, pinBody);
  if (pin.hole_radius > 0) el('circle', { cx: 0, cy: -d, r: pin.hole_radius, fill: c.pin_hole }, pinBody);

  const labelG = el('g', { style: 'display:none' }, pinG);
  const labelRect = el('rect', { fill: c.label_bg || '#fff', filter: 'url(#f-label)' }, labelG);
  const labelText = el('text', { fill: c.label_text || '#111', 'font-family': "'DM Sans', system-ui, sans-serif", 'font-weight': 700,
    'font-size': label.font_size || 58, 'dominant-baseline': 'central' }, labelG);
  labelText.textContent = label.text || '';

  const samples = ln.samples || 600;
  const rs = resampleByLength(catmullRom(cfg.route.points, samples * 2), samples);
  const poly0 = rs.poly;

  // ---------------------------------------------------------------- render por instante
  function renderAt(t) {
    const st = animState(t);
    const M = matrixAt(t);
    const poly = poly0.map((p) => applyH(M, p));
    const vis = partial(poly, st.progress);
    const dAttr = st.progress > 0 ? 'M' + vis.map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' L') : '';
    for (const p of [glowP, casingP, mainP, coreP, flowP]) p.setAttribute('d', dAttr);
    const [sx0, sy0] = poly[0], [ex, ey] = poly[poly.length - 1];
    routeGrad.setAttribute('x1', sx0.toFixed(1)); routeGrad.setAttribute('y1', sy0.toFixed(1));
    routeGrad.setAttribute('x2', ex.toFixed(1)); routeGrad.setAttribute('y2', ey.toFixed(1));
    if (flow.enabled) flowP.setAttribute('stroke-dashoffset', (-(flow.speed || 600) * t).toFixed(1));
    else flowP.style.display = 'none';

    if (head.enabled && st.headAlpha > 0 && st.progress > 0) {
      const [hx, hy] = vis[vis.length - 1];
      headG.style.display = '';
      headG.setAttribute('transform', `translate(${hx.toFixed(1)},${hy.toFixed(1)})`);
      headG.setAttribute('opacity', st.headAlpha.toFixed(3));
    } else headG.style.display = 'none';

    if (st.pin) {
      const { sx, sy, dy, hf } = st.pin;
      pinG.style.display = '';
      pinBody.setAttribute('transform', `translate(${ex.toFixed(1)},${(ey + dy).toFixed(1)}) scale(${sx.toFixed(3)},${sy.toFixed(3)})`);
      pinShadow.setAttribute('cx', ex.toFixed(1)); pinShadow.setAttribute('cy', (ey + 8).toFixed(1));
      pinShadow.setAttribute('rx', (68 * (1 + 0.6 * hf) * Math.max(sx, 0.2)).toFixed(1));
      pinShadow.setAttribute('ry', (26 * (1 + 0.6 * hf)).toFixed(1));
      pinShadow.setAttribute('opacity', ((pin.shadow_opacity ?? 0.35) * (1 - 0.7 * hf) * Math.min(1, sx)).toFixed(3));
      rippleEls.forEach((r, i) => {
        if (!ripple.enabled || st.impact == null) { r.style.display = 'none'; return; }
        const ru = (t - (st.impact + i * (ripple.interval || 0.35))) / Math.max(ripple.duration || 1, 1e-6);
        if (ru < 0 || ru >= 1) { r.style.display = 'none'; return; }
        const e = easeOutCubic(ru);
        const rad = ripple.radius_from + (ripple.radius_to - ripple.radius_from) * e;
        r.style.display = '';
        r.setAttribute('cx', ex.toFixed(1)); r.setAttribute('cy', ey.toFixed(1));
        r.setAttribute('rx', rad.toFixed(1)); r.setAttribute('ry', (rad * 0.6).toFixed(1));
        r.setAttribute('stroke-width', ((ripple.width || 12) * (1 - 0.6 * ru)).toFixed(1));
        r.setAttribute('opacity', ((ripple.opacity ?? 0.75) * (1 - ru)).toFixed(3));
      });
      const lstart = st.impact == null ? Infinity : st.impact + (label.delay ?? 0.15);
      if (label.enabled && label.text && t >= lstart) {
        const e = easeOutCubic(clamp01((t - lstart) / Math.max(label.duration || 0.35, 1e-6)));
        const fs = label.font_size || 58, px = label.padding_x ?? 36, py = label.padding_y ?? 22;
        let tw = 0;
        try { tw = labelText.getComputedTextLength(); } catch (err) { tw = fs * 0.55 * label.text.length; }
        const bw = tw + 2 * px, bh = fs * 0.72 + 2 * py;
        const lx = ex + (R + cw + (label.gap ?? 30)) - (1 - e) * 40;
        const ly = ey + dy - d - bh / 2;
        labelG.style.display = '';
        labelG.setAttribute('opacity', e.toFixed(3));
        labelRect.setAttribute('x', lx.toFixed(1)); labelRect.setAttribute('y', ly.toFixed(1));
        labelRect.setAttribute('width', bw.toFixed(1)); labelRect.setAttribute('height', bh.toFixed(1));
        labelRect.setAttribute('rx', Math.min(label.radius ?? 44, bh / 2).toFixed(1));
        labelText.setAttribute('x', (lx + px).toFixed(1)); labelText.setAttribute('y', (ly + bh / 2).toFixed(1));
      } else labelG.style.display = 'none';
    } else pinG.style.display = 'none';
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
    if (e.target && e.target.tagName === 'INPUT') return;
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
