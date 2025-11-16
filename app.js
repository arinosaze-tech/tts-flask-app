// app.js — Theme toggle + Result Card after run + Stage-based progress + Cancel in overlay
// Compatible with your endpoints: /api/save, /api/run (SSE), /api/run-once, /api/list-outputs, /api/activate

let CAP = null;          // TTS capabilities from backend
let LLM_CAP = null;      // LLM capabilities from backend
let PREMIUM = false;     // read from /api/edition or after activation
let BASE_LANGS = [];     // seed for ".txt" mode
let _lastVideoFile = null; // will hold final MP4 filename from logs

// ---------- safe helpers ----------
const qs = (s)=>document.querySelector(s);
const on = (s, ev, fn)=>{ const el=qs(s); if (el) el.addEventListener(ev, fn); };
const gv = (s, def="")=>{ const el=qs(s); return el && el.value!=null ? el.value : def; };
const gb = (s, def=false)=>{ const el=qs(s); return el && typeof el.checked==="boolean" ? !!el.checked : def; };
const gi = (s, def=0)=>{ const v = parseInt(gv(s,"")); return Number.isFinite(v) ? v : def; };

async function jget(url){ const r=await fetch(url); return r.json(); }
async function jpost(url, body){
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body||{})});
  try{ return await r.json(); }catch(_){ return {ok:false, error:"Bad JSON"}; }
}

// ---------- small toast ----------
function toast(msg, type="ok"){
  let host = qs("#toasts");
  if (!host){
    host = document.createElement("div"); host.id = "toasts"; host.className="toasts"; document.body.appendChild(host);
  }
  const el = document.createElement("div"); el.className = `toast ${type}`; el.textContent = msg;
  host.appendChild(el);
  setTimeout(()=>{ el.style.opacity="0"; setTimeout(()=>el.remove(), 220); }, 3200);
}

// ---------- theme (light/dark) ----------
function applyTheme(theme){
  const t = (theme==="light" || theme==="dark") ? theme : (localStorage.getItem("theme") || "dark");
  document.documentElement.setAttribute("data-theme", t);
  localStorage.setItem("theme", t);
}
function toggleTheme(){
  const cur = document.documentElement.getAttribute("data-theme") || "dark";
  applyTheme(cur === "dark" ? "light" : "dark");
}

// ---------- feature toggles / estimates ----------
function isLLM(){ return gv("#content_source","").toLowerCase().includes("generate"); }
function estimateItems(){
  const v = gv("#target_duration","medium").toLowerCase();
  if (v.includes("short")) return 16;
  if (v.includes("long"))  return 40;
  return 28;
}
function updateItemsDisplay(){
  const est = estimateItems();
  const override = gb("#items_override", false);
  let manual = gi("#llm_items_basic", 0); if (!manual || manual<1) manual = est;
  const label = qs("#estimated_items"); if (label) label.textContent = override ? ("Manual items: " + manual) : ("Estimated items: " + est);
}

