const $ = id => document.getElementById(id);

// ── State ──
let selectedFile = null;
let currentJobId = null;
let currentES = null;

// ── File input ──
const dropZone = $('dropZone');
const fileInput = $('fileInput');
const btn = $('btnTranslate');

fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) selectFile(fileInput.files[0]);
});
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  const f = e.dataTransfer.files[0];
  if (f && /\.epub$/i.test(f.name)) selectFile(f);
});

async function selectFile(file) {
  selectedFile = file;
  const mb = (file.size / 1024 / 1024).toFixed(2);
  const badge = $('fileBadge');
  badge.textContent = `${file.name}  ·  ${mb} MB`;
  badge.style.display = 'block';
  $('doneCard').classList.remove('visible');
  btn.disabled = false;
  btn.textContent = 'Translate';
  analyzeFile(file);
}

async function analyzeFile(file) {
  $('settingsSummary').textContent = 'analyzing…';
  const form = new FormData();
  form.append('file', file);
  try {
    const res = await fetch('/api/analyze', { method: 'POST', body: form });
    if (!res.ok) return;
    const s = await res.json();
    applyAutoSettings(s);
  } catch {
    $('settingsSummary').textContent = '';
  }
}

function applyAutoSettings(s) {
  const wk = s.total_words >= 1000 ? Math.round(s.total_words / 1000) + 'k' : s.total_words;
  const label = s.is_structured ? 'chapters' : 'words';
  $('settingsSummary').textContent = `${wk} ${label}`;
}

// ── The book: open the cover, flip the leaf ──
const codexEl = $('codex');
const VIEW_KEY = 'codex_view';

function saveView() {
  try {
    sessionStorage.setItem(VIEW_KEY, JSON.stringify({
      open: codexEl.classList.contains('open'),
      flipped: codexEl.classList.contains('flipped'),
      flipped2: codexEl.classList.contains('flipped2'),
    }));
  } catch {}
}

function openBook() { codexEl.classList.add('open'); saveView(); }

$('bookCover').addEventListener('click', openBook);
$('bookCover').addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openBook(); }
});
$('flipToAudio').addEventListener('click', () => { codexEl.classList.add('flipped'); saveView(); });
$('flipToTrans').addEventListener('click', () => { codexEl.classList.remove('flipped'); saveView(); });
$('flipToArchive').addEventListener('click', () => { codexEl.classList.add('flipped2'); saveView(); loadArchive(); });
$('flipToLectorium').addEventListener('click', () => { codexEl.classList.remove('flipped2'); saveView(); });

// restore the saved view (per tab) without replaying the animations
try {
  const v = JSON.parse(sessionStorage.getItem(VIEW_KEY) || '{}');
  if (v.open) {
    codexEl.classList.add('instant', 'open');
    if (v.flipped || v.flipped2) codexEl.classList.add('flipped');
    if (v.flipped2) codexEl.classList.add('flipped2');
    requestAnimationFrame(() => requestAnimationFrame(() => codexEl.classList.remove('instant')));
  }
} catch {}

const SETTINGS = [
  { key: 'Workers', rangeId: 'rangeWorkers', numId: 'numWorkers' },
  { key: 'Tol',     rangeId: 'rangeTol',     numId: 'numTol' },
  { key: 'Block',   rangeId: 'rangeBlock',   numId: 'numBlock' },
];

SETTINGS.forEach(({ rangeId, numId }) => {
  const range = $(rangeId), num = $(numId);
  range.addEventListener('input', () => { num.value = range.value; updateSettingsSummary(); });
  num.addEventListener('input',   () => { range.value = num.value; updateSettingsSummary(); });
});

function updateSettingsSummary() {
  const w = $('numWorkers').value;
  const scribes = w > 1 ? `${w} scribes · ` : '';
  $('settingsSummary').textContent = `${scribes}${$('numBlock').value}¶ blocks · tol ±${$('numTol').value}%`;
}

