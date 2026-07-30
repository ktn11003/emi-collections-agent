/* Browser side of the voice loop.
 *
 *  mic  -> AudioWorklet -> downsample to 16 kHz mono PCM16 -> binary WS frames
 *  WS   -> JSON events   -> transcript, latency HUD, tool ledger, step trace
 *  TTS  -> base64 audio  -> streaming playback that can be flushed on barge-in
 *
 * Two playback paths, picked from the chunk's content type:
 *   audio/mpeg  ->  MediaSource (progressive, lowest latency)
 *   audio/wav   ->  decodeAudioData + AudioContext scheduling
 * Barge-in drops whatever is still buffered, otherwise the bot keeps talking
 * out of the browser's buffer after the server has already stopped sending.
 */
'use strict';

const TARGET_RATE = 16000;
const LATENCY_TARGET_MS = 800;
const LATENCY_LIMIT_MS = 1200;

const $ = (id) => document.getElementById(id);
const state = {
  ws: null,
  loanId: null,
  callId: null,
  audioCtx: null,
  micCtx: null,
  micStream: null,
  worklet: null,
  sink: null,
  calling: false,
  toolRows: 0,
};

/* ------------------------------------------------------------------ helpers */
function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* ------------------------------------------------- streaming audio playback */
class AudioSink {
  constructor() {
    this.mode = null;         // 'mse' | 'webaudio'
    this.audioEl = null;
    this.mediaSource = null;
    this.sourceBuffer = null;
    this.pending = [];        // mp3 chunks waiting for the SourceBuffer
    this.ctx = null;
    this.nextStart = 0;
    this.nodes = new Set();
    this.generation = 0;      // bumped on flush, so late chunks are dropped
  }

  async ensure(contentType) {
    const wantMse =
      contentType.includes('mpeg') &&
      typeof MediaSource !== 'undefined' &&
      MediaSource.isTypeSupported('audio/mpeg');

    if (wantMse && this.mode !== 'mse') {
      await this._initMse();
    } else if (!wantMse && this.mode !== 'webaudio') {
      this._initWebAudio();
    }
  }

  async _initMse() {
    this.mode = 'mse';
    this.audioEl = new Audio();
    this.audioEl.autoplay = true;
    this.mediaSource = new MediaSource();
    this.audioEl.src = URL.createObjectURL(this.mediaSource);
    await new Promise((resolve) => {
      this.mediaSource.addEventListener('sourceopen', resolve, { once: true });
    });
    this.sourceBuffer = this.mediaSource.addSourceBuffer('audio/mpeg');
    this.sourceBuffer.mode = 'sequence';
    this.sourceBuffer.addEventListener('updateend', () => this._drain());
  }

  _initWebAudio() {
    this.mode = 'webaudio';
    this.ctx = state.audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    state.audioCtx = this.ctx;
    this.nextStart = 0;
  }

  async push(bytes, contentType) {
    await this.ensure(contentType);
    if (this.mode === 'mse') {
      this.pending.push(bytes);
      this._drain();
    } else {
      await this._playBuffer(bytes);
    }
  }

  _drain() {
    if (this.mode !== 'mse' || !this.sourceBuffer || this.sourceBuffer.updating) return;
    const next = this.pending.shift();
    if (!next) return;
    try {
      this.sourceBuffer.appendBuffer(next);
    } catch (err) {
      console.warn('appendBuffer failed, falling back to Web Audio', err);
      this._initWebAudio();
      this._playBuffer(next);
    }
  }

