# app.py — Flask web app (Premium non-persistent + Piper/LB + correct settings_temp schema)
from __future__ import annotations

import sys, json, shutil, subprocess, importlib
from pathlib import Path
from typing import Dict, List, Tuple
from flask import Flask, render_template, request, jsonify, Response, send_from_directory, abort

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT
TEXT_ROOT = PROJECT_ROOT / "Text"
OUTPUT_DIR = PROJECT_ROOT / "Output"
CACHE_DIRS = [PROJECT_ROOT / ".cache_tts", PROJECT_ROOT / ".cache_images", PROJECT_ROOT / ".cache_video"]

BASE_LANGS = ["en","fr","de","es","it","pt","hi","zh-cn","ru","lb"]  # include lb
LANG_DISPLAY = {
    "en":"English","fr":"French","de":"German","es":"Spanish","it":"Italian","pt":"Portuguese","hi":"Hindi",
    "zh-cn":"Chinese (Simplified)","zh-tw":"Chinese (Traditional)","ru":"Russian","lb":"Luxembourgish",
}
LEVELS = ["A1","A2","B1","B2"]
VOCAB_SUBDIR = "Vocab"
SCENARIO_SUBDIR = "Scenario"

app = Flask(
    __name__,
    template_folder=str(APP_ROOT),
    static_folder=str(APP_ROOT),
    static_url_path="/static"
)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["PREMIUM_UNLOCKED"] = False  # always default FREE on each start

def ensure_dirs():
    for sub in (VOCAB_SUBDIR, SCENARIO_SUBDIR):
        for lvl in LEVELS:
            (TEXT_ROOT / sub / lvl).mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for d in CACHE_DIRS:
        d.mkdir(parents=True, exist_ok=True)
ensure_dirs()

def list_txt_files(mode: str, level: str) -> List[str]:
    base = TEXT_ROOT / (VOCAB_SUBDIR if (mode or "").lower()=="vocab" else SCENARIO_SUBDIR) / level
    return sorted([p.name for p in base.glob("*.txt")]) if base.exists() else []

def _is_premium_unlocked() -> bool:
    return bool(app.config.get("PREMIUM_UNLOCKED", False))

def _set_premium_unlocked(flag: bool) -> None:
    app.config["PREMIUM_UNLOCKED"] = bool(flag)

def _load_settings_module():
    try:
        if (PROJECT_ROOT / "settings_temp.py").exists():
            return importlib.import_module("settings_temp")
    except Exception:
        pass
    return importlib.import_module("settings")

# ---- TTS capabilities ----
GTTS_CODES = sorted({
    "af","ar","bn","ca","cs","da","de","el","en","es","fi","fr","hi","hu","id","it","ja","ko",
    "ms","nl","no","pl","pt","ro","ru","sk","sv","ta","th","tr","uk","vi","zh-cn","zh-tw"
})
ELEVEN_CODES = sorted({
    "ar","bn","bg","ca","cs","da","de","el","en","es","fa","fi","fr","he","hi","hu","id","it",
    "ja","ko","ms","nl","no","pl","pt","ro","ru","sk","sv","th","tr","uk","vi","zh-cn","zh-tw"
})
def _piper_codes() -> List[str]:
    try:
        s = _load_settings_module()
        mp = getattr(s, "PIPER_MODEL_MAP", {}) or {}
        codes = [str(k).lower() for k in mp.keys()]
        if "lb" not in codes: codes.append("lb")
        return sorted(set(codes))
    except Exception:
        return ["lb"]

# ---------------- Routes ----------------
@app.get("/")
def index():
    # Render your existing index.html; server-side langs only seed initial selects
    return render_template("index.html", langs=BASE_LANGS, text_files=list_txt_files("vocab","A1"))

@app.get("/api/edition")
def api_edition():
    return jsonify({"premium_unlocked": _is_premium_unlocked()})