// ---------- language / TTS ----------
function union(a,b){ const s=new Set(a||[]); (b||[]).forEach(x=>s.add(x)); return Array.from(s); }
function languagesForLLM(){
  if (!CAP) return BASE_LANGS.slice();
  if (!PREMIUM) return ((CAP.gtts && CAP.gtts.codes) || BASE_LANGS).slice().sort();
  const p = gv("#tts_primary","gtts");
  const s = gv("#tts_secondary","gtts");
  const pc = (CAP[p] && CAP[p].codes) || [];
  const sc = (CAP[s] && CAP[s].codes) || [];
  const piper = (CAP.piper && CAP.piper.codes) || [];
  return union(union(pc, sc), piper).sort();
}
function setOptions(sel, list, keep){
  if (!sel) return;
  const prev = typeof keep==="string" ? keep : sel.value;
  sel.innerHTML = "";
  (list||[]).forEach(v=>{ const o=document.createElement("option"); o.value=v; o.textContent=v; sel.appendChild(o); });
  if (list && list.includes(prev)) sel.value=prev; else if (list && list.length) sel.value=list[0];
}
function rebuildLanguageSelects(){
  const codes = isLLM() ? languagesForLLM() : BASE_LANGS.slice();
  setOptions(qs("#primary_lang"), codes);
  setOptions(qs("#secondary_lang"), codes);
  gateProviderOptionsByLanguage();
}
function gateProviderOptionsByLanguage(){
  if (!CAP) return;
  const pSel = qs("#primary_lang"), sSel = qs("#secondary_lang");
  const pLang = pSel ? pSel.value : "en";
  const sLang = sSel ? sSel.value : "fr";
  const piperCaps = new Set((CAP.piper && CAP.piper.codes) || []);
  [qs("#tts_primary"), qs("#tts_secondary")].forEach((sel,idx)=>{
    if (!sel) return;
    Array.from(sel.options).forEach(o=>{
      if (o.value === "piper")          o.disabled = !PREMIUM || !piperCaps.has(idx===0 ? pLang : sLang);
      else if (o.dataset.premium==="1") o.disabled = !PREMIUM;
      else                               o.disabled = false;
    });
    const langHere = idx===0 ? pLang : sLang;
    const piperOption = Array.from(sel.options).find(o=>o.value==="piper");
    if (PREMIUM && langHere==="lb" && piperOption && !piperOption.disabled) {
      if (sel.value === "gtts" || sel.value === "elevenlabs") sel.value = "piper";
    }
    if (sel.selectedOptions && sel.selectedOptions[0] && sel.selectedOptions[0].disabled) sel.value = "gtts";
  });
}

// ---------- badges & LLM Advanced ----------
function updateBadges(){
  const b = document.querySelector("[data-lock-badge]");
  if (!b) return;
  b.textContent = PREMIUM ? "Premium: active" : "Premium: locked";
  b.className   = PREMIUM ? "badge success" : "badge warn";
}
function updateLLMAdvanced(){
  const prov = qs("#llm_provider"), mdl = qs("#llm_model");
  if (!prov || !mdl || !LLM_CAP) return;
  if (!PREMIUM){
    prov.value="openai"; prov.disabled=true;
    setOptions(mdl, ["gpt-4o"]); mdl.disabled=true;
    return;
  }
  prov.disabled=false; mdl.disabled=false;
  if (prov.value==="ollama") setOptions(mdl, (LLM_CAP.ollama && LLM_CAP.ollama.suggested) || ["llama3.1:8b"]);
  else setOptions(mdl, (LLM_CAP.openai && LLM_CAP.openai.premium) || ["gpt-4o","gpt-5"]);
}

// ---------- source toggle ----------
function isGen(){ return isLLM(); }
function contentSourceToggle(){
  const gen = isGen();
  const topic = qs("#llm_topic_basic"); const file = qs("#text_file");
  if (topic) topic.disabled = !gen; if (file) file.disabled = gen;
  const onOv = gb("#items_override", false); const mi = qs("#llm_items_basic");
  if (mi) mi.disabled = !onOv || !gen;
  rebuildLanguageSelects(); updateItemsDisplay();
}

// ---------- files ----------
async function refreshTextFiles(){
  const mode = gv("#mode","vocab"), level = gv("#level","A1");
  const data = await jget(`/api/text-files?mode=${encodeURIComponent(mode)}&level=${encodeURIComponent(level)}`);
  const sel = qs("#text_file"); if (!sel) return;
  sel.innerHTML = ""; (data.files || []).forEach(f=>{ const o=document.createElement("option"); o.value=f; o.textContent=f; sel.appendChild(o); });
}