  async _playBuffer(bytes) {
    const generation = this.generation;
    let buffer;
    try {
      buffer = await this.ctx.decodeAudioData(bytes.buffer.slice(0));
    } catch (err) {
      console.warn('decodeAudioData failed for this chunk', err);
      return;
    }
    if (generation !== this.generation) return;   // flushed while decoding

    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.ctx.destination);
    const now = this.ctx.currentTime;
    const at = Math.max(now, this.nextStart || now);
    src.start(at);
    this.nextStart = at + buffer.duration;
    this.nodes.add(src);
    src.onended = () => this.nodes.delete(src);
  }

  /** Barge-in: stop immediately and discard everything queued. */
  flush() {
    this.generation++;
    this.pending.length = 0;

    if (this.mode === 'mse' && this.sourceBuffer) {
      try {
        if (this.sourceBuffer.updating) this.sourceBuffer.abort();
        const end = this.audioEl.currentTime;
        const buffered = this.sourceBuffer.buffered;
        if (buffered.length && buffered.end(buffered.length - 1) > end) {
          this.sourceBuffer.remove(end, buffered.end(buffered.length - 1) + 1);
        }
      } catch (err) {
        console.debug('MSE flush', err);
      }
    }

    for (const node of this.nodes) {
      try { node.stop(); } catch { /* already finished */ }
    }
    this.nodes.clear();
    this.nextStart = 0;
  }

  close() {
    this.flush();
    if (this.audioEl) { this.audioEl.pause(); this.audioEl.src = ''; }
    this.mode = null;
    this.sourceBuffer = null;
    this.mediaSource = null;
    this.audioEl = null;
  }
}

/* -------------------------------------------------------- microphone capture
 * An AudioWorklet keeps resampling off the main thread. It is defined inline as
 * a Blob so the demo stays a two-file front end.
 */
const WORKLET_SRC = `
class Downsampler extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.target = options.processorOptions.targetRate;
    this.ratio = sampleRate / this.target;
    this.acc = [];
    this.pos = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;

    // Linear-interpolation decimation to the target rate.
    const out = [];
    let peak = 0;
    for (let i = this.pos; i < ch.length; i += this.ratio) {
      const i0 = Math.floor(i), frac = i - i0;
      const a = ch[i0] ?? 0, b = ch[i0 + 1] ?? a;
      const s = a + (b - a) * frac;
      out.push(s);
      const abs = Math.abs(s);
      if (abs > peak) peak = abs;
    }
    this.pos = (this.pos + Math.ceil(ch.length / this.ratio) * this.ratio) - ch.length;
    if (this.pos < 0) this.pos = 0;

    const pcm = new Int16Array(out.length);
    for (let i = 0; i < out.length; i++) {
      const v = Math.max(-1, Math.min(1, out[i]));
      pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    this.port.postMessage({ pcm: pcm.buffer, peak }, [pcm.buffer]);
    return true;
  }
}
registerProcessor('downsampler', Downsampler);
`;

async function startMic(onPcm, onLevel) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,   // stops the bot's own voice re-triggering VAD
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' }));
  await ctx.audioWorklet.addModule(url);
  URL.revokeObjectURL(url);

  const node = new AudioWorkletNode(ctx, 'downsampler', {
    processorOptions: { targetRate: TARGET_RATE },
  });
  node.port.onmessage = (e) => {
    onPcm(e.data.pcm);
    onLevel(e.data.peak);
  };
  ctx.createMediaStreamSource(stream).connect(node);
  // Keep the graph alive without echoing the mic to the speakers.
  node.connect(ctx.createGain()).connect(ctx.destination);

  return { ctx, stream, node };
}

/* ------------------------------------------------------------------ UI paint */
function addMessage(speaker, text, extra) {
  const box = $('transcript');
  const node = el('div', `msg ${speaker}`);
  const tag = el('span', 'tag', speaker === 'BOT' ? 'Priya · agent' : speaker.toLowerCase());
  if (extra) tag.textContent += ` · ${extra}`;
  node.appendChild(tag);
  node.appendChild(document.createTextNode(text));
  box.appendChild(node);
  box.scrollTop = box.scrollHeight;
  return node;
}

function addSystem(text, bad) {
  const box = $('transcript');
  box.appendChild(el('div', `msg SYSTEM${bad ? ' bad' : ''}`, text));
  box.scrollTop = box.scrollHeight;
}

function setState(name) {
  $('statePill').className = `pill ${name}`;
  $('stateText').textContent = name;
}

function setMetric(id, ms, warnAt, badAt) {
  const node = $(id);
  const v = node.querySelector('.v');
  if (ms === undefined || ms === null) { v.innerHTML = '–<small> ms</small>'; return; }
  v.innerHTML = `${Math.round(ms)}<small> ms</small>`;
  node.classList.remove('good', 'warn', 'bad');
  node.classList.add(ms >= badAt ? 'bad' : ms >= warnAt ? 'warn' : 'good');
}

