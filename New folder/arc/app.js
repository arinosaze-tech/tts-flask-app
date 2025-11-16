async function jget(url) { const r = await fetch(url); return r.json(); }
async function jpost(url, body) {
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  return r.json();
}
const qs  = (s)=>document.querySelector(s);

// ---- Global UI state ----
let CAP = null;                 // capabilities from /api/tts-capabilities
let PREMIUM = false;            // premium unlocked server-side
let BASE_LANGS = [];            // the fixed 10 codes used for ".txt" mode

// ---- Helpers ----
function isLLM() { return qs("#content_source").value.toLowerCase().startsWith("generate"); }
function currentLangCodesInSelect(sel) {
  return Array.from(sel.options).map(o => o.value);
}
function setSelectOptions(sel, codes, selected) {
  const want = new Set(codes);
  const names = (CAP && CAP.display_names) || {};
  sel.innerHTML = "";
  codes.forEach(code => {
    const opt = document.createElement("option");
    opt.value = code;
    opt.textContent = `${code}${names[code]?(" — "+names[code]):""}`;
    sel.appendChild(opt);
  });
  if (selected && want.has(selected)) sel.value = selected;
}

function union(a,b){ const s = new Set(a); b.forEach(x => s.add(x)); return Array.from(s); }

// LLM language set: in Free -> only gTTS; in Premium -> union(selected providers) + Piper discoverables
function languagesForLLM() {
  if (!CAP) return BASE_LANGS.slice();
  if (!PREMIUM) {
    return (CAP.gtts && CAP.gtts.codes ? CAP.gtts.codes.slice() : BASE_LANGS.slice()).sort();
  }
  const p = qs("#tts_primary").value;
  const s = qs("#tts_secondary").value;
  const pc = (CAP[p] && CAP[p].codes) || [];
  const sc = (CAP[s] && CAP[s].codes) || [];
  let codes = union(pc, sc);
  // make Piper languages discoverable in Premium (e.g., Luxembourgish)
  codes = union(codes, (CAP.piper && CAP.piper.codes) || []);
  return codes.sort();
}

function rebuildLanguageSelects() {
  const pSel = qs("#primary_lang");
  const sSel = qs("#secondary_lang");
  const oldP = pSel.value, oldS = sSel.value;

  // If LLM -> dynamic; else -> fixed 10
  const codes = isLLM() ? languagesForLLM() : BASE_LANGS.slice();
  setSelectOptions(pSel, codes, oldP);
  setSelectOptions(sSel, codes, (oldS !== oldP || codes.length<2) ? oldS : codes[1] || codes[0]);

  // Provider gating by chosen language
  gateProviderOptionsByLanguage();
}

function gateProviderOptionsByLanguage() {
  if (!CAP) return;
  const pLang = qs("#primary_lang").value;
  const sLang = qs("#secondary_lang").value;

  const piperCaps = new Set((CAP.piper && CAP.piper.codes) || []);

  // primary
  const pSel = qs("#tts_primary");
  Array.from(pSel.options).forEach(o => {
    if (o.value === "piper") {
      o.disabled = !PREMIUM || !piperCaps.has(pLang);
      o.title = o.disabled ? "Premium-only or no Piper model for this language" : "";
    } else if (o.dataset.premium === "1") {
      o.disabled = !PREMIUM;
    } else {
      o.disabled = false;
    }
  });
  // secondary
  const sSel = qs("#tts_secondary");
  Array.from(sSel.options).forEach(o => {
    if (o.value === "piper") {
      o.disabled = !PREMIUM || !piperCaps.has(sLang);
      o.title = o.disabled ? "Premium-only or no Piper model for this language" : "";
    } else if (o.dataset.premium === "1") {
      o.disabled = !PREMIUM;
    } else {
      o.disabled = false;
    }
  });

  // Ensure a selectable value remains selected
  if (pSel.selectedOptions[0]?.disabled) pSel.value = PREMIUM && piperCaps.has(pLang) ? "piper" : "gtts";
  if (sSel.selectedOptions[0]?.disabled) sSel.value = PREMIUM && piperCaps.has(sLang) ? "piper" : "gtts";
}

function updateTTSLockState() {
  const lockBadges = document.querySelectorAll("[data-lock-badge]");
  lockBadges.forEach(el => {
    el.textContent = PREMIUM ? "Premium: active" : "Premium: locked";
    el.className = PREMIUM ? "badge success" : "badge warn";
  });
  // Disable premium provider options + refresh language list
  gateProviderOptionsByLanguage();
  rebuildLanguageSelects();
}

