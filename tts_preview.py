#!/usr/bin/env python3
"""Quick preview server for tts_chunks/ and chapters/. Run and open http://localhost:7788"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

CHUNKS_DIR   = Path("tts_chunks_edge")
CHAPTERS_DIR = CHUNKS_DIR / "chapters"
PORT = 7788

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TTS Preview — 艾森霍恩</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0c0b0f; color: #ddd0bc; font-family: Georgia, serif;
         display: flex; flex-direction: column; align-items: center;
         padding: 40px 20px 60px; min-height: 100vh; }
  h1 { font-size: 22px; color: #c8913a; margin-bottom: 4px; }
  h2 { font-size: 15px; color: #c8913a; margin: 28px 0 12px; width: 100%; max-width: 760px; }
  .sub { font-size: 13px; color: #7a7060; margin-bottom: 28px; }

 /* ── Chapter section ── */
  .ch-section { width: 100%; max-width: 760px; background: #13121a;
                 border: 1px solid #2a2838; border-radius: 10px; padding: 18px; margin-bottom: 8px; }
  .ch-player { width: 100%; margin-bottom: 14px; }
  .ch-label { font-family: monospace; font-size: 12px; color: #7a7060; margin-bottom: 10px; }
  .ch-list { display: flex; flex-direction: column; gap: 4px; max-height: 260px; overflow-y: auto; }
  .ch-item { padding: 7px 12px; font-size: 12px; font-family: monospace; color: #7a7060;
             cursor: pointer; border-radius: 5px; border: 1px solid transparent; transition: all 0.12s; }
  .ch-item:hover { background: #1c1b26; color: #ddd0bc; border-color: #2a2838; }
  .ch-item.active { background: rgba(200,145,58,0.1); color: #c8913a; border-color: rgba(200,145,58,0.3); }
  .ch-empty { font-size: 13px; color: #7a7060; font-style: italic; padding: 8px 0; }
  .autoplay-btn { background: none; border: 1px solid #2a2838; color: #7a7060;
                   font-size: 11px; padding: 3px 8px; cursor: pointer; border-radius: 4px;
                   font-family: monospace; transition: all 0.12s; flex-shrink: 0; }
  .autoplay-btn:hover { border-color: #4a4858; }
  .autoplay-btn.on { background: rgba(200,145,58,0.15); border-color: rgba(200,145,58,0.4); color: #c8913a; }

  /* ── Chunk section ── */
  .chunk-section { width: 100%; max-width: 760px; background: #13121a;
                   border: 1px solid #2a2838; border-radius: 10px; padding: 18px; }
  .wav-label { font-family: monospace; font-size: 12px; color: #7a7060; white-space: nowrap; }
  .wav-nav { display: flex; gap: 6px; margin-bottom: 12px; align-items: center; }
  button { background: #1c1b26; border: 1px solid #2a2838; color: #ddd0bc;
           padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  button:hover { border-color: #c8913a; color: #c8913a; }
  button:disabled { opacity: 0.35; cursor: not-allowed; }
  .page-info { font-family: monospace; font-size: 12px; color: #7a7060; }
  .list { display: grid; grid-template-columns: repeat(auto-fill, minmax(58px,1fr)); gap: 5px; }
  .chip { background: #0c0b0f; border: 1px solid #2a2838; border-radius: 6px;
          padding: 6px 4px; text-align: center; cursor: pointer;
          font-family: monospace; font-size: 11px; color: #7a7060; transition: all 0.12s; }
  .chip:hover { border-color: #c8913a; color: #c8913a; }
  .chip.active { background: rgba(200,145,58,0.12); border-color: #c8913a; color: #e0aa55; }

  /* ── Custom audio player ── */
  .cplayer { display: flex; align-items: center; gap: 8px; background: #0c0b0f;
             border: 1px solid #2a2838; border-radius: 8px; padding: 6px 12px; }
  .cplayer audio { display: none; }
  .cplayer .btn { background: none; border: 1px solid transparent; color: #ddd0bc;
                  font-size: 16px; padding: 4px 8px; cursor: pointer; border-radius: 4px;
                  line-height: 1; transition: all 0.12s; flex-shrink: 0; }
  .cplayer .btn:hover { border-color: #2a2838; color: #c8913a; background: #1c1b26; }
  .cplayer .btn:active { background: rgba(200,145,58,0.15); }
  .cplayer .btn:disabled { opacity: 0.35; cursor: not-allowed; }
  .cplayer .seek { flex: 1; -webkit-appearance: none; appearance: none; height: 4px;
                   background: #1c1b26; border-radius: 2px; outline: none; cursor: pointer; }
  .cplayer .seek::-webkit-slider-thumb { -webkit-appearance: none; width: 13px; height: 13px;
                                          background: #c8913a; border-radius: 50%; cursor: pointer;
                                          box-shadow: 0 0 6px rgba(200,145,58,0.4); }
  .cplayer .seek::-moz-range-thumb { width: 13px; height: 13px; background: #c8913a;
                                      border-radius: 50%; border: none; cursor: pointer; }
  .cplayer .time { font-family: monospace; font-size: 15px; color: #9a9080; white-space: nowrap;
                   flex-shrink: 0; }
</style>
</head>
<body>
<h1>艾森霍恩 · TTS Preview</h1>
<p class="sub" id="sub">Loading…</p>

<h2>Chapters</h2>
<div class="ch-section">
  <div class="ch-player">
    <div class="cplayer" id="chCPlayer">
      <audio id="chPlayer"></audio>
      <button class="btn" id="chPlayBtn" onclick="togglePlay('chPlayer')">▶</button>
      <button class="btn" onclick="stopAudio('chPlayer')">⏹</button>
      <button class="autoplay-btn" id="chAutoplayBtn" onclick="toggleAutoplay('ch')">Auto</button>
      <input type="range" class="seek" id="chSeek" min="0" max="1000" value="0" oninput="seekAudio('chPlayer', this.value)">
      <span class="time" id="chTime">0:00 / 0:00</span>
    </div>
  </div>
  <div class="ch-list" id="chList"><div class="ch-empty">No chapters stitched yet…</div></div>
</div>

<h2>Chunks</h2>
<div class="chunk-section">
  <div class="cplayer" id="wavCPlayer">
      <audio id="wavPlayer"></audio>
      <button class="btn" id="wavPlayBtn" onclick="togglePlay('wavPlayer')">▶</button>
      <button class="btn" onclick="stopAudio('wavPlayer')">⏹</button>
      <button class="autoplay-btn" id="wavAutoplayBtn" onclick="toggleAutoplay('wav')">Auto</button>
      <input type="range" class="seek" id="wavSeek" min="0" max="1000" value="0" oninput="seekAudio('wavPlayer', this.value)">
      <span class="time" id="wavTime">0:00 / 0:00</span>
    </div>
  <div class="wav-nav">
    <button id="btnPrev" onclick="step(-1)">◀ Prev</button>
    <button id="btnNext" onclick="step(1)">Next ▶</button>
    <button id="btnPagePrev" onclick="changePage(-1)">◀ Page</button>
    <span class="page-info" id="pageInfo"></span>
    <button id="btnPageNext" onclick="changePage(1)">Page ▶</button>
  </div>
  <div class="list" id="chunkList"></div>
</div>

<script>
const PAGE_SIZE = 120;
const STATE_KEY = 'tts_preview_state';
let chunks = [], chMp3s = [], current = -1, currentPage = 0, waitingForChunk = false, activeCh = -1;
let wavAutoplay = true, chAutoplay = true;

function saveState() {
  const wavPlayer = document.getElementById('wavPlayer');
  const chPlayer = document.getElementById('chPlayer');
  const state = {
    wavIndex: current,
    wavPage: currentPage,
    wavTime: wavPlayer.readyState >= 1 ? wavPlayer.currentTime : 0,
    wavPlaying: !wavPlayer.paused && !wavPlayer.ended,
    wavAutoplay: wavAutoplay,
    chIndex: activeCh,
    chTime: chPlayer.readyState >= 1 ? chPlayer.currentTime : 0,
    chPlaying: !chPlayer.paused && !chPlayer.ended,
    chAutoplay: chAutoplay
  };
  localStorage.setItem(STATE_KEY, JSON.stringify(state));
}

function restoreState() {
  try {
    const raw = localStorage.getItem(STATE_KEY);
    if (!raw) return false;
    const s = JSON.parse(raw);
    if (typeof s.wavIndex === 'number') current = s.wavIndex;
    if (typeof s.wavPage === 'number') currentPage = s.wavPage;
    if (typeof s.chIndex === 'number') activeCh = s.chIndex;
    if (typeof s.wavAutoplay === 'boolean') wavAutoplay = s.wavAutoplay;
    if (typeof s.chAutoplay === 'boolean') chAutoplay = s.chAutoplay;
    return { wavTime: s.wavTime || 0, wavPlaying: !!s.wavPlaying, chTime: s.chTime || 0, chPlaying: !!s.chPlaying };
  } catch { return false; }
}

function renderChapterListHighlight() {
  document.querySelectorAll('.ch-item').forEach(e => e.classList.remove('active'));
  if (activeCh >= 0) {
    const el = document.getElementById('chi-' + activeCh);
    if (el) el.classList.add('active');
  }
}

function loadWavWithoutPlay(idx) {
  if (idx < 0 || idx >= chunks.length) return;
  const c = chunks[idx];
  const player = document.getElementById('wavPlayer');
  player.src = '/wav/' + c.file;
   document.getElementById('btnPrev').disabled = idx === 0;
   document.getElementById('btnNext').disabled = false;
   player.onended = () => { if (wavAutoplay) step(1); };
}

function setupSaveListeners() {
  const wavPlayer = document.getElementById('wavPlayer');
  const chPlayer = document.getElementById('chPlayer');
  ['pause', 'play', 'ended', 'seeked'].forEach(evt => {
    wavPlayer.addEventListener(evt, saveState);
    chPlayer.addEventListener(evt, saveState);
  });
  window.addEventListener('beforeunload', saveState);
}

async function init() {
  const saved = restoreState();
  await Promise.all([refreshChunks(), refreshChapterMp3s()]);
  renderChips();
  renderChapterListHighlight();
  if (saved && current >= 0 && current < chunks.length) {
    loadWavWithoutPlay(current);
    const wavPlayer = document.getElementById('wavPlayer');
    if (wavPlayer.readyState >= 1) {
      wavPlayer.currentTime = saved.wavTime || 0;
      if (saved.wavPlaying) wavPlayer.play();
    } else {
      wavPlayer.addEventListener('loadedmetadata', function onWavReady() {
        wavPlayer.removeEventListener('loadedmetadata', onWavReady);
        wavPlayer.currentTime = saved.wavTime || 0;
        if (saved.wavPlaying) wavPlayer.play();
      });
    }
  }
  if (saved && activeCh >= 0 && activeCh < chMp3s.length) {
    const chPlayer = document.getElementById('chPlayer');
    chPlayer.src = '/mp3/' + encodeURIComponent(chMp3s[activeCh].file);
    if (chPlayer.readyState >= 1) {
      chPlayer.currentTime = saved.chTime || 0;
      if (saved.chPlaying) chPlayer.play();
    } else {
      chPlayer.addEventListener('loadedmetadata', function onChReady() {
        chPlayer.removeEventListener('loadedmetadata', onChReady);
        chPlayer.currentTime = saved.chTime || 0;
        if (saved.chPlaying) chPlayer.play();
      });
    }
  }
  setupSaveListeners();
  setupPlayer('wavPlayer');
  setupPlayer('chPlayer');
  setupAutoplayToggles();
  setInterval(refreshChunks, 8000);
  setInterval(refreshChapterMp3s, 15000);
}

async function refreshChunks() {
  const res = await fetch('/chunks');
  const fresh = await res.json();
  if (fresh.length !== chunks.length) {
    chunks = fresh;
    document.getElementById('sub').textContent = chunks.length + ' chunks available';
    renderChips();
    if (waitingForChunk) playWav(current);
  }
}

async function refreshChapterMp3s() {
  const res = await fetch('/chapter-mp3s');
  const files = await res.json();
  chMp3s = files;
  const el = document.getElementById('chList');
  if (!files.length) {
    el.innerHTML = '<div class="ch-empty">No chapters stitched yet…</div>';
    return;
  }
  el.innerHTML = files.map((f, i) => `
    <div class="ch-item${i === activeCh ? ' active' : ''}" id="chi-${i}" onclick="playChapter(${i})">${f.name}</div>
  `).join('');
}

function playChapter(i) {
  activeCh = i;
  document.querySelectorAll('.ch-item').forEach(e => e.classList.remove('active'));
  const el = document.getElementById('chi-' + i);
  if (el) { el.classList.add('active'); el.scrollIntoView({block:'nearest'}); }
  const f = chMp3s[i];
  const player = document.getElementById('chPlayer');
  player.src = '/mp3/' + encodeURIComponent(f.file);
  player.play().catch(()=>{});
}

function totalPages() { return Math.max(1, Math.ceil(chunks.length / PAGE_SIZE)); }

function renderChips() {
  const list = document.getElementById('chunkList');
  list.innerHTML = '';
  const start = currentPage * PAGE_SIZE;
  chunks.slice(start, start + PAGE_SIZE).forEach((c, i) => {
    const idx = start + i;
    const el = document.createElement('div');
    el.className = 'chip' + (idx === current ? ' active' : '');
    el.id = 'chip-' + idx;
    el.textContent = c.id;
    el.onclick = () => playWav(idx);
    list.appendChild(el);
  });
  const tp = totalPages();
  document.getElementById('pageInfo').textContent = `${currentPage+1}/${tp}  ·  ${chunks.length}`;
  document.getElementById('btnPagePrev').disabled = currentPage === 0;
  document.getElementById('btnPageNext').disabled = currentPage >= tp - 1;
}

function changePage(d) {
  currentPage = Math.max(0, Math.min(totalPages()-1, currentPage+d));
  renderChips();
}

function playWav(idx) {
  if (idx < 0) return;
  if (idx >= chunks.length) {
    waitingForChunk = true; current = idx;
    return;
  }
  waitingForChunk = false;
  const targetPage = Math.floor(idx / PAGE_SIZE);
  if (targetPage !== currentPage) { currentPage = targetPage; renderChips(); }
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  const chip = document.getElementById('chip-' + idx);
  if (chip) { chip.classList.add('active'); chip.scrollIntoView({block:'nearest'}); }
  current = idx;
  const c = chunks[idx];
  const player = document.getElementById('wavPlayer');
  player.src = '/wav/' + c.file;
  player.play().catch(()=>{});
 document.getElementById('btnPrev').disabled = idx === 0;
   document.getElementById('btnNext').disabled = false;
   player.onended = () => { if (wavAutoplay) step(1); };
}

function step(d) { playWav(current + d); }

function stopAudio(id) {
  const p = document.getElementById(id);
  p.pause(); p.currentTime = 0;
  updatePlayer(id);
}

function togglePlay(id) {
  const p = document.getElementById(id);
  if (!p.src || p.src === window.location.href) return;
  if (p.paused) { p.play().catch(()=>{}); } else { p.pause(); }
}

let seekingMap = { wavPlayer: false, chPlayer: false };

function seekAudio(id, val) {
  seekingMap[id] = true;
  const p = document.getElementById(id);
  if (p.duration && !isNaN(p.duration)) {
    p.currentTime = (val / 1000) * p.duration;
  }
  setTimeout(() => { seekingMap[id] = false; }, 100);
}

function fmt(s) {
  s = Math.max(0, Math.floor(s || 0));
  const m = Math.floor(s / 60);
  const sec = String(s % 60).padStart(2, '0');
  return m + ':' + sec;
}

function updatePlayer(id) {
  const p = document.getElementById(id);
  const btn = document.getElementById(id.replace('Player', '') + 'PlayBtn');
  const seek = document.getElementById(id.replace('Player', 'Seek'));
  const timeEl = document.getElementById(id.replace('Player', 'Time'));
  if (btn) btn.textContent = p.paused ? '▶' : '⏸';
  if (seek && p.duration && !p.seeking && !seekingMap[id]) {
    seek.value = (p.currentTime / p.duration) * 1000;
  }
  if (timeEl) timeEl.textContent = fmt(p.currentTime) + ' / ' + fmt(p.duration);
}

function setupPlayer(id) {
  const p = document.getElementById(id);
  p.addEventListener('play', () => updatePlayer(id));
  p.addEventListener('pause', () => updatePlayer(id));
  p.addEventListener('ended', () => updatePlayer(id));
  p.addEventListener('timeupdate', () => updatePlayer(id));
  p.addEventListener('loadedmetadata', () => updatePlayer(id));
}

function toggleAutoplay(which) {
  if (which === 'ch') {
    chAutoplay = !chAutoplay;
    document.getElementById('chAutoplayBtn').classList.toggle('on', chAutoplay);
  } else {
    wavAutoplay = !wavAutoplay;
    document.getElementById('wavAutoplayBtn').classList.toggle('on', wavAutoplay);
  }
  saveState();
}

function setupAutoplayToggles() {
  document.getElementById('chAutoplayBtn').classList.toggle('on', chAutoplay);
  document.getElementById('wavAutoplayBtn').classList.toggle('on', wavAutoplay);
  const chPlayer = document.getElementById('chPlayer');
  chPlayer.addEventListener('ended', () => {
    if (chAutoplay && activeCh >= 0 && activeCh < chMp3s.length - 1) {
      playChapter(activeCh + 1);
    }
  });
}

function resetAll() {
   const wavPlayer = document.getElementById('wavPlayer');
   const chPlayer = document.getElementById('chPlayer');
   wavPlayer.pause(); wavPlayer.currentTime = 0;
   chPlayer.pause(); chPlayer.currentTime = 0;
   current = -1; activeCh = -1; currentPage = 0;
   wavAutoplay = true; chAutoplay = true;
   document.getElementById('chAutoplayBtn').classList.add('on');
   document.getElementById('wavAutoplayBtn').classList.add('on');
   document.getElementById('btnPrev').disabled = true;
   localStorage.removeItem(STATE_KEY);
   renderChips();
   renderChapterListHighlight();
}

init();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def serve_file(self, fpath, content_type):
        if not fpath.exists():
            self.send_response(404)
            self.end_headers()
            return
        data = fpath.read_bytes()
        full_len = len(data)
        range_header = self.headers.get("Range")
        try:
            if range_header:
                match = __import__("re").match(r"bytes=(\d+)-(\d*)", range_header)
                if match:
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else full_len - 1
                    start = max(0, min(start, full_len - 1))
                    end = max(start, min(end, full_len - 1))
                    chunk = data[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Content-Range", f"bytes {start}-{end}/{full_len}")
                    self.end_headers()
                    self.wfile.write(chunk)
                    return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(full_len))
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode())

        elif path == "/chunks":
            files = sorted(CHUNKS_DIR.glob("*.mp3"))
            data = [{"id": int(f.stem) if f.stem.isdigit() else f.stem, "file": f.name} for f in files]
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        elif path == "/chapter-mp3s":
            files = sorted(CHAPTERS_DIR.glob("*.mp3")) if CHAPTERS_DIR.exists() else []
            data = [{"name": f.stem, "file": f.name} for f in files]
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        elif path == "/chaptermap":
            if Path("tts_chapters.json").exists():
                body = Path("tts_chapters.json").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        elif path.startswith("/wav/"):
            self.serve_file(CHUNKS_DIR / path[5:], "audio/mpeg")

        elif path.startswith("/mp3/"):
            self.serve_file(CHAPTERS_DIR / unquote(path[5:]), "audio/mpeg")

        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Preview server → http://localhost:{PORT}")
    print("Ctrl+C to stop")
    server.serve_forever()