function paintLatency(l) {
  setMetric('mSTT', l.stt_finalisation_ms, 200, 400);
  setMetric('mLLM', l.llm_ttft_ms, 500, 900);
  setMetric('mTTS', l.tts_ttfa_ms, 300, 600);
  const total = l.end_of_speech_to_first_audio_ms;
  setMetric('mTotal', total, LATENCY_TARGET_MS, LATENCY_LIMIT_MS);
  if (total != null) {
    const bar = $('budgetBar');
    bar.style.width = `${Math.min(100, (total / LATENCY_LIMIT_MS) * 100)}%`;
    bar.style.background = total >= LATENCY_LIMIT_MS
      ? 'var(--bad)' : total >= LATENCY_TARGET_MS ? 'var(--warn)' : 'var(--ok)';
  }
}

function paintCompliance(flags) {
  const map = {
    'recording disclosed': 'disclosed_recording',
    'identified self': 'identified_self',
    'in calling window': 'in_window',
    'no threat': 'no_threat',
    'no credential ask': 'no_credential_request',
  };
  $('compliance').innerHTML = '';
  for (const [label, key] of Object.entries(map)) {
    const on = flags[key];
    $('compliance').appendChild(
      el('span', `chip ${on === undefined ? '' : on ? 'on' : 'off'}`,
        `${on === undefined ? '·' : on ? '✓' : '✕'} ${label}`)
    );
  }
}

function addTool(name, ok, detail, replayed) {
  const box = $('tools');
  if (!state.toolRows) box.innerHTML = '';
  state.toolRows++;
  const row = el('div', 'row');
  row.appendChild(el('span', `st ${ok ? 'ok' : 'bad'}`, ok ? '✓' : '✕'));
  row.appendChild(el('span', 'nm', name));
  row.appendChild(el('span', 'dt', (replayed ? '[replayed] ' : '') + detail));
  box.appendChild(row);
  box.scrollTop = box.scrollHeight;
}