function estimateItems() {
  const dur = qs("#target_duration").value.toLowerCase();
  let est = 20;
  if (dur.includes("short")) est = 16;
  else if (dur.includes("medium")) est = 28;
  else if (dur.includes("long")) est = 40;
  qs("#estimated_items").textContent = "Estimated items: " + est;
  return est;
}
function contentSourceToggle() {
  const _isLLM = isLLM();
  qs("#llm_topic_basic").disabled = !_isLLM;
  qs("#text_file").disabled = _isLLM;
  rebuildLanguageSelects();
}
async function refreshTextFiles() {
  const mode = qs("#mode").value;
  const level= qs("#level").value;
  const data = await jget(`/api/text-files?mode=${encodeURIComponent(mode)}&level=${encodeURIComponent(level)}`);
  const sel = qs("#text_file");
  sel.innerHTML = "";
  (data.files || []).forEach(f => {
    const opt = document.createElement("option"); opt.textContent = f; opt.value = f; sel.appendChild(opt);
  });
}
function payloadFromUI() {
  const langs = isLLM() ? languagesForLLM() : BASE_LANGS.slice();
  return {
    content_source: qs("#content_source").value,
    use_llm: isLLM(),
    llm_topic: qs("#llm_topic_basic").value,
    target_duration: qs("#target_duration").value,
    estimated_items: estimateItems(),
    items_override: qs("#items_override").checked,
    llm_items_basic: parseInt(qs("#llm_items_basic").value || "20"),
    mode: qs("#mode").value.toLowerCase(),
    level: qs("#level").value,
    text_file: qs("#text_file").value || "sample.txt",
    edition: qs("#edition").value.toLowerCase(),
    enable_bilingual: qs("#enable_bilingual").checked,
    primary_lang_idx: qs("#primary_lang").selectedIndex,
    secondary_lang_idx: qs("#secondary_lang").selectedIndex,
    lang_codes: langs,
    tts_primary: PREMIUM ? qs("#tts_primary").value : "gtts",
    tts_secondary: PREMIUM ? qs("#tts_secondary").value : "gtts",
    vocab_primary: parseInt(qs("#vocab_primary").value),
    vocab_secondary: parseInt(qs("#vocab_secondary").value),
    vocab_pause_rep: parseInt(qs("#vocab_pause_rep").value),
    vocab_pause_sent: parseInt(qs("#vocab_pause_sent").value),
    scen_primary: parseInt(qs("#scen_primary").value),
    scen_secondary: parseInt(qs("#scen_secondary").value),
    scen_pause_rep: parseInt(qs("#scen_pause_rep").value),
    scen_pause_sent: parseInt(qs("#scen_pause_sent").value),
    bg_mode: (()=>{
      const v = qs("#bg_mode").value;
      if (v.includes("Per")) return "per_sentence";
      if (v.includes("Static")) return "single";
      return "none";
    })(),
    bg_enabled: qs("#bg_enabled").checked,
    video_size: qs("#video_size").value || "1920x1080",
    video_fps: parseInt(qs("#video_fps").value || "30"),
  };
}
async function saveSettings() {
  const body = payloadFromUI();
  const res = await jpost("/api/save", body);
  if (!res.ok) alert("Save failed: " + (res.error || ""));
  if (res.warnings && res.warnings.length) alert(res.warnings.join("\\n"));
}
async function listOutputs() {
  const data = await jget("/api/list-outputs");
  const div = qs("#outputs"); div.innerHTML = "";
  (data.items || []).forEach(it => {
    if (!it.name) return;
    const a = document.createElement("a");
    a.href = "/out/" + encodeURIComponent(it.name);
    a.textContent = it.name + (it.is_dir ? "/" : "");
    const p = document.createElement("p");
    p.appendChild(a);
    div.appendChild(p);
  });
}
let _evtSrc = null;
function stopStream() {
  if (_evtSrc) { _evtSrc.close(); _evtSrc = null; }
  qs("#btnStop").disabled = true;
}
async function runPipeline() {
  stopStream();
  qs("#log").textContent = "";
  const src = new EventSource("/api/run");
  _evtSrc = src;
  qs("#btnStop").disabled = false;
  src.addEventListener("message", (e)=>{
    qs("#log").textContent += (e.data || "") + "\\n";
  });
  src.addEventListener("error", ()=>{
    stopStream();
  });
}
async function clearCache(){ await jpost("/api/clear-cache",{}); alert("Cache cleared."); }
async function clearOutput(){ await jpost("/api/clear-output",{}); await listOutputs(); alert("Output cleared."); }

async function activatePremium() {
  const code = (qs("#premium_code").value || "").trim();
  if (!code) return alert("Enter the premium code.");
  const res = await jpost("/api/activate", {code});
  if (res.ok) {
    PREMIUM = true;
    updateTTSLockState();
    rebuildLanguageSelects();
  } else {
    alert(res.error || "Activation failed.");
  }
}

function bind() {
  // Save base 10 languages from server-rendered <select>
  BASE_LANGS = currentLangCodesInSelect(qs("#primary_lang"));

  // Bind basics
  qs("#target_duration").addEventListener("change", estimateItems);
  qs("#content_source").addEventListener("change", ()=>{ contentSourceToggle(); });
  qs("#items_override").addEventListener("change", ()=>{ qs("#llm_items_basic").disabled = !qs("#items_override").checked; });
  qs("#mode").addEventListener("change", ()=>{ refreshTextFiles(); });
  qs("#level").addEventListener("change", ()=>{ refreshTextFiles(); });

  // TTS + language dynamics
  qs("#tts_primary").addEventListener("change", ()=>{ rebuildLanguageSelects(); });
  qs("#tts_secondary").addEventListener("change", ()=>{ rebuildLanguageSelects(); });
  qs("#primary_lang").addEventListener("change", ()=>{ gateProviderOptionsByLanguage(); });
  qs("#secondary_lang").addEventListener("change", ()=>{ gateProviderOptionsByLanguage(); });

  // Buttons
  qs("#btnSave").addEventListener("click", saveSettings);
  qs("#btnRun").addEventListener("click", runPipeline);
  qs("#btnStop").addEventListener("click", stopStream);
  qs("#btnClearCache").addEventListener("click", clearCache);
  qs("#btnClearOutput").addEventListener("click", clearOutput);
  qs("#btnActivate").addEventListener("click", activatePremium);
}
async function init() {
  bind();
  estimateItems();
  contentSourceToggle();
  listOutputs();

  // Load caps + edition
  try {
    CAP = await jget("/api/tts-capabilities");
  } catch (e) {}
  try {
    const ed = await jget("/api/edition");
    PREMIUM = !!(ed && ed.premium_unlocked);
  } catch (e) {}
  updateTTSLockState();
  rebuildLanguageSelects();
}
document.addEventListener("DOMContentLoaded", init);