// ---------- payload ----------
function payloadFromUI(){
  const langs = isLLM() ? languagesForLLM() : BASE_LANGS.slice();
  const bgRaw = gv("#bg_mode","per_sentence").toLowerCase();
  const bg_mode = bgRaw.includes("per") ? "per_sentence" : (bgRaw.includes("static") ? "single" : (bgRaw.includes("none") ? "none" : "per_sentence"));
  const llm_provider = PREMIUM ? gv("#llm_provider","openai") : "openai";
  const llm_model    = PREMIUM ? gv("#llm_model","gpt-4o") : "gpt-4o";
  return {
    content_source: gv("#content_source","From .txt file"),
    use_llm: isLLM(),
    estimated_items: estimateItems(),
    items_override: gb("#items_override", false),
    llm_items_basic: gi("#llm_items_basic", 20),
    llm_topic: gv("#llm_topic_basic",""),

    mode: gv("#mode","vocab").toLowerCase(),
    level: gv("#level","A1"),
    text_file: gv("#text_file","sample.txt"),
    edition: (PREMIUM ? "premium" : "free"),

    enable_bilingual: gb("#enable_bilingual", true),
    primary_lang_idx: (qs("#primary_lang")?.selectedIndex ?? 0),
    secondary_lang_idx: (qs("#secondary_lang")?.selectedIndex ?? 1),
    lang_codes: langs,
    tts_primary: PREMIUM ? gv("#tts_primary","gtts") : "gtts",
    tts_secondary: PREMIUM ? gv("#tts_secondary","gtts") : "gtts",

    vocab_primary: gi("#vocab_primary", 1),
    vocab_secondary: gi("#vocab_secondary", 2),
    vocab_pause_rep: gi("#vocab_pause_rep", 2500),
    vocab_pause_sent: gi("#vocab_pause_sent", 2500),
    scen_primary: gi("#scen_primary", 1),
    scen_secondary: gi("#scen_secondary", 2),
    scen_pause_rep: gi("#scen_pause_rep", 2500),
    scen_pause_sent: gi("#scen_pause_sent", 3500),

    bg_mode, bg_enabled: gb("#bg_enabled", true),
    video_size: gv("#video_size","1920x1080"),
    video_fps: gi("#video_fps", 30),

    llm_provider, llm_model,
  };
}

// ---------- Stage-based progress (English labels) ----------
const STAGES = { prep: 6, input: 14, llm: 22, audioStart: 30, audioEnd: 78, subs: 86, video: 96, done: 100 };

