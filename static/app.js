'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const root = document.body;
  const card = $('card'), before = $('before'), after = $('after'), hit = $('hit'), wipe = $('wipe');
  const fill = document.querySelector('.fill');
  const calm = matchMedia('(prefers-reduced-motion: reduce)');

  // Images wider or taller than this are shown as a preview of this size. The full size is made on download.
  const PREVIEW = 1600;
  const MAX_BYTES = 100 * 1024 * 1024;
  const BG = { checker: 'transparent', white: 'white', black: 'black' };

  const S = {
    file: null, stem: 'image', id: null, big: false, mode: 'ai', bg: 'checker',
    run: 0,          // counts loads and re-runs, so a slow answer for an older one is dropped
    image: 0,        // counts loads only, so a full-size result is never kept or saved for another image
    xhr: null,       // the request for what is on screen, aborted when a newer one starts
    shown: null,     // { key, blob } of the cutout on screen
    full: null,      // { key, blob } of the last full-size result
    pos: 100, dragging: false, raf: 0, creep: 0, p: 0, tune: 0, noteTimer: 0,
  };

  // Real vibration where the browser has it (Android). Silent elsewhere.
  const buzz = (pattern) => { try { if (navigator.vibrate) navigator.vibrate(pattern); } catch (e) { /* not allowed here */ } };
  const flag = (el, name, on) => { if (on) el.dataset[name] = ''; else delete el.dataset[name]; };
  const setPhase = (phase) => { root.dataset.phase = phase; };

  // Point an <img> at a blob and wait until it is on screen. Not img.decode(): that rejects very large images
  // the browser can still show. The previous blob URL is released once the new one is in.
  function put(img, blob) {
    return new Promise((ok, bad) => {
      const old = img.src;
      img.onload = () => { if (old) URL.revokeObjectURL(old); ok(); };
      img.onerror = bad;
      img.src = URL.createObjectURL(blob);
    });
  }

  function note(text, ms) {
    clearTimeout(S.noteTimer);
    $('note').textContent = text;
    if (ms) S.noteTimer = setTimeout(() => { $('note').textContent = ''; }, ms);
  }

  function fail(title, hint) {
    S.run++;
    if (S.xhr) S.xhr.abort();
    clearInterval(S.creep);
    flag(root, 'ready', false);
    $('error-title').textContent = title;
    $('error-hint').textContent = hint || '';
    setPhase('error');
    buzz([10, 50, 10]);
  }

  function explain(err) {
    if (err.status === 0) return fail('Could not reach the server', 'Check the connection and try again.');
    if (err.error) return fail(err.error, err.hint);
    fail('Something went wrong on the server', 'Try again in a moment.');
  }

  const settings = () => ({
    mode: S.mode,
    tolerance: $('tolerance').value,
    smooth: $('smooth').value,
    edges: $('edges').getAttribute('aria-checked') === 'true' ? 1 : 0,
  });

  // One POST to /api/remove. The body is the image file, or nothing when the query carries the id of one sent before.
  function send(query, body, hooks = {}) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/remove?' + new URLSearchParams(query));
      xhr.responseType = 'blob';
      if (hooks.upload) xhr.upload.onprogress = (e) => { if (e.lengthComputable) hooks.upload(e.loaded / e.total); };
      xhr.onload = async () => {
        if (xhr.status === 200) return resolve({ blob: xhr.response, id: xhr.getResponseHeader('X-Image-Id') });
        let info = {};
        try { info = JSON.parse(await xhr.response.text()); } catch (e) { /* not our JSON, a proxy error page for example */ }
        reject({ status: xhr.status, error: info.error, hint: info.hint });
      };
      xhr.onerror = () => reject({ status: 0 });
      xhr.onabort = () => reject({ aborted: true });
      if (body) xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      if (hooks.handle) hooks.handle(xhr);
      xhr.send(body || null);
    });
  }

  // Same, but when the server no longer has the image (it keeps them for half an hour), send the file again.
  async function sendWithImage(query, hooks) {
    if (S.id) {
      try { return await send({ ...query, id: S.id }, null, hooks); } catch (err) {
        if (err.status !== 410) throw err;
        S.id = null;
      }
    }
    const res = await send(query, S.file, hooks);
    S.id = res.id;
    return res;
  }

  function progress(p) { S.p = p; fill.style.setProperty('--p', p); }
  function creep() {
    // There is no real percentage while the server works, so the line eases toward the end without reaching it.
    clearInterval(S.creep);
    S.creep = setInterval(() => progress(S.p + (0.92 - S.p) * 0.05), 150);
  }
  const working = () => (S.mode === 'ai' ? 'Finding the subject' : 'Matching the background color');

  function setPos(v) {
    S.pos = v;
    flag(card, 'edge', v <= 0 || v >= 100);  // the line would only show as a sliver on the image's edge
    card.style.setProperty('--pos', v + '%');
    wipe.value = Math.round(v);
  }

  // The one orchestrated moment: the line crosses once and uncovers the cutout, then the controls come in.
  function reveal() {
    const done = () => { flag(root, 'ready', true); note('Drag across the image to compare', 4000); buzz(12); };
    if (calm.matches) { setPos(100); return done(); }
    const t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / 900);
      setPos((k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2) * 100);
      if (k < 1) S.raf = requestAnimationFrame(step); else done();
    };
    S.raf = requestAnimationFrame(step);
  }

  // Ask for the cutout with the current settings and put it on screen.
  // how: 'upload' for a new image, 'rerun' after a mode switch, 'tune' after a slider moved.
  async function show(how) {
    const run = ++S.run;
    if (S.xhr) S.xhr.abort();
    cancelAnimationFrame(S.raf);
    const query = { ...settings(), bg: 'transparent' };
    if (S.big) query.max = PREVIEW;
    const key = JSON.stringify(query);

    if (how === 'tune') {
      flag(root, 'busy', true);
    } else {
      flag(root, 'ready', false);
      note('');
      if (after.src) URL.revokeObjectURL(after.src);
      after.removeAttribute('src');
      setPos(0);
      progress(0.04);
      setPhase('working');
      $('stage').textContent = how === 'upload' ? 'Uploading' : working();
      if (how !== 'upload') creep();
    }

    let res;
    try {
      res = await sendWithImage(query, {
        handle: (xhr) => { S.xhr = xhr; },
        upload: (f) => {
          if (run !== S.run) return;
          progress(0.04 + f * 0.4);
          if (f === 1) { $('stage').textContent = working(); creep(); }
        },
      });
    } catch (err) {
      if (err.aborted || run !== S.run) return;
      flag(root, 'busy', false);
      // A slider change that fails keeps the image and the last good cutout on screen.
      if (how === 'tune') return note(err.error || 'That change did not go through. Try again.', 4000);
      return explain(err);
    }
    if (run !== S.run) return;

    try { await put(after, res.blob); } catch (e) { return run === S.run && explain({}); }
    if (run !== S.run) return;
    S.shown = { key, blob: res.blob };
    clearInterval(S.creep);
    flag(root, 'busy', false);
    if (how !== 'tune') {
      progress(1);
      $('stage').textContent = '';
      setPhase('done');
      reveal();
    }
  }

  async function load(file) {
    if (!file.type.startsWith('image/')) return fail('That file is not an image', 'Use PNG, JPEG, WebP, GIF, BMP or AVIF.');
    if (file.size > MAX_BYTES) return fail('That file is over 100 MB', 'Scale it down and drop it again.');
    const run = ++S.run;
    S.image++;
    if (S.xhr) S.xhr.abort();
    S.file = file; S.id = null; S.shown = null; S.full = null;
    S.stem = (file.name || 'image').replace(/\.[^.]*$/, '').replace(/[^\w .-]+/g, '_').slice(0, 80) || 'image';
    try { await put(before, file); } catch (e) {
      return fail('This browser cannot show that image', 'Use PNG, JPEG, WebP, GIF, BMP or AVIF.');
    }
    if (run !== S.run) return;
    S.big = Math.max(before.naturalWidth, before.naturalHeight) > PREVIEW;
    card.style.setProperty('--ratio', before.naturalWidth / before.naturalHeight);
    show('upload');
  }

  // An image dragged out of another page arrives as its address. The server fetches it, so no other site sees this visitor.
  async function loadUrl(url) {
    const run = ++S.run;
    $('invite-title').textContent = 'Fetching the image';
    flag(root, 'ready', false);
    setPhase('fetching');
    try {
      const r = await fetch('/api/fetch', { method: 'POST', body: url, headers: { 'Content-Type': 'text/plain' } });
      if (run !== S.run) return;
      if (!r.ok) return explain({ status: r.status, ...(await r.json().catch(() => ({}))) });
      const blob = await r.blob();
      if (run !== S.run) return;
      let name = 'image';
      try { name = decodeURIComponent(new URL(url).pathname.split('/').pop()) || name; } catch (e) { /* keep the default */ }
      load(new File([blob], name, { type: blob.type }));
    } catch (e) {
      if (run === S.run) explain({ status: 0 });
    } finally {
      $('invite-title').textContent = 'Drop an image';
    }
  }

  function take(transfer) {
    const files = [...transfer.files];
    const image = files.find((f) => f.type.startsWith('image/'));
    if (image) return load(image);
    if (files.length) return fail('That file is not an image', 'Use PNG, JPEG, WebP, GIF, BMP or AVIF.');
    const text = transfer.getData('text/uri-list') || transfer.getData('text/plain') || '';
    const url = text.split('\n').map((l) => l.trim()).find((l) => /^https?:\/\/\S+$/.test(l));
    if (url) loadUrl(url);
  }

  // The full-size result with the chosen background, for Download and Copy.
  async function fullBlob() {
    const query = { ...settings(), bg: BG[S.bg] };
    const key = JSON.stringify(query), image = S.image;
    if (S.full && S.full.key === key) return S.full.blob;
    // What is on screen already is the full transparent cutout, unless it is a preview or mid-update.
    if (!S.big && S.shown && S.shown.key === key) return S.shown.blob;
    const res = await sendWithImage(query);
    if (image !== S.image) throw { replaced: true };  // another image was dropped while this one was being made
    S.full = { key, blob: res.blob };
    return res.blob;
  }

  function flash(button, label, text, message) {
    label.textContent = text;
    flag(button, 'saved', true);
    note(message, 2500);
    buzz(10);
    setTimeout(() => { label.textContent = button.dataset.label; flag(button, 'saved', false); }, 1900);
  }

  async function download() {
    const button = $('download'), label = $('download-label');
    const name = S.stem + '-nobg.png';
    const slow = setTimeout(() => { label.textContent = 'Preparing'; }, 300);
    try {
      const blob = await fullBlob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 30000);
      flash(button, label, 'Saved', 'Saved ' + a.download);
    } catch (err) {
      label.textContent = button.dataset.label;
      if (!err.replaced) note(err.error || 'The download failed. Try again.', 4000);
    } finally {
      clearTimeout(slow);
    }
  }

  async function copy() {
    const button = $('copy');
    let failed = null;  // set when making the image failed, as opposed to the browser refusing the clipboard
    const blob = fullBlob().catch((err) => { failed = err; throw err; });
    try {
      // Handing over the promise keeps the click's permission alive while the full size is fetched.
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
      flash(button, button, 'Copied', 'Copied to the clipboard');
    } catch (e) {
      if (failed && failed.replaced) return;
      note(failed ? failed.error || 'The copy failed. Try again.' : 'This browser does not allow copying images. Use Download.', 4000);
    }
  }

  function setMode(mode) {
    if (mode === S.mode) return;
    S.mode = mode;
    root.dataset.mode = mode;
    $('mode-ai').setAttribute('aria-pressed', mode === 'ai');
    $('mode-color').setAttribute('aria-pressed', mode === 'color');
    if (mode === 'ai') { $('panel').hidden = true; $('adjust').setAttribute('aria-expanded', 'false'); }
    buzz(4);
    if (S.file && ['working', 'done'].includes(root.dataset.phase)) show(S.id ? 'rerun' : 'upload');
  }

  function retune() {
    clearTimeout(S.tune);
    S.tune = setTimeout(() => { if (root.dataset.phase === 'done') show('tune'); }, 140);
  }

  // Compare line: follows the pointer exactly, and the keyboard through the hidden range input.
  function moveTo(e) {
    const r = card.getBoundingClientRect();
    const v = Math.max(0, Math.min(100, ((e.clientX - r.left) / r.width) * 100));
    if ((v === 0 || v === 100) && v !== S.pos) { buzz(6); note(v === 0 ? 'Original' : 'Cutout', 1500); }
    setPos(v);
  }
  hit.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;  // a right-click opens the menu and never sends the matching pointerup
    cancelAnimationFrame(S.raf);
    flag(root, 'ready', true);
    hit.setPointerCapture(e.pointerId);
    S.dragging = true;
    flag(card, 'dragging', true);
    moveTo(e);
  });
  hit.addEventListener('pointermove', (e) => { if (S.dragging) moveTo(e); });
  const release = () => { S.dragging = false; flag(card, 'dragging', false); };
  hit.addEventListener('pointerup', release);
  hit.addEventListener('pointercancel', release);
  wipe.addEventListener('input', () => setPos(Number(wipe.value)));

  // Files: picker, drop anywhere, paste.
  const pick = () => $('file').click();
  $('choose').addEventListener('click', pick);
  $('retry').addEventListener('click', pick);
  $('new').addEventListener('click', pick);
  $('file').addEventListener('change', (e) => { if (e.target.files[0]) load(e.target.files[0]); e.target.value = ''; });

  let depth = 0;
  const droppable = (e) => [...e.dataTransfer.types].some((t) => t === 'Files' || t === 'text/uri-list');
  const over = (on) => {
    flag(root, 'over', on);
    if (root.dataset.phase === 'empty') $('invite-title').textContent = on ? 'Let go' : 'Drop an image';
  };
  addEventListener('dragenter', (e) => { if (droppable(e)) { depth++; over(true); } });
  addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) over(false); });
  addEventListener('dragover', (e) => { if (droppable(e)) e.preventDefault(); });
  addEventListener('drop', (e) => { e.preventDefault(); depth = 0; over(false); take(e.dataTransfer); });
  addEventListener('paste', (e) => { if (e.clipboardData) take(e.clipboardData); });

  // Controls.
  $('mode-ai').addEventListener('click', () => setMode('ai'));
  $('mode-color').addEventListener('click', () => setMode('color'));
  for (const b of document.querySelectorAll('.swatches button')) {
    b.addEventListener('click', () => {
      S.bg = b.dataset.bg;
      root.dataset.bg = S.bg;
      for (const o of document.querySelectorAll('.swatches button')) o.setAttribute('aria-pressed', o === b);
      buzz(4);
    });
  }
  $('adjust').addEventListener('click', () => {
    const open = $('panel').hidden;
    $('panel').hidden = !open;
    $('adjust').setAttribute('aria-expanded', open);
    buzz(4);
  });
  for (const name of ['tolerance', 'smooth']) {
    $(name).addEventListener('input', (e) => {
      $(name + '-value').textContent = e.target.value;
      if (e.target.value === e.target.defaultValue) buzz(4);  // a tick when the slider crosses its default
      retune();
    });
  }
  $('edges').addEventListener('click', (e) => {
    const b = e.currentTarget;
    b.setAttribute('aria-checked', b.getAttribute('aria-checked') !== 'true');
    buzz(4);
    retune();
  });
  $('download').dataset.label = 'Download';
  $('copy').dataset.label = 'Copy';
  $('download').addEventListener('click', download);
  $('copy').addEventListener('click', copy);
})();