@app.post("/api/activate")
def api_activate():
    code = (request.json or {}).get("code","").strip()
    if code == "12345678":
        _set_premium_unlocked(True)
        return jsonify({"ok": True, "premium_unlocked": True})
    return jsonify({"ok": False, "premium_unlocked": _is_premium_unlocked(), "error": "Invalid code"}), 403

@app.get("/api/tts-capabilities")
def api_tts_capabilities():
    return jsonify({
        "display_names": LANG_DISPLAY,
        "gtts": {"name":"gTTS","premium":0,"codes": GTTS_CODES},
        "elevenlabs": {"name":"ElevenLabs","premium":1,"codes": ELEVEN_CODES},
        "piper": {"name":"Piper (local)","premium":1,"codes": _piper_codes()},
    })

@app.get("/api/llm-capabilities")
def api_llm_capabilities():
    return jsonify({
        "providers": ["openai","ollama"],
        "openai": {"free": ["gpt-4o-mini"], "premium": ["gpt-4o-mini","gpt-4o","gpt-5"]},
        "ollama": {"suggested": ["llama3.1:8b","llama3.2:3b","qwen2:7b","phi3:3.8b"]}
    })

@app.get("/api/text-files")
def api_text_files():
    mode = request.args.get("mode","vocab")
    level= request.args.get("level","A1")
    return jsonify({"files": list_txt_files(mode, level)})