function getSettings() {
  return {
    workers:           parseInt($('numWorkers').value),
    block_paras:       parseInt($('numBlock').value),
    tolerance_percent: parseInt($('numTol').value),
  };
}

// ── Correction pass toggle ──
$('fixPassToggle').addEventListener('change', () => {
  $('fixPassState').textContent = $('fixPassToggle').checked ? 'On' : 'Off';
});

// ── Base glossary upload ──
let selectedBaseGlossary = null;

$('baseGlossaryFile').addEventListener('change', () => {
  const file = $('baseGlossaryFile').files[0];
  if (!file) return;
  selectedBaseGlossary = file;
  $('baseGlossaryBtnText').textContent = file.name;
  $('baseGlossaryLabel').classList.add('has-file');
  $('baseGlossaryClear').style.display = 'inline-flex';
});

$('baseGlossaryClear').addEventListener('click', () => {
  selectedBaseGlossary = null;
  $('baseGlossaryFile').value = '';
  $('baseGlossaryBtnText').textContent = 'Upload base_glossary.json';
  $('baseGlossaryLabel').classList.remove('has-file');
  $('baseGlossaryClear').style.display = 'none';
});




// ── Submit ──
btn.addEventListener('click', startTranslation);

async function startTranslation() {
  if (!selectedFile) return;

  btn.disabled = true;
  btn.textContent = 'Starting…';
  $('doneCard').classList.remove('visible');
  $('errorCard').classList.remove('visible');
  $('progLog').innerHTML = '';
  $('progFill').classList.remove('complete');
  $('progFill').style.width = '0%';
  $('progCount').textContent = '';
  $('progPct').textContent = '';
  _currentPhase = null;
  setPhase('prepare');

  const settings = getSettings();
  const form = new FormData();
  form.append('file',          selectedFile);
  form.append('base_url',      $('baseUrl').value.trim());
  form.append('model',         $('modelName').value.trim());
  form.append('src_lang',      $('srcLang').value);
  form.append('tgt_lang',      $('tgtLang').value);
  form.append('workers',           settings.workers);
  form.append('block_paras',       settings.block_paras);
  form.append('tolerance_percent', settings.tolerance_percent);
  form.append('fix_pass',          $('fixPassToggle').checked);
  if (selectedBaseGlossary) form.append('base_glossary_file', selectedBaseGlossary);

  try {
    const res = await fetch('/api/translate', { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Server error');
    }
    const { job_id } = await res.json();
    currentJobId = job_id;
    listenProgress(job_id);
  } catch (e) {
    showError(e.message);
  }
}

// ── Phase step indicator ──
const PHASES = ['prepare', 'scout', 'translate', 'generate'];
let _currentPhase = null;

function setPhase(phase) {
  const idx = PHASES.indexOf(phase);
  PHASES.forEach((s, i) => {
    const el = $('step-' + s);
    el.classList.remove('active', 'done');
    if (i < idx)       el.classList.add('done');
    else if (i === idx) el.classList.add('active');
  });

  const wrap = $('progBarWrap');
  const meta = $('progMeta');
  const isCountable = phase === 'translate' || phase === 'scout';
  if (isCountable) {
    wrap.classList.remove('scanning');
    meta.classList.remove('hidden');
    if (phase !== _currentPhase) {
      $('progFill').style.width = '0%';
      $('progCount').textContent = '';
      $('progPct').textContent = '';
    }
  } else {
    wrap.classList.add('scanning');
    meta.classList.add('hidden');
  }
  _currentPhase = phase;
}

// All steps settle to green, bar full, no more pulsing/scanning.
function setPhaseComplete() {
  PHASES.forEach(s => {
    const el = $('step-' + s);
    el.classList.remove('active');
    el.classList.add('done');
  });
  $('progBarWrap').classList.remove('scanning');
  $('progMeta').classList.remove('hidden');
  $('progFill').classList.add('complete');
  $('progFill').style.width = '100%';
  $('progPct').textContent = '100%';
  _currentPhase = null;
}

// ── Stop ──
$('btnStop').addEventListener('click', async () => {
  if (!currentJobId) return;
  $('btnStop').disabled = true;
  $('btnStop').textContent = 'Stopping…';
  try { await fetch(`/api/cancel/${currentJobId}`, { method: 'POST' }); } catch {}
});

// ── SSE Progress ──
function listenProgress(jobId) {
  const es = new EventSource(`/api/progress/${jobId}`);
  currentES = es;

  es.onmessage = e => {
    const ev = JSON.parse(e.data);

    if (ev.type === 'status') {
      setPhase(ev.phase);
      addLog(ev.message);
    }
    else if (ev.type === 'total') {
      $('progCount').textContent = `0 / ${ev.total} chapters`;
    }
    else if (ev.type === 'progress') {
      setPhase(ev.phase);
      const pct = Math.round((ev.done / ev.total) * 100);
      $('progFill').style.width = pct + '%';
      $('progCount').textContent = `${ev.done} / ${ev.total} chapters`;
      $('progPct').textContent = pct + '%';
      const label = ev.phase === 'scout' ? 'scouted' : 'translated';
      addLog(`✓ ${label} chapter: ${ev.chapter}`, 'done');
    }
    else if (ev.type === 'correction') {
      addLog(`✎ corrected chapter: ${ev.chapter}`, 'done');
    }
    else if (ev.type === 'para_count') {
      const ok = ev.src === ev.tgt;
      addLog(`¶ ${ev.chapter}: ${ev.src} → ${ev.tgt}${ok ? '' : ' ⚠ mismatch'}`, ok ? '' : 'err');
    }
    else if (ev.type === 'done') {
      setPhaseComplete();
      const mb = (ev.size / 1024 / 1024).toFixed(2);
      showDone(jobId, ev.filename, mb);
      es.close();
    }
    else if (ev.type === 'cancelled') {
      addLog('Stopped.', 'err');
      resetBtn();
      es.close();
    }
    else if (ev.type === 'error') {
      showError(ev.message);
      es.close();
    }
  };

  es.onerror = () => {
    if (currentJobId === jobId) showError('Connection lost.');
    es.close();
  };
}

function addLog(text, cls = '') {
  const log = $('progLog');
  const line = document.createElement('div');
  line.className = 'log-line' + (cls ? ' ' + cls : '');
  line.textContent = text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function resetBtn() {
  btn.disabled = false;
  btn.textContent = 'Translate';
  $('btnStop').disabled = false;
  $('btnStop').textContent = '■ Halt';
}

function showDone(jobId, filename, mb) {
  codexEl.classList.remove('flipped', 'flipped2');
  saveView();
  resetBtn();
  $('doneCard').classList.add('visible');
  $('doneSub').textContent = `${filename}  ·  ${mb} MB`;
  $('btnDownload').href = `/api/download/${jobId}`;
  addLog(`❦ Opus complete — ${filename} (${mb} MB)`, 'done');
}

function showError(msg) {
  $('errorCard').textContent = '⚠ ' + msg;
  $('errorCard').classList.add('visible');
  resetBtn();
}

// ── Audiobook (TTS) ──
let ttsFileSel = null, ttsJobId = null, ttsPlaylist = [], ttsFiles = [], ttsIndex = -1;
let ttsBook = null;

const TTS_STATE_KEY = 'codex_tts_state';

function ttsState() {
  try { return JSON.parse(localStorage.getItem(TTS_STATE_KEY)) || {}; }
  catch { return {}; }
}

function savePos(book, file, time) {
  if (!book || !file) return;
  const s = ttsState();
  s.books = s.books || {};
  s.books[book] = { file, time: time || 0 };
  localStorage.setItem(TTS_STATE_KEY, JSON.stringify(s));
}

function saveTTSPosition() {
  if (ttsIndex < 0) return;
  savePos(ttsBook, ttsFiles[ttsIndex], $('ttsAudio').currentTime);
}

async function selectTTSBook(book) {
  ttsBook = book;
  ttsIndex = -1;
  await loadTTSLibrary();
  // restore last played chapter & position for this book
  const saved = (ttsState().books || {})[book];
  if (!saved) return;
  const i = ttsFiles.indexOf(saved.file);
  if (i < 0) return;
  ttsIndex = i;
  markActiveChapter(i);
  const a = $('ttsAudio');
  a.src = ttsPlaylist[i];
  a.addEventListener('loadedmetadata', function once() {
    a.removeEventListener('loadedmetadata', once);
    a.currentTime = saved.time || 0;
  });
}

const ttsDropZone = $('ttsDropZone');

function selectTTSFile(f) {
  ttsFileSel = f;
  const badge = $('ttsFileBadge');
  badge.textContent = `${f.name}  ·  ${(f.size / 1024 / 1024).toFixed(2)} MB`;
  badge.style.display = 'block';
  resetTTSBtn();
  selectTTSBook(f.name.replace(/\.[^.]+$/, ''));
}

$('ttsFile').addEventListener('change', () => {
  if ($('ttsFile').files[0]) selectTTSFile($('ttsFile').files[0]);
});
ttsDropZone.addEventListener('dragover', e => { e.preventDefault(); ttsDropZone.classList.add('drag-over'); });
ttsDropZone.addEventListener('dragleave', () => ttsDropZone.classList.remove('drag-over'));
ttsDropZone.addEventListener('drop', e => {
  e.preventDefault();
  ttsDropZone.classList.remove('drag-over');
  const f = e.dataTransfer.files[0];
  if (f && /\.epub$/i.test(f.name)) selectTTSFile(f);
});

$('btnTTS').addEventListener('click', async () => {
  if (!ttsFileSel) return;
  $('btnTTS').disabled = true;
  $('btnTTS').textContent = 'Generating…';
  $('ttsProgressWrap').style.display = '';
  $('ttsLog').innerHTML = '';
  $('ttsFill').classList.remove('complete');
  $('ttsFill').style.width = '0%';
  $('ttsCount').textContent = '';
  $('btnTTSStop').disabled = false;

  const form = new FormData();
  form.append('file', ttsFileSel);
  form.append('voice', $('ttsVoice').value);
  try {
    const res = await fetch('/api/tts', { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Server error');
    }
    const { job_id } = await res.json();
    ttsJobId = job_id;
    listenTTS(job_id);
  } catch (e) {
    ttsLog('⚠ ' + e.message, 'err');
    resetTTSBtn();
  }
});

$('btnTTSStop').addEventListener('click', async () => {
  if (!ttsJobId) return;
  $('btnTTSStop').disabled = true;
  try { await fetch(`/api/cancel/${ttsJobId}`, { method: 'POST' }); } catch {}
});

function listenTTS(jobId) {
  const es = new EventSource(`/api/progress/${jobId}`);
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.type === 'status') {
      ttsLog(ev.message);
    }
    else if (ev.type === 'total') {
      $('ttsCount').textContent = `0 / ${ev.total} chapters`;
    }
    else if (ev.type === 'progress') {
      const pct = Math.round((ev.done / ev.total) * 100);
      $('ttsFill').style.width = pct + '%';
      $('ttsCount').textContent = `${ev.done} / ${ev.total} chapters · ${pct}%`;
    }
    else if (ev.type === 'tts_chapter') {
      ttsLog(`✓ ${ev.name}`, 'done');
      loadTTSLibrary();
      loadArchive();
    }
    else if (ev.type === 'done') {
      const warn = ev.missing ? ` · ${ev.missing} chapters failed — run again to retry` : '';
      $('ttsFill').style.width = '100%';
      if (!ev.missing) $('ttsFill').classList.add('complete');
      $('ttsCount').textContent = `${ev.chapters} / ${ev.chapters} chapters · done`;
      ttsLog(`Audiobook complete — ${ev.chapters} chapters${warn}`, ev.missing ? 'err' : 'done');
      loadTTSLibrary();
      loadArchive();
      resetTTSBtn();
      es.close();
    }
    else if (ev.type === 'cancelled') {
      ttsLog('Stopped.', 'err');
      resetTTSBtn();
      es.close();
    }
    else if (ev.type === 'error') {
      ttsLog('⚠ ' + ev.message, 'err');
      resetTTSBtn();
      es.close();
    }
  };
  es.onerror = () => { es.close(); resetTTSBtn(); };
}

function resetTTSBtn() {
  $('btnTTS').disabled = !ttsFileSel;
  $('btnTTS').textContent = ttsFileSel ? 'Generate Audiobook' : 'Select an EPUB to begin';
  $('btnTTSStop').disabled = false;
}

function ttsLog(text, cls = '') {
  const log = $('ttsLog');
  const line = document.createElement('div');
  line.className = 'log-line' + (cls ? ' ' + cls : '');
  line.textContent = text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

async function loadTTSLibrary() {
  try {
    const res = await fetch('/api/tts/library');
    const books = await res.json();
    const currentFile = ttsIndex >= 0 ? ttsFiles[ttsIndex] : null;
    ttsPlaylist = [];
    ttsFiles = [];
    const el = $('ttsLibrary');
    if (!ttsBook) {
      el.innerHTML = '<div class="tts-empty">Upload an EPUB to open its audiobook.</div>';
      return;
    }
    const book = books.find(b => b.book === ttsBook);
    if (!book || !book.chapters.length) {
      el.innerHTML = '<div class="tts-empty">No chapters voiced yet — the lectern stands silent.</div>';
      return;
    }
    el.innerHTML =
      `<div class="tts-book">${book.book}</div>` +
      book.chapters.map(c => {
        const i = ttsPlaylist.length;
        ttsPlaylist.push(`/api/tts/audio/${encodeURIComponent(book.book)}/${encodeURIComponent(c.file)}`);
        ttsFiles.push(c.file);
        return `<div class="tts-chapter" id="ttsCh-${i}" onclick="playTTS(${i})">${c.name}</div>`;
      }).join('');
    if (currentFile) {
      ttsIndex = ttsFiles.indexOf(currentFile);
      if (ttsIndex >= 0) markActiveChapter(ttsIndex);
    }
  } catch {}
}

function markActiveChapter(i) {
  document.querySelectorAll('.tts-chapter').forEach(e => e.classList.remove('active'));
  const el = $('ttsCh-' + i);
  if (el) { el.classList.add('active'); el.scrollIntoView({ block: 'nearest' }); }
}

function playTTS(i) {
  if (i < 0 || i >= ttsPlaylist.length) return;
  ttsIndex = i;
  markActiveChapter(i);
  const a = $('ttsAudio');
  a.src = ttsPlaylist[i];
  a.play().catch(() => {});
  saveTTSPosition();
}

$('ttsAudio').addEventListener('ended', () => {
  if ($('ttsAutoNext').checked && ttsIndex >= 0 && ttsIndex < ttsPlaylist.length - 1) {
    playTTS(ttsIndex + 1);
  }
});

// ── Voice preview ──
let previewAudio = null;
$('ttsVoicePreview').addEventListener('click', async () => {
  const btn = $('ttsVoicePreview');
  if (previewAudio) { previewAudio.pause(); previewAudio = null; }
  btn.disabled = true;
  previewAudio = new Audio('/api/tts/preview/' + encodeURIComponent($('ttsVoice').value));
  previewAudio.addEventListener('canplay', () => { btn.disabled = false; });
  previewAudio.addEventListener('error', () => { btn.disabled = false; });
  previewAudio.play().catch(() => { btn.disabled = false; });
});

// ── Custom player controls ──
const ttsAudioEl = $('ttsAudio');

$('ttsPlayBtn').addEventListener('click', () => {
  if (!ttsAudioEl.src) {
    if (ttsPlaylist.length) playTTS(0);
    return;
  }
  if (ttsAudioEl.paused) ttsAudioEl.play().catch(() => {});
  else ttsAudioEl.pause();
});

$('ttsSeek').addEventListener('input', () => {
  if (ttsAudioEl.duration) {
    ttsAudioEl.currentTime = ($('ttsSeek').value / 1000) * ttsAudioEl.duration;
  }
});

function fmtTime(s) {
  s = Math.max(0, Math.floor(s || 0));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

let _lastPosSave = 0;

function updatePlayerUI() {
  $('ttsPlayBtn').textContent = ttsAudioEl.paused ? '▶' : '❚❚';
  if (ttsAudioEl.duration && !ttsAudioEl.seeking) {
    $('ttsSeek').value = (ttsAudioEl.currentTime / ttsAudioEl.duration) * 1000;
  }
  $('ttsTime').textContent = fmtTime(ttsAudioEl.currentTime) + ' / ' + fmtTime(ttsAudioEl.duration);
  if (!ttsAudioEl.paused && Date.now() - _lastPosSave > 5000) {
    _lastPosSave = Date.now();
    saveTTSPosition();
  }
}
['play', 'pause', 'ended', 'timeupdate', 'loadedmetadata'].forEach(evt =>
  ttsAudioEl.addEventListener(evt, updatePlayerUI)
);
['pause', 'seeked', 'ended'].forEach(evt =>
  ttsAudioEl.addEventListener(evt, saveTTSPosition)
);

// ── Archivum (page V) — shelf of finished audiobooks · Auditorium (page VI) reader ──
let archBook = null, archPlaylist = [], archFiles = [], archIndex = -1;
const archAudioEl = $('archAudio');

function prettyBook(name) { return name.replace(/_/g, ' '); }

async function loadArchive() {
  try {
    const res = await fetch('/api/tts/library');
    const books = await res.json();
    const shelf = $('archShelf');
    shelf.innerHTML = '';
    if (!books.length) {
      shelf.innerHTML = '<div class="tts-empty">No audiobooks bound yet — the shelf stands empty.</div>';
      return;
    }
    const bookmarks = ttsState().books || {};
    books.forEach(b => {
      const card = document.createElement('div');
      card.className = 'arch-book' + (b.book === archBook ? ' active' : '');
      const title = document.createElement('div');
      title.className = 'arch-book-title';
      title.textContent = prettyBook(b.book);
      title.title = b.book;
      const meta = document.createElement('div');
      meta.className = 'arch-book-meta';
      meta.textContent = `${b.chapters.length} chapters` + (bookmarks[b.book] ? ' · ⟲ bookmarked' : '');
      card.append(title, meta);
      card.addEventListener('click', () => selectArchBook(b));
      shelf.appendChild(card);
    });
    // a book still being voiced grows — keep the open chapter list current
    if (archBook) {
      const open = books.find(b => b.book === archBook);
      if (open) renderArchChapters(open, true);
    }
  } catch {}
}

function selectArchBook(b) {
  if (b.book === archBook) return;
  archAudioEl.pause();
  archAudioEl.removeAttribute('src');
  archBook = b.book;
  archIndex = -1;
  document.querySelectorAll('.arch-book').forEach(el =>
    el.classList.toggle('active', el.querySelector('.arch-book-title').title === b.book));
  renderArchChapters(b, false);
  // restore last played chapter & position for this book
  const saved = (ttsState().books || {})[b.book];
  if (!saved) return;
  const i = archFiles.indexOf(saved.file);
  if (i < 0) return;
  archIndex = i;
  markActiveArch(i);
  archAudioEl.src = archPlaylist[i];
  archAudioEl.addEventListener('loadedmetadata', function once() {
    archAudioEl.removeEventListener('loadedmetadata', once);
    archAudioEl.currentTime = saved.time || 0;
  });
}

function renderArchChapters(b, preserve) {
  const playingFile = preserve && archIndex >= 0 ? archFiles[archIndex] : null;
  archPlaylist = [];
  archFiles = [];
  const list = $('archChapters');
  list.innerHTML = '';
  const head = document.createElement('div');
  head.className = 'tts-book';
  head.textContent = prettyBook(b.book);
  list.appendChild(head);
  b.chapters.forEach((c, i) => {
    archPlaylist.push(`/api/tts/audio/${encodeURIComponent(b.book)}/${encodeURIComponent(c.file)}`);
    archFiles.push(c.file);
    const el = document.createElement('div');
    el.className = 'tts-chapter';
    el.id = 'archCh-' + i;
    el.textContent = c.name;
    el.addEventListener('click', () => playArch(i));
    list.appendChild(el);
  });
  archIndex = playingFile ? archFiles.indexOf(playingFile) : -1;
  if (archIndex >= 0) markActiveArch(archIndex);
}

function markActiveArch(i) {
  document.querySelectorAll('#archChapters .tts-chapter').forEach(e => e.classList.remove('active'));
  const el = $('archCh-' + i);
  if (el) { el.classList.add('active'); el.scrollIntoView({ block: 'nearest' }); }
}

function playArch(i) {
  if (i < 0 || i >= archPlaylist.length) return;
  archIndex = i;
  markActiveArch(i);
  archAudioEl.src = archPlaylist[i];
  archAudioEl.play().catch(() => {});
  saveArchPosition();
}

function saveArchPosition() {
  if (archIndex < 0) return;
  savePos(archBook, archFiles[archIndex], archAudioEl.currentTime);
}

$('archPlayBtn').addEventListener('click', () => {
  if (!archAudioEl.src) {
    if (archPlaylist.length) playArch(Math.max(0, archIndex));
    return;
  }
  if (archAudioEl.paused) archAudioEl.play().catch(() => {});
  else archAudioEl.pause();
});

$('archSeek').addEventListener('input', () => {
  if (archAudioEl.duration) {
    archAudioEl.currentTime = ($('archSeek').value / 1000) * archAudioEl.duration;
  }
});

let _lastArchPosSave = 0;

function updateArchPlayerUI() {
  $('archPlayBtn').textContent = archAudioEl.paused ? '▶' : '❚❚';
  if (archAudioEl.duration && !archAudioEl.seeking) {
    $('archSeek').value = (archAudioEl.currentTime / archAudioEl.duration) * 1000;
  }
  $('archTime').textContent = fmtTime(archAudioEl.currentTime) + ' / ' + fmtTime(archAudioEl.duration);
  if (!archAudioEl.paused && Date.now() - _lastArchPosSave > 5000) {
    _lastArchPosSave = Date.now();
    saveArchPosition();
  }
}
['play', 'pause', 'ended', 'timeupdate', 'loadedmetadata'].forEach(evt =>
  archAudioEl.addEventListener(evt, updateArchPlayerUI)
);
['pause', 'seeked', 'ended'].forEach(evt =>
  archAudioEl.addEventListener(evt, saveArchPosition)
);
archAudioEl.addEventListener('ended', () => {
  if ($('archAutoNext').checked && archIndex >= 0 && archIndex < archPlaylist.length - 1) {
    playArch(archIndex + 1);
  }
});

// one lectern speaks at a time
archAudioEl.addEventListener('play', () => ttsAudioEl.pause());
ttsAudioEl.addEventListener('play', () => archAudioEl.pause());

window.addEventListener('beforeunload', () => { saveTTSPosition(); saveArchPosition(); });

loadTTSLibrary();
loadArchive();