function addStep(stage, detail) {
  const box = $('steps');
  const row = el('div', 's');
  row.appendChild(el('span', 'stage', stage));
  row.appendChild(el('span', 'd', detail));
  box.appendChild(row);
  while (box.childElementCount > 300) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

/* -------------------------------------------------------------- call control */
async function startCall() {
  if (state.calling || !state.loanId) return;
  state.calling = true;
  $('btnCall').disabled = true;
  $('btnHang').disabled = false;
  $('transcript').innerHTML = '';
  $('tools').innerHTML = '<div class="row"><span class="dt">no tool calls yet</span></div>';
  $('steps').innerHTML = '';
  $('outcome').innerHTML = '<span class="hint">call in progress…</span>';
  state.toolRows = 0;
  paintLatency({});

  state.sink = new AudioSink();

  const lang = $('langSel').value;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const qs = new URLSearchParams({ loan_id: state.loanId });
  if (lang) qs.set('language', lang);
  const ws = new WebSocket(`${proto}://${location.host}/ws/voice?${qs}`);
  ws.binaryType = 'arraybuffer';
  state.ws = ws;

  ws.onopen = async () => {
    addSystem('Socket open — running pre-call compliance checks…');
    try {
      const mic = await startMic(
        (pcm) => { if (ws.readyState === WebSocket.OPEN) ws.send(pcm); },
        (peak) => { $('micBar').style.width = `${Math.min(100, peak * 160)}%`; }
      );
      state.micCtx = mic.ctx; state.micStream = mic.stream; state.worklet = mic.node;
    } catch (err) {
      addSystem(`Microphone denied: ${err.message}. The bot will still speak; you just cannot reply.`, true);
    }
  };

  ws.onmessage = (evt) => handleEvent(JSON.parse(evt.data));
  ws.onerror = () => addSystem('WebSocket error', true);
  ws.onclose = () => { addSystem('Call disconnected.'); teardown(); };
}

function handleEvent(msg) {
  switch (msg.event) {
    case 'call_started':
      state.callId = msg.call_id;
      addSystem(
        `Call ${msg.call_id.slice(0, 8)} started · ${msg.borrower.name} · ` +
        `₹${msg.borrower.emi_rupees.toLocaleString('en-IN')} · ${msg.language}`
      );
      addStep('call.started', `${msg.correlation_id}`);
      paintCompliance({ in_window: msg.in_window });
      break;

    case 'call_blocked': {
      const reasons = msg.reasons.map((r) => `${r.reason} (${r.regulation})`).join('; ');
      addSystem(`Call blocked by the compliance gate: ${reasons}`, true);
      addStep('dialer.precheck', `BLOCKED — ${reasons}`);
      break;
    }

    case 'transcript':
      addMessage(msg.speaker, msg.text,
        msg.language ? `${msg.language}${msg.confidence ? ` ${(msg.confidence * 100).toFixed(0)}%` : ''}` : '');
      addStep(msg.speaker === 'BOT' ? 'llm.sentence' : 'stt.transcript', msg.text.slice(0, 90));
      break;

    case 'audio':
      if (state.sink) state.sink.push(b64ToBytes(msg.b64), msg.content_type || 'audio/mpeg');
      break;

    case 'vad':
      addStep(msg.signal === 'START_SPEECH' ? 'stt.speech_start' : 'stt.speech_end', `state=${msg.state}`);
      break;

    case 'barge_in':
      if (msg.flush_audio && state.sink) state.sink.flush();
      addSystem('⚡ Barge-in — caller interrupted, TTS cancelled and buffer flushed.');
      addStep('agent.barge_in', 'cancelled TTS + flushed output buffer');
      break;

    case 'state':
      setState(msg.state);
      break;

    case 'latency':
      paintLatency(msg);
      addStep('llm.first_token', `end→audio ${msg.end_of_speech_to_first_audio_ms ?? '–'} ms`);
      break;

    case 'language_switched':
      addSystem(`🌐 Language switch ${msg.from} → ${msg.to} (voice: ${msg.voice})`);
      addStep('stt.transcript', `language switched ${msg.from} → ${msg.to}`);
      break;

    case 'tool_call':
      addStep('llm.tool_call', `${msg.name} ${JSON.stringify(msg.arguments)}`);
      break;

    case 'tool_result':
      addTool(msg.name, msg.ok, msg.error || JSON.stringify(msg.data), msg.replayed);
      addStep('tool.executed', `${msg.name} ok=${msg.ok}`);
      break;

    case 'compliance_block':
      addSystem(`🛡 Guardrail blocked an utterance: ${msg.tags.join(', ')} — substituted a safe line.`, true);
      addStep('compliance.flag', msg.tags.join(','));
      break;

    case 'call_ended':
      paintCompliance(msg.compliance || {});
      renderOutcome(msg);
      addStep('cdr.written', `disposition=${msg.disposition}`);
      setState('Ended');
      break;

    case 'error':
      addSystem(`Error (${msg.source}): ${msg.detail}`, true);
      addStep('error', `${msg.source}: ${msg.detail}`);
      break;
  }
}

function renderOutcome(msg) {
  const l = msg.latency || {};
  $('outcome').innerHTML = `
    <div class="metrics">
      <div class="metric"><div class="k">Disposition</div><div class="v" style="font-size:15px">${msg.disposition}</div></div>
      <div class="metric ${msg.compliance_score >= 100 ? 'good' : 'warn'}">
        <div class="k">Compliance</div><div class="v">${msg.compliance_score ?? '–'}<small> /100</small></div>
      </div>
      <div class="metric"><div class="k">p50 end→audio</div><div class="v">${l.p50_end_to_first_audio_ms ?? '–'}<small> ms</small></div></div>
      <div class="metric"><div class="k">p95 end→audio</div><div class="v">${l.p95_end_to_first_audio_ms ?? '–'}<small> ms</small></div></div>
    </div>
    <div class="row-btns">
      <button id="btnAnalyse">Run post-call analytics</button>
      <a href="/dashboard" style="flex:1"><button style="width:100%">Open dashboard</button></a>
    </div>
    <div id="analysisOut" class="hint" style="margin-top:10px"></div>`;

  $('btnAnalyse').onclick = async () => {
    const out = $('analysisOut');
    out.textContent = 'Running batch STT → diarization → sarvam-105b summary/sentiment → QA scoring…';
    try {
      const r = await fetch(`/api/analytics/${msg.call_id}`, { method: 'POST' });
      const a = await r.json();
      out.innerHTML = `
        <b>Summary:</b> ${a.summary || '–'}<br>
        <b>English:</b> ${a.summary_english || '–'}<br>
        <b>Sentiment:</b> ${a.sentiment} (${a.sentiment_score})<br>
        <b>Predicted disposition:</b> ${a.disposition_predicted}<br>
        <b>Objections:</b> ${(a.objections || []).join(', ') || '–'}<br>
        <b>QA score:</b> ${a.qa_score}/100`;
    } catch (err) {
      out.textContent = `analytics failed: ${err.message}`;
    }
  };
}

function hangup() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ action: 'hangup' }));
    setTimeout(() => state.ws && state.ws.close(), 2500);
  } else {
    teardown();
  }
}