# ---------- Strict settings writer expected by main.py ----------
def write_settings_temp(payload: Dict) -> Tuple[bool, List[str]]:
    """
    Creates settings_temp.py with keys:
    - VOCAB_REPEAT, SCENARIO_REPEAT (dicts: primary, secondary, pause_rep, pause_sent)
    - USE_LLM, GENERATE_WITH_LLM, LLM_PROVIDER, OPENAI_MODEL, OLLAMA_MODEL, LLM_MODEL (compat)
    Gating: Free => only gTTS, OpenAI gpt-4o-mini. Premium => Piper/Eleven/OpenAI gpt-5, Ollama allowed.
    """
    warnings: List[str] = []

    # Edition gate
    unlocked = _is_premium_unlocked()
    edition_req = str(payload.get("edition","free")).lower()
    edition = "premium" if (edition_req=="premium" and unlocked) else "free"
    if edition_req == "premium" and not unlocked:
        warnings.append("Premium requested but not activated; falling back to Free.")

    # Basics
    mode    = str(payload.get("mode","vocab")).lower()
    level   = str(payload.get("level","A1"))
    text_fn = str(payload.get("text_file","sample.txt"))

    enable_bilingual   = bool(payload.get("enable_bilingual", True))
    primary_lang_idx   = int(payload.get("primary_lang_idx", 0))
    secondary_lang_idx = int(payload.get("secondary_lang_idx", 1))

    vocab_primary   = int(payload.get("vocab_primary", 1))
    vocab_secondary = int(payload.get("vocab_secondary", 2))
    vocab_pause_rep = int(payload.get("vocab_pause_rep", 2500))
    vocab_pause_sent= int(payload.get("vocab_pause_sent", 2500))

    scen_primary   = int(payload.get("scen_primary", 1))
    scen_secondary = int(payload.get("scen_secondary", 2))
    scen_pause_rep = int(payload.get("scen_pause_rep", 2500))
    scen_pause_sent= int(payload.get("scen_pause_sent", 3500))

    bg_mode    = str(payload.get("bg_mode","per_sentence"))
    bg_enabled = bool(payload.get("bg_enabled", True))
    video_size = str(payload.get("video_size","1920x1080"))
    video_fps  = int(payload.get("video_fps", 30))

    # LLM
    use_llm        = bool(payload.get("use_llm", False))
    llm_topic      = str(payload.get("llm_topic","")).strip()
    items_override = bool(payload.get("items_override", False))
    llm_items_basic= int(payload.get("llm_items_basic", 20))
    estimated_items= int(payload.get("estimated_items", 20))
    llm_items      = llm_items_basic if items_override else estimated_items

    # Languages
    lang_codes = [str(c).lower() for c in (payload.get("lang_codes") or BASE_LANGS)]
    if primary_lang_idx >= len(lang_codes): primary_lang_idx = 0
    if secondary_lang_idx >= len(lang_codes): secondary_lang_idx = min(1, len(lang_codes)-1)

    # TTS providers (per language role)
    tts_primary   = (payload.get("tts_primary") or "gtts").lower()
    tts_secondary = (payload.get("tts_secondary") or "gtts").lower()
    if edition == "free":
        if tts_primary != "gtts" or tts_secondary != "gtts":
            warnings.append("Free edition: Only gTTS is available; other TTS providers will be ignored.")
        tts_primary = "gtts"
        tts_secondary = "gtts"

    # LLM provider/model (gated)
    req_provider = (payload.get("llm_provider") or "openai").lower()
    req_model    = (payload.get("llm_model") or "gpt-4o-mini")
    if edition == "free":
        llm_provider = "openai"; llm_model = "gpt-4o-mini"
    else:
        if req_provider == "ollama":
            llm_provider = "ollama"; llm_model = req_model or "llama3.1:8b"
        else:
            llm_provider = "openai"
            llm_model = req_model if req_model in ("gpt-4o-mini","gpt-4o","gpt-5") else "gpt-4o-mini"

    # Provider routing per language; ensure Piper has model entry for lb by default
    primary_code = lang_codes[primary_lang_idx]
    secondary_code = lang_codes[secondary_lang_idx]
    default_provider = "elevenlabs" if edition == "premium" else "gtts"
    provider_map = {"default": default_provider, primary_code: tts_primary, secondary_code: tts_secondary}

    try:
        s = _load_settings_module(); pmap = getattr(s, "PIPER_MODEL_MAP", {}) or {"lb":"voices/lb_LU-marylux-medium.onnx"}
    except Exception:
        pmap = {"lb":"voices/lb_LU-marylux-medium.onnx"}
    for code in (primary_code, secondary_code):
        if provider_map.get(code) == "piper" and code not in pmap:
            provider_map[code] = default_provider
            warnings.append(f"Piper has no model for '{code}'; using {default_provider}.")

    # ---- render
    subdir = VOCAB_SUBDIR if mode == "vocab" else SCENARIO_SUBDIR
    content = f"""# Auto-generated by Flask webapp
from pathlib import Path

# Input/Output
INPUT_DIR  = Path("Text") / "{subdir}" / "{level}"; INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR = Path("Output"); OUTPUT_DIR.mkdir(exist_ok=True)
INPUT_FILENAME = "{text_fn}"

# Edition / TTS
EDITION      = "{edition}"
TTS_PROVIDER = "gtts"  # global hint; language routing overrides this

# Mode / Level
MODE  = "{mode}"
LEVEL = "{level}"

# Languages (dynamic — order matters)
LANG_MAP = {json.dumps({i:c for i,c in enumerate(lang_codes)}, ensure_ascii=False)}
ENABLE_BILINGUAL   = {str(enable_bilingual)}
PRIMARY_LANG_IDX   = {primary_lang_idx}
SECONDARY_LANG_IDX = {secondary_lang_idx}
PRIMARY_LANG_CODE   = LANG_MAP.get(PRIMARY_LANG_IDX, "en")
SECONDARY_LANG_CODE = LANG_MAP.get(SECONDARY_LANG_IDX, "fr")

# Language → Provider routing
TTS_PROVIDER_MAP = {json.dumps(provider_map, ensure_ascii=False)}

# Piper (local) — default lb
PIPER_BIN    = 'piper'
PIPER_MODEL  = 'voices/lb_LU-marylux-medium.onnx'
PIPER_CONFIG = 'voices/lb_LU-marylux-medium.onnx.json'
PIPER_MODEL_MAP = {{"lb": PIPER_MODEL}}
PIPER_LENGTH  = 1.0
PIPER_NOISE   = 0.5
PIPER_NOISE_W = 0.5

# Timing (dicts expected by main.py)
VOCAB_REPEAT = {{"primary": {vocab_primary}, "secondary": {vocab_secondary}, "pause_rep": {vocab_pause_rep}, "pause_sent": {vocab_pause_sent}}}
SCENARIO_REPEAT = {{"primary": {scen_primary}, "secondary": {scen_secondary}, "pause_rep": {scen_pause_rep}, "pause_sent": {scen_pause_sent}}}

# Background / video
BG_MODE   = "{bg_mode}"
BG_ENABLED= {str(bg_enabled)}
VIDEO_SIZE= "{video_size}"
VIDEO_FPS = {video_fps}

# LLM
USE_LLM           = {str(use_llm)}
GENERATE_WITH_LLM = {str(use_llm)}
LLM_TOPIC         = {json.dumps(llm_topic, ensure_ascii=False)}
LLM_ITEMS         = {llm_items}
LLM_PROVIDER      = "{llm_provider}"
OPENAI_MODEL      = "{llm_model if llm_provider=='openai' else 'gpt-4o-mini'}"
OLLAMA_BASE_URL   = "http://localhost:11434"
OLLAMA_MODEL      = "{llm_model if llm_provider=='ollama' else 'llama3.1:8b'}"
LLM_MODEL         = OPENAI_MODEL if LLM_PROVIDER == "openai" else OLLAMA_MODEL

# FFmpeg fallback
FFMPEG_FALLBACK = r".\\ffmpeg\\bin\\ffmpeg.exe"

# Caches
CACHE_TTS_DIR   = Path(".cache_tts"); CACHE_TTS_DIR.mkdir(exist_ok=True)
CACHE_IMG_DIR   = Path(".cache_images"); CACHE_IMG_DIR.mkdir(exist_ok=True)
CACHE_VIDEO_DIR = Path(".cache_video"); CACHE_VIDEO_DIR.mkdir(exist_ok=True)

APP_ID = "TTS-Video CFR multilingual"
"""
    (PROJECT_ROOT / "settings_temp.py").write_text(content, encoding="utf-8")
    return (edition == "premium"), warnings