const PROG = {
  val: 0, cap: STAGES.prep, timer: null,
  useLLM: false,
  audio: { idx: 0, total: 0 },

  _el(id){ return document.querySelector(id); },
  _setPercent(p){
    const pct = Math.max(0, Math.min(100, Math.round(p)));
    const el = this._el("#busyPercent") || this._el("#busy_pct");
    if (el) el.textContent = pct + "%";
    const bar = this._el("#busyBar"); if (bar) bar.style.width = pct + "%";
    const ring= this._el("#ringFill"); if (ring) ring.style.setProperty("--p", String(pct));
  },
  _setStage(t){ const s = this._el("#busyStage") || this._el("#busy_stage"); if (s) s.textContent = t || "Processing…"; },

  set(v){ this.val = Math.max(0, Math.min(100, v)); this._setPercent(this.val); },
  to(t){ this.cap = Math.max(this.cap, t); },
  _tick(){
    const rem = this.cap - this.val;
    if (rem > 0.05){
      let step = rem * 0.06;
      if (this.val < 30) step = Math.max(0.12, step);
      else if (this.val < 60) step = Math.max(0.18, step);
      else step = Math.max(0.12, step);
      this.set(this.val + step);
    }
  },

  _estimateAudioTotal(){
    try{
      const est = (typeof estimateItems==="function" ? estimateItems() : 28) || 28;
      const bi  = (qs("#enable_bilingual")?.checked) ? 2 : 1;
      return Math.max(10, est * bi);
    }catch(_){ return 40; }
  },

  start(){
    this.val = 0; this.cap = STAGES.prep; this.set(0);
    this.useLLM = (gv("#content_source","From .txt file").toLowerCase().includes("generate"));
    this.audio.idx = 0; this.audio.total = this._estimateAudioTotal();
    this._setStage("Preparing…");
    if (this.timer) clearInterval(this.timer);
    this.timer = setInterval(()=>this._tick(), 180);
  },
  finish(){
    // smooth tail so it doesn't jump to 92 then hang — logical easing to 100
    this.to(STAGES.video);
    if (this.timer) clearInterval(this.timer);
    const step = ()=>{
      if (this.val >= 99.6){
        setBusy(false);
        // After overlay hides, show latest result card
        setTimeout(showLatestResultCard, 140);
        return;
      }
      const rem = 100 - this.val;
      this.set(this.val + Math.max(0.35, rem * 0.22));
      requestAnimationFrame(step);
    };
    step();
  },

  // stage hooks from log parsing
  onInput(){ this._setStage("Loading input…"); this.to(STAGES.input); },
  onLLMStart(){
    this.useLLM = true; this._setStage("Generating with LLM…");
    const aim = STAGES.llm + 0.01; this.to(aim);
    const limit = STAGES.llm * 0.85;
    const t = setInterval(()=>{
      if (this.val >= limit || this.cap >= limit){ clearInterval(t); return; }
      this.to(Math.min(limit, this.cap + 0.2));
    }, 900);
  },
  onLLMEnd(){ this._setStage("Preparing TTS…"); this.to(STAGES.audioStart); },
  bumpTTS(){
    this.audio.idx += 1;
    if (!this.audio.total) this.audio.total = this._estimateAudioTotal();
    const denom = Math.max(1, this.audio.total * 1.8);
    const frac  = Math.max(0, Math.min(1, this.audio.idx / denom));
    const p     = STAGES.audioStart + frac * (STAGES.audioEnd - STAGES.audioStart);
    this._setStage(`Synthesizing audio (${Math.min(this.audio.idx, this.audio.total)} / ~${this.audio.total})…`);
    this.to(p);
  },
  onSubs(){ this._setStage("Building subtitles…"); this.to(STAGES.subs); },
  onVideo(){
    this._setStage("Compositing video…");
    const lim = STAGES.video * 0.99;
    const t = setInterval(()=>{
      if (this.val >= lim || this.cap >= lim){ clearInterval(t); return; }
      this.to(Math.min(lim, this.cap + 0.18));
    }, 850);
    this.to(STAGES.video - 1);
  },
  onError(){ this._setStage("Error"); this.to(Math.max(this.cap, 92)); }
};

// ---------- SSE / Busy / actions ----------
let _evt = null;
function setBusy(on){
  const panel = qs("#busy");
  if (panel){ panel.classList.toggle("hidden", !on); panel.setAttribute("aria-hidden", on ? "false" : "true"); }
  ["#btnRun","#btnSave","#btnClearCache","#btnClearOutput","#btnActivate"].forEach(id=>{
    const b = qs(id); if (b) b.disabled = !!on;
  });
  const stop = qs("#btnStop"); if (stop) stop.disabled = !on;
  if (on) PROG.start();
}
function stopStream(){
  if (_evt) { try{ _evt.close(); }catch(_){ } _evt=null; }
  setBusy(false);
  toast("Canceled.", "warn");
}

async function saveSettings(){
  const res = await jpost("/api/save", payloadFromUI());
  if (!res.ok) { toast(res.error || "Save failed.", "err"); return; }
  if (res.warnings && res.warnings.length) toast(res.warnings.join("\n"), "warn");
}

// Map logs → stages
function updateStageFromLine(line){
  const L = (line||"").toLowerCase();

  if (/\[error\]/.test(L)){ PROG.onError(); return; }

  // main.py — input & LLM
  if (/\[info\].*using input file/.test(L) || /mode=|level=/.test(L)){ PROG.onInput(); return; }
  if (/\[info\].*requesting llm/.test(L)){ PROG.onLLMStart(); return; }
  if (/llm returned no usable text|strict parse|generated/.test(L)){ PROG.onLLMEnd(); return; }

  // audio_utils.py — TTS items
  if (/\[tts\]\s*selected provider=/.test(L) || /\[tts\].*(gtts ok|piper ok|elevenlabs ok)/.test(L)){
    PROG.bumpTTS(); return;
  }

  // subtitles.py
  if (/subtitle|write_srt|write_ass/.test(L)){ PROG.onSubs(); return; }

  // video_utils.py / ffmpeg
  if (/pixabay|unsplash|slideshow|render|ffmpeg|mux/.test(L)){ PROG.onVideo(); return; }
}

