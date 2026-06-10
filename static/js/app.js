const $ = id => document.getElementById(id);

// ── State ──
let selectedFile = null;
let selectedFormat = 'epub';
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
  if (e.dataTransfer.files[0]) selectFile(e.dataTransfer.files[0]);
});

async function selectFile(file) {
  selectedFile = file;
  const mb = (file.size / 1024 / 1024).toFixed(2);
  const badge = $('fileBadge');
  badge.textContent = `${file.name}  ·  ${mb} MB`;
  badge.style.display = 'block';
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
  setSetting('Workers',  s.workers,            null);
  setSetting('Chunk',    s.chunk_size,          s.chunk_size + 'w');
  setSetting('Overlap',  s.overlap_words,       s.overlap_words + 'w');

  // Hide chunk/overlap settings for structured EPUBs — chapters are natural boundaries
  const chunkRow   = $('rangeChunk').closest('.setting-row');
  const overlapRow = $('rangeOverlap').closest('.setting-row');
  if (s.is_structured) {
    chunkRow.style.display   = 'none';
    overlapRow.style.display = 'none';
  } else {
    chunkRow.style.display   = '';
    overlapRow.style.display = '';
  }

  const wk = s.total_words >= 1000 ? Math.round(s.total_words / 1000) + 'k' : s.total_words;
  const label = s.is_structured ? 'chapters' : 'words';
  $('settingsSummary').textContent = `${wk} ${label} · auto`;
  updateSettingsSummary();
}

// ── Subtabs (Manuscript / Apparatus) ──
function switchPane(paneId) {
  document.querySelectorAll('.subtab').forEach(t =>
    t.classList.toggle('active', t.dataset.pane === paneId));
  document.querySelectorAll('.subpane').forEach(p =>
    p.classList.toggle('active', p.id === paneId));
}
document.querySelectorAll('.subtab').forEach(t =>
  t.addEventListener('click', () => switchPane(t.dataset.pane)));

const SETTINGS = [
  { key: 'Workers', rangeId: 'rangeWorkers', numId: 'numWorkers', hintId: 'hintWorkers' },
  { key: 'Chunk',   rangeId: 'rangeChunk',   numId: 'numChunk',   hintId: 'hintChunk'   },
  { key: 'Overlap', rangeId: 'rangeOverlap', numId: 'numOverlap', hintId: 'hintOverlap' },
];

SETTINGS.forEach(({ rangeId, numId }) => {
  const range = $(rangeId), num = $(numId);
  range.addEventListener('input', () => { num.value = range.value; updateSettingsSummary(); });
  num.addEventListener('input',   () => { range.value = num.value; updateSettingsSummary(); });
});

function setSetting(key, value, hintText) {
  const s = SETTINGS.find(x => x.key === key);
  if (!s) return;
  $(s.rangeId).value = value;
  $(s.numId).value   = value;
  if (s.hintId) $(s.hintId).textContent = hintText ? `Auto: ${hintText}` : `Auto: ${value}`;
}

function updateSettingsSummary() {
  const w = $('numWorkers').value;
  const c = $('numChunk').value;
  $('settingsSummary').textContent = `${w} workers · ${c}w chunks`;
}

function getSettings() {
  return {
    workers:       parseInt($('numWorkers').value),
    chunk_size:    parseInt($('numChunk').value),
    overlap_words: parseInt($('numOverlap').value),
  };
}

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




// ── Format pills ──
document.querySelectorAll('#formatPills .pill').forEach(pill => {
  pill.addEventListener('click', () => {
    document.querySelectorAll('#formatPills .pill').forEach(p => p.classList.remove('active'));
    pill.classList.add('active');
    selectedFormat = pill.dataset.fmt;
  });
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
  form.append('output_format', selectedFormat);
  form.append('workers',       settings.workers);
  form.append('chunk_size',    settings.chunk_size);
  form.append('overlap_words', settings.overlap_words);
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
const PHASES = ['prepare', 'translate', 'generate'];
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
  const isCountable = phase === 'translate';
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
      $('progCount').textContent = `0 / ${ev.total} segments`;
    }
    else if (ev.type === 'progress') {
      const pct = Math.round((ev.done / ev.total) * 100);
      $('progFill').style.width = pct + '%';
      $('progCount').textContent = `${ev.done} / ${ev.total} segments`;
      $('progPct').textContent = pct + '%';
      const label = ev.phase === 'extract' ? 'extracted' : 'translated';
      const loc = ev.chapter ? `chapter: ${ev.chapter}` : `pages ${ev.pages}`;
      addLog(`✓ ${label} ${loc}`, 'done');
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
  switchPane('paneManuscript');
  $('doneCard').classList.add('visible');
  $('doneSub').textContent = `${filename}  ·  ${mb} MB`;
  $('btnDownload').href = `/api/download/${jobId}`;
  addLog(`❦ Opus complete — ${filename} (${mb} MB)`, 'done');
  resetBtn();
}

function showError(msg) {
  switchPane('paneManuscript');
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

function saveTTSPosition() {
  if (!ttsBook || ttsIndex < 0 || !ttsFiles[ttsIndex]) return;
  const s = ttsState();
  s.books = s.books || {};
  s.books[ttsBook] = { file: ttsFiles[ttsIndex], time: $('ttsAudio').currentTime || 0 };
  localStorage.setItem(TTS_STATE_KEY, JSON.stringify(s));
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

$('ttsFile').addEventListener('change', () => {
  const f = $('ttsFile').files[0];
  if (!f) return;
  ttsFileSel = f;
  $('ttsFileName').textContent = f.name;
  $('ttsFileLabel').classList.add('has-file');
  resetTTSBtn();
  selectTTSBook(f.name.replace(/\.[^.]+$/, ''));
});

$('btnTTS').addEventListener('click', async () => {
  if (!ttsFileSel) return;
  $('btnTTS').disabled = true;
  $('btnTTS').textContent = 'Generating…';
  $('ttsProgressWrap').style.display = '';
  $('ttsLog').innerHTML = '';
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
    }
    else if (ev.type === 'done') {
      const warn = ev.missing ? ` · ${ev.missing} chapters failed — run again to retry` : '';
      ttsLog(`Audiobook complete — ${ev.chapters} chapters${warn}`, ev.missing ? 'err' : 'done');
      loadTTSLibrary();
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
window.addEventListener('beforeunload', saveTTSPosition);

loadTTSLibrary();