@app.post("/api/save")
def api_save():
    data = request.json or {}
    _, warnings = write_settings_temp(data)
    return jsonify({"ok": True, "warnings": warnings})

@app.post("/api/clear-cache")
def api_clear_cache():
    for d in CACHE_DIRS:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    return jsonify({"ok": True})

@app.post("/api/clear-output")
def api_clear_output():
    if OUTPUT_DIR.exists():
        for p in OUTPUT_DIR.iterdir():
            if p.is_file(): p.unlink(missing_ok=True)
            elif p.is_dir(): shutil.rmtree(p, ignore_errors=True)
    return jsonify({"ok": True})

@app.get("/api/list-outputs")
def api_list_outputs():
    items = []
    if OUTPUT_DIR.exists():
        for p in sorted(OUTPUT_DIR.iterdir()):
            items.append({"name": p.name, "is_dir": p.is_dir()})
    return jsonify({"items": items})

@app.get("/api/run")
def api_run():
    def _gen():
        try:
            print("[INFO] Starting..."); sys.stdout.flush()
            proc = subprocess.Popen([sys.executable, "main.py"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT))
            for line in proc.stdout:
                yield f"data: {line.decode('utf-8','ignore').rstrip()}\n\n"
            proc.wait()
            yield "data: [DONE] All done.\n\n"
        except Exception as e:
            yield f"data: [ERROR] {e}\n\n"
    return Response(_gen(), mimetype="text/event-stream")

@app.get("/out/<path:fn>")
def out_file(fn: str):
    p = OUTPUT_DIR/Path(fn).name
    if p.exists() and p.is_file():
        return send_from_directory(str(OUTPUT_DIR), p.name, as_attachment=False)
    abort(404)

if __name__ == "__main__":
    app.run("0.0.0.0", 5000, debug=True)