function teardown() {
  state.calling = false;
  $('btnCall').disabled = !state.loanId;
  $('btnHang').disabled = true;
  $('micBar').style.width = '0%';
  if (state.worklet) { try { state.worklet.disconnect(); } catch {} state.worklet = null; }
  if (state.micStream) { state.micStream.getTracks().forEach((t) => t.stop()); state.micStream = null; }
  if (state.micCtx) { state.micCtx.close(); state.micCtx = null; }
  if (state.sink) { state.sink.close(); state.sink = null; }
}

/* ------------------------------------------------------------------ bootstrap */
async function loadBorrowers() {
  const rows = await (await fetch('/api/borrowers')).json();
  const box = $('borrowers');
  box.innerHTML = '';
  const callable = rows.filter((r) => r.callable).length;
  $('listMeta').textContent = `${callable}/${rows.length} callable`;

  for (const b of rows) {
    const node = el('div', `borrower${b.callable ? '' : ' blocked'}`);
    node.title = b.callable
      ? `${b.phone} · voice ${b.voice}`
      : b.block_reasons.map((r) => `${r.reason} (${r.regulation})`).join('\n');

    const who = el('div', 'who');
    who.appendChild(el('div', 'nm', b.name));
    who.appendChild(el('div', 'meta', `${b.loan_id} · ${b.language}${b.callable ? '' : ' · BLOCKED'}`));
    const amt = el('div', 'amt', `₹${b.emi_rupees.toLocaleString('en-IN')}`);
    amt.appendChild(el('small', null, `${b.dpd} DPD`));
    node.append(who, amt);

    if (b.callable) {
      node.onclick = () => {
        document.querySelectorAll('.borrower').forEach((n) => n.classList.remove('active'));
        node.classList.add('active');
        state.loanId = b.loan_id;
        if (!state.calling) $('btnCall').disabled = false;
      };
    }
    box.appendChild(node);
  }
}

async function loadHealth() {
  const h = await (await fetch('/api/health')).json();
  $('modeBadge').textContent =
    `${h.mode} · db ${h.database} · ${h.llm ?? h.models.llm} · TTS over ${h.tts_transport}`;
  const banner = $('banner');
  if (h.mode === 'offline-mock') {
    banner.innerHTML =
      '<div class="banner warn"><b>Offline mock mode.</b> No SARVAM_API_KEY set, so responses are canned ' +
      'and the audio is a placeholder tone. Add your key to <code>.env</code> and restart for the real models.</div>';
  } else if (!h.in_calling_window && h.enforce_calling_window) {
    banner.innerHTML =
      '<div class="banner bad"><b>Outside the RBI calling window (08:00–19:00 IST).</b> ' +
      'The pre-call gate is refusing every call. Set <code>ENFORCE_CALLING_WINDOW=false</code> in ' +
      '<code>.env</code> to demo anyway — the call record will still show <code>in_window: false</code>.</div>';
  } else if (!h.in_calling_window) {
    banner.innerHTML =
      '<div class="banner warn"><b>Outside the RBI calling window (08:00–19:00 IST), ' +
      'and enforcement is overridden for this demo.</b> Calls will connect, but every ' +
      'CDR will honestly record <code>in_window: false</code> and score below 100 on compliance.</div>';
  }
}

$('btnCall').onclick = startCall;
$('btnHang').onclick = hangup;
loadHealth();
loadBorrowers();
