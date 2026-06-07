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
  setSetting('Extract',  s.extract_chunk_size,  s.extract_chunk_size + 'w');
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

// ── Settings Panel ──
$('settingsToggle').addEventListener('click', () => {
  const panel = $('settingsPanel');
  const chevron = $('settingsChevron');
  panel.classList.toggle('open');
  chevron.classList.toggle('open');
});

const SETTINGS = [
  { key: 'Workers',     rangeId: 'rangeWorkers',     numId: 'numWorkers',     hintId: 'hintWorkers' },
  { key: 'Chunk',       rangeId: 'rangeChunk',       numId: 'numChunk',       hintId: 'hintChunk'   },
  { key: 'Extract',     rangeId: 'rangeExtract',     numId: 'numExtract',     hintId: 'hintExtract' },
  { key: 'Sample',      rangeId: 'rangeSample',      numId: 'numSample',      hintId: null          },
  { key: 'Overlap',     rangeId: 'rangeOverlap',     numId: 'numOverlap',     hintId: 'hintOverlap' },
  { key: 'Temperature', rangeId: 'rangeTemperature', numId: 'numTemperature', hintId: null          },
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
    workers:            parseInt($('numWorkers').value),
    chunk_size:         parseInt($('numChunk').value),
    extract_chunk_size:  parseInt($('numExtract').value),
    extract_sample_pct:  parseInt($('numSample').value),
    overlap_words:       parseInt($('numOverlap').value),
    temperature:        parseFloat($('numTemperature').value),
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

// ── Glossary upload ──
let selectedGlossary = null;

$('glossaryFile').addEventListener('change', () => {
  const file = $('glossaryFile').files[0];
  if (!file) return;
  selectedGlossary = file;
  $('glossaryBtnText').textContent = file.name;
  $('glossaryLabel').classList.add('has-file');
  $('glossaryClear').style.display = 'inline-flex';
  $('rangeExtract').closest('.setting-row').style.display = 'none';
});

$('glossaryClear').addEventListener('click', () => {
  selectedGlossary = null;
  $('glossaryFile').value = '';
  $('glossaryBtnText').textContent = 'Upload glossary.json';
  $('glossaryLabel').classList.remove('has-file');
  $('glossaryClear').style.display = 'none';
  $('rangeExtract').closest('.setting-row').style.display = '';
});

// ── API key toggle ──
let keyVisible = false;
$('keyToggle').addEventListener('click', () => {
  keyVisible = !keyVisible;
  $('apiKey').type = keyVisible ? 'text' : 'password';
  $('eyePath').setAttribute('d', keyVisible
    ? 'M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24M1 1l22 22'
    : 'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z');
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
  $('progressCard').classList.add('visible');
  $('doneCard').classList.remove('visible');
  $('errorCard').classList.remove('visible');
  $('progLog').innerHTML = '';
  $('progFill').style.width = '0%';
  $('progCount').textContent = '';
  $('progPct').textContent = '';
  setPhase('extract');

  const settings = getSettings();
  const form = new FormData();
  form.append('file',           selectedFile);
  form.append('api_key',        $('apiKey').value.trim());
  form.append('base_url',       $('baseUrl').value.trim());
  form.append('model',          $('modelName').value.trim());
  form.append('src_lang',       $('srcLang').value);
  form.append('tgt_lang',       $('tgtLang').value);
  form.append('output_format',  selectedFormat);
  form.append('workers',        settings.workers);
  form.append('chunk_size',     settings.chunk_size);
  form.append('extract_chunk_size',  settings.extract_chunk_size);
  form.append('extract_sample_pct',  settings.extract_sample_pct);
  form.append('overlap_words',  settings.overlap_words);
  form.append('temperature',    settings.temperature);
  if (selectedBaseGlossary) form.append('base_glossary_file', selectedBaseGlossary);
  if (selectedGlossary) form.append('glossary_file', selectedGlossary);

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
const PHASES = ['extract', 'resolve', 'translate', 'generate'];

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
  const isCountable = phase === 'extract' || phase === 'translate';
  if (isCountable) {
    wrap.classList.remove('scanning');
    meta.classList.remove('hidden');
    $('progFill').style.width = '0%';
    $('progCount').textContent = '';
    $('progPct').textContent = '';
  } else {
    wrap.classList.add('scanning');
    meta.classList.add('hidden');
  }
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
    else if (ev.type === 'done') {
      $('progFill').style.width = '100%';
      setPhase('generate');
      const mb = (ev.size / 1024 / 1024).toFixed(2);
      showDone(jobId, ev.filename, mb);
      es.close();
    }
    else if (ev.type === 'cancelled') {
      addLog('Stopped.', 'err');
      $('progressCard').classList.remove('visible');
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
  $('btnStop').textContent = '■ Stop';
}

function showDone(jobId, filename, mb) {
  $('progressCard').classList.remove('visible');
  $('doneCard').classList.add('visible');
  $('doneSub').textContent = `${filename}  ·  ${mb} MB`;
  $('btnDownload').href = `/api/download/${jobId}`;
  resetBtn();
}

function showError(msg) {
  $('errorCard').textContent = '⚠ ' + msg;
  $('errorCard').classList.add('visible');
  resetBtn();
}