async function runPipeline(){
  if (_evt){ try{ _evt.close(); }catch(_){ } _evt=null; }
  const log = qs("#log"); if (log) log.textContent = "";
  const det = qs("#logDetails"); if (det) det.open = false;         // keep log collapsed by default

  await saveSettings();
  setBusy(true);

  try {
    _evt = new EventSource("/api/run");                               // SSE stream + [DONE] end
    const stopBtn = qs("#btnStop"); if (stopBtn) stopBtn.disabled = false;

    _evt.onopen = ()=> toast("Started.", "ok");

    _evt.onmessage = (e)=>{
      const line = e.data || "";

      // --- NEW: capture final mp4 path from logs ---
      // Example log: [OK] Final video written: C:\...\Output\my_video.mp4
      const m = line.match(/Final video written:\s*(.+\.mp4)/i);
      if (m) {
        const full = m[1].trim().replace(/["']/g, "");
        const base = full.split(/[\\/]/).pop(); // works on Windows & Unix paths
        _lastVideoFile = base;                   // e.g., "my_video.mp4"
      }
      // --- END NEW ---

      // باقی کد قبلی onmessage شما (لاگ، stage، [DONE] و ...) بدون تغییر
      const logEl = document.querySelector("#log");
      if (logEl){ logEl.textContent += line + "\n"; logEl.scrollTop = logEl.scrollHeight; }
      updateStageFromLine(line);

      if (line.startsWith("[DONE]")){
        try{ _evt.close(); }catch(_){}
        _evt=null; PROG.finish(); toast("Finished.", "ok");
        const stopBtn = document.querySelector("#btnStop"); if (stopBtn) stopBtn.disabled = true;
      }
    };


    _evt.onerror = async ()=>{
      if (qs("#log")) { qs("#log").textContent += "[WARN] SSE error; falling back to /api/run-once\n"; qs("#log").scrollTop = qs("#log").scrollHeight; }
      try{
        if (_evt){ _evt.close(); _evt=null; }
        const r = await fetch("/api/run-once");
        const t = await r.text();
        if (qs("#log")) { qs("#log").textContent += t + (t.endsWith("\n")?"":"\n"); qs("#log").scrollTop = qs("#log").scrollHeight; }
        t.split(/\r?\n/).forEach(updateStageFromLine);
        PROG.finish(); toast("Finished (fallback).", "warn");
      }catch(err){
        setBusy(false);
        if (qs("#log")) { qs("#log").textContent += "[ERROR] " + (err?.message || String(err)) + "\n"; qs("#log").scrollTop = qs("#log").scrollHeight; }
        toast("Run failed.", "err");
      }
      const stopBtn = qs("#btnStop"); if (stopBtn) stopBtn.disabled = true;
    };
  } catch (err) {
    try{
      const r = await fetch("/api/run-once");
      const t = await r.text();
      if (qs("#log")) { qs("#log").textContent += t + (t.endsWith("\n")?"":"\n"); qs("#log").scrollTop = qs("#log").scrollHeight; }
      t.split(/\r?\n/).forEach(updateStageFromLine);
      PROG.finish(); toast("Finished (single).", "ok");
    }catch(e){
      setBusy(false);
      if (qs("#log")) { qs("#log").textContent += "[ERROR] " + (e?.message || String(e)) + "\n"; qs("#log").scrollTop = qs("#log").scrollHeight; }
      toast("Run failed.", "err");
    }
  }
}

async function clearCache(){ await jpost("/api/clear-cache",{}); toast("Cache cleared."); }
async function clearOutput(){ await jpost("/api/clear-output",{}); toast("Output cleared."); }
async function listOutputs(){
  const d = await jget("/api/list-outputs");
  const div = qs("#outputs"); if (!div) return;
  div.innerHTML = "";
  (d.items || []).forEach(it=>{
    if (!it.name) return;
    const a = document.createElement("a");
    a.href = "/out/"+encodeURIComponent(it.name);
    a.textContent = it.name + (it.is_dir ? "/" : "");
    div.appendChild(a);
  });
}

// ---------- Result Card (open latest output) ----------
// ---------- Result Card (open the final MP4) ----------
async function showLatestResultCard(){
  try{
    let url = null;

    // 1) Prefer the explicit mp4 from logs (most reliable)
    if (_lastVideoFile && /\.mp4$/i.test(_lastVideoFile)){
      url = "/out/" + encodeURIComponent(_lastVideoFile);
    } else {
      // 2) Fallback: pick the last *.mp4 from list-outputs (not .wav)
      const d = await jget("/api/list-outputs");
      const items = (d && d.items) || [];
      const videos = items.filter(x => x && typeof x.name === "string" && /\.mp4$/i.test(x.name));
      if (videos.length){
        const last = videos[videos.length - 1]; // alphabetical list → take last mp4
        url = "/out/" + encodeURIComponent(last.name);
      }
    }

    if (!url) return; // No mp4 found → do nothing

    // Prepare Result Card
    const host = document.querySelector("#resultCard");
    const link = document.querySelector("#rcLink");
    if (link) {
      const name = decodeURIComponent(url.split("/").pop());
      link.href = url;
      link.textContent = "Open: " + name;
    }
    if (host){
      host.classList.remove("hidden");
      const closer = document.querySelector("#rcClose");
      const hide = ()=> host.classList.add("hidden");
      if (closer){ closer.onclick = hide; }

      // Click anywhere on the card (except the close) → open video
      host.onclick = (ev)=>{
        if (ev.target && (ev.target.id==="rcClose")) return;
        window.open(url, "_blank", "noopener,noreferrer");
      };

      // Auto-hide after 10s
      setTimeout(()=>{ if (!host.classList.contains("hidden")) host.classList.add("hidden"); }, 10000);
    }

    // Refresh outputs list (optional)
    listOutputs();
  }catch(_){}
}


// ---------- Premium activation ----------
async function activatePremium(){
  const code = gv("#premium_code","").trim();
  if (!code) return toast("Enter the premium code.","warn");
  const r = await jpost("/api/activate", {code});
  if (r && r.ok){
    PREMIUM = true; updateBadges(); updateLLMAdvanced(); rebuildLanguageSelects(); updateItemsDisplay();
    const inp = qs("#premium_code"); if (inp){ inp.value=""; inp.disabled=true; inp.placeholder="Premium active"; }
    const b = qs("#btnActivate"); if (b){ b.textContent="Activated"; b.disabled=true; }
    const ed = qs("#edition"); if (ed){ ed.value = "Premium"; }
    toast("Premium unlocked. Enjoy!");
  }else{
    toast((r && r.error) || "Activation failed.","err");
  }
}

// ---------- bind & init ----------
function bind(){
  const pSel = qs("#primary_lang"); if (pSel) BASE_LANGS = Array.from(pSel.options).map(o=>o.value);
  on("#target_duration","change", updateItemsDisplay);
  on("#items_override","change", ()=>{ const mi = qs("#llm_items_basic"); if (mi) mi.disabled = !gb("#items_override") || !isLLM(); updateItemsDisplay(); });
  on("#llm_items_basic","input", updateItemsDisplay);
  on("#content_source","change", contentSourceToggle);
  on("#mode","change", refreshTextFiles);
  on("#level","change", refreshTextFiles);
  on("#tts_primary","change", rebuildLanguageSelects);
  on("#tts_secondary","change", rebuildLanguageSelects);
  on("#primary_lang","change", gateProviderOptionsByLanguage);
  on("#secondary_lang","change", gateProviderOptionsByLanguage);
  on("#llm_provider","change", updateLLMAdvanced);
  on("#btnSave","click", saveSettings);
  on("#btnRun","click", runPipeline);
  on("#btnStop","click", stopStream);
  on("#btnClearCache","click", clearCache);
  on("#btnClearOutput","click", clearOutput);
  on("#btnActivate","click", activatePremium);

  // Cancel inside Busy overlay
  const cancelA = qs("#btnCancel");
  if (cancelA) cancelA.addEventListener("click", stopStream);

  // Theme
  on("#btnTheme","click", toggleTheme);
}
async function init(){
  applyTheme(); // load from localStorage
  bind();
  updateItemsDisplay(); contentSourceToggle(); listOutputs();

  try { CAP = await jget("/api/tts-capabilities"); } catch(e){}
  try { LLM_CAP = await jget("/api/llm-capabilities"); } catch(e){}
  try { const ed = await jget("/api/edition"); PREMIUM = !!(ed && ed.premium_unlocked); } catch(e){}
  updateBadges(); updateLLMAdvanced(); rebuildLanguageSelects();
}
document.addEventListener("DOMContentLoaded", init);

// =========== HAMBURGER MENU ===========
function openMenu(open){
  const btn = document.getElementById("btnHamburger");
  const menu = document.getElementById("hamburgerMenu");
  const bd   = document.getElementById("hamburgerBackdrop");
  if (!btn || !menu || !bd) return;

  if (open){
    btn.setAttribute("aria-expanded","true");
    menu.hidden = false; menu.setAttribute("aria-hidden","false");
    bd.hidden = false;
    // فوکوس آیتم اول
    const first = menu.querySelector(".sheet-item"); if (first) first.focus();
  }else{
    btn.setAttribute("aria-expanded","false");
    menu.hidden = true;  menu.setAttribute("aria-hidden","true");
    bd.hidden = true;
  }
}
function bindHamburger(){
  const btn = document.getElementById("btnHamburger");
  const menu= document.getElementById("hamburgerMenu");
  const bd  = document.getElementById("hamburgerBackdrop");
  if (!btn || !menu || !bd) return;

  btn.addEventListener("click", ()=> openMenu(btn.getAttribute("aria-expanded")!=="true"));
  bd.addEventListener("click", ()=> openMenu(false));
  document.addEventListener("keydown", (e)=>{ if (e.key === "Escape") openMenu(false); });

  // اکشن‌های منو
  menu.addEventListener("click", async (e)=>{
    const el = e.target.closest(".sheet-item"); if (!el) return;
    const act = el.dataset.act; openMenu(false);
    try{
      if      (act === "run")          await runPipeline();
      else if (act === "stop")         stopStream();
      else if (act === "save")         await saveSettings();
      else if (act === "clear-cache")  await clearCache();
      else if (act === "clear-output") await clearOutput();
      else if (act === "theme")        toggleTheme();
      else if (act === "toggle-log"){
        const d = document.getElementById("logDetails");
        if (d){ d.open = !d.open; if (d.open) d.scrollIntoView({behavior:"smooth", block:"start"}); }
      } else if (act === "open") {
        await openLastVideo();
      }
    }catch(_){}
  });
}
// باز کردن آخرین MP4 (همونی که قبلاً برای کارت نتیجه نوشته بودیم)
async function openLastVideo(){
  if (window._lastVideoFile && /\.mp4$/i.test(window._lastVideoFile)){
    window.open("/out/" + encodeURIComponent(window._lastVideoFile), "_blank", "noopener,noreferrer");
    return;
  }
  try{
    const d = await jget("/api/list-outputs");
    const items = (d && d.items) || [];
    const vids = items.filter(x => x && x.name && /\.mp4$/i.test(x.name));
    if (vids.length){
      const last = vids[vids.length - 1];
      window.open("/out/" + encodeURIComponent(last.name), "_blank", "noopener,noreferrer");
    }else{
      toast("No MP4 found yet.", "warn");
    }
  }catch(_){ toast("Could not open outputs.", "err"); }
}

// فعال‌سازی بعد از لود
document.addEventListener("DOMContentLoaded", bindHamburger);

