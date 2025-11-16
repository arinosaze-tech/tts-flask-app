# app.py — Flask web app for the TTS/LLM Video generator (with Premium gate + dynamic TTS caps)
from __future__ import annotations

import os, json, shutil, subprocess, sys, importlib
from pathlib import Path
from typing import Dict, List, Tuple
from flask import Flask, render_template, render_template_string, request, jsonify, Response, send_from_directory, abort

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT                 # project root
TEXT_ROOT = PROJECT_ROOT / "Text"
OUTPUT_DIR = PROJECT_ROOT / "Output"
CACHE_DIRS = [PROJECT_ROOT / ".cache_tts", PROJECT_ROOT / ".cache_images", PROJECT_ROOT / ".cache_video"]

# ---- Base 10 languages (for ".txt" mode) ----
LANG_CODES = ["en","fr","de","es","it","pt","hi","zh-cn","ru","lb"]
LANG_DISPLAY_NAMES = {
    # core 10 + extended common codes
    "en":"English","fr":"French","de":"German","es":"Spanish","it":"Italian","pt":"Portuguese","hi":"Hindi",
    "zh-cn":"Chinese (Simplified)","zh-tw":"Chinese (Traditional)","ru":"Russian","lb":"Luxembourgish",
    "ar":"Arabic","fa":"Persian","tr":"Turkish","ja":"Japanese","ko":"Korean","nl":"Dutch","sv":"Swedish","no":"Norwegian",
    "da":"Danish","fi":"Finnish","pl":"Polish","uk":"Ukrainian","ro":"Romanian","cs":"Czech","sk":"Slovak",
    "el":"Greek","he":"Hebrew","id":"Indonesian","ms":"Malay","vi":"Vietnamese","th":"Thai","bn":"Bengali",
    "ta":"Tamil","hu":"Hungarian","ca":"Catalan","hr":"Croatian","sr":"Serbian","bg":"Bulgarian"
}

# ---- Frontend levels/subdirs ----
LEVELS = ["A1","A2","B1","B2"]
VOCAB_SUBDIR = "Vocab"
SCENARIO_SUBDIR = "Scenario"

# Flask with root folder as template/static so current files work
app = Flask(
    __name__,
    template_folder=str(APP_ROOT),   # serve index.html from project root
    static_folder=str(APP_ROOT),     # serve /static/style.css -> ./style.css, /static/app.js -> ./app.js
    static_url_path="/static"
)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Premium state is in-memory only (default FREE on every fresh start)
app.config["PREMIUM_UNLOCKED"] = False

# ---------------- Utilities ----------------
def ensure_text_subdirs():
    for sub in (VOCAB_SUBDIR, SCENARIO_SUBDIR):
        for lvl in LEVELS:
            (TEXT_ROOT / sub / lvl).mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for d in CACHE_DIRS:
        d.mkdir(parents=True, exist_ok=True)
ensure_text_subdirs()

def list_txt_files(mode: str, level: str) -> List[str]:
    subdir = VOCAB_SUBDIR if (mode or "").lower() == "vocab" else SCENARIO_SUBDIR
    base = TEXT_ROOT / subdir / level
    if not base.exists():
        return []
    return sorted([p.name for p in base.glob("*.txt")])

def _load_settings_module():
    """Prefer settings_temp (GUI), fallback to settings.py"""
    try:
        if (PROJECT_ROOT/"settings_temp.py").exists():
            return importlib.import_module("settings_temp")
    except Exception:
        pass
    return importlib.import_module("settings")

def _is_premium_unlocked() -> bool:
    # non-persistent by design
    return bool(app.config.get("PREMIUM_UNLOCKED", False))

def _set_premium_unlocked(flag: bool) -> None:
    app.config["PREMIUM_UNLOCKED"] = bool(flag)

# ---------------- TTS capability tables ----------------
# Carefully curated, aligned with audio_utils._GTTs_LANG_MAP expansion.
GTTS_CAPS = {
    "af":"Afrikaans","ar":"Arabic","bn":"Bengali","ca":"Catalan","cs":"Czech","da":"Danish","de":"German",
    "el":"Greek","en":"English","es":"Spanish","fi":"Finnish","fr":"French","hi":"Hindi","hu":"Hungarian",
    "id":"Indonesian","it":"Italian","ja":"Japanese","ko":"Korean","ms":"Malay","nl":"Dutch","no":"Norwegian",
    "pl":"Polish","pt":"Portuguese","ro":"Romanian","ru":"Russian","sk":"Slovak","sv":"Swedish","ta":"Tamil",
    "th":"Thai","tr":"Turkish","uk":"Ukrainian","vi":"Vietnamese","zh-cn":"Chinese (Simplified)",
    "zh-tw":"Chinese (Traditional)"
}
ELEVEN_CAPS = {
    # ElevenLabs multilingual v2 broadly supports many; keep same set for UI
    **GTTS_CAPS
}
PIPER_CAPS = {
    # ship Luxembourgish initially; users can extend in settings
    "lb":"Luxembourgish"
}

def _piper_caps() -> Dict[str,str]:
    s = _load_settings_module()
    mp = getattr(s, "PIPER_MODEL_MAP", {}) or {}
    out = {}
    for code in mp.keys():
        out[str(code).lower()] = LANG_DISPLAY_NAMES.get(code, code)
    if not out:
        out = {"lb":"Luxembourgish"}
    return out

# ---------------- routes ----------------
@app.get("/")
def index():
    files = list_txt_files("Vocab","A1")
    # Render minimal Jinja wrapper for index.html (keeps static index.html)
    return render_template("index.html", text_files=files, levels=LEVELS, langs=LANG_CODES)

@app.get("/favicon.ico")
def favicon():
    # avoid 404 noise
    return ("", 204)

@app.get("/api/text-files")
def api_text_files():
    mode = request.args.get("mode","Vocab")
    level= request.args.get("level","A1")
    return jsonify({"files": list_txt_files(mode, level)})

@app.get("/api/tts-capabilities")
def api_tts_capabilities():
    # Piper caps pulled from settings to reflect installed local models
    p_caps = _piper_caps()
    return jsonify({
        "display_names": LANG_DISPLAY_NAMES,
        "gtts":  {"name":"gTTS","premium":0,"codes": sorted(GTTS_CAPS.keys())},
        "elevenlabs":{"name":"ElevenLabs","premium":1,"codes": sorted(ELEVEN_CAPS.keys())},
        "piper": {"name":"Piper (local)","premium":1,"codes": sorted(p_caps.keys())},
    })

@app.get("/api/edition")
def api_edition():
    return jsonify({"premium_unlocked": _is_premium_unlocked()})

@app.post("/api/activate")
def api_activate():
    data = request.json or {}
    code = str(data.get("code","")).strip()
    if code == "12345678":
        _set_premium_unlocked(True)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error":"Invalid code"}), 403

def write_settings_temp(payload: Dict) -> Tuple[bool, List[str]]:
    """Create settings_temp.py based on UI payload. Returns (is_premium, warnings)."""
    warnings: List[str] = []

    # -------- Basic fields --------
    edition_requested = payload.get("edition","free").lower()
    mode    = payload.get("mode","vocab").lower()
    level   = payload.get("level","A1")
    text_fn = payload.get("text_file","sample.txt")

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

    bg_mode    = payload.get("bg_mode","per_sentence")
    bg_enabled = bool(payload.get("bg_enabled", True))
    video_size = payload.get("video_size","1920x1080")
    video_fps  = int(payload.get("video_fps", 30))

    # LLM
    use_llm   = bool(payload.get("use_llm", False))
    llm_topic = payload.get("llm_topic","").strip()
    items_override = bool(payload.get("items_override", False))
    llm_items_basic= int(payload.get("llm_items_basic", 20))
    estimated_items= int(payload.get("estimated_items", 20))
    llm_items = llm_items_basic if items_override else estimated_items

    # Language universe from UI (dynamic for LLM; base for .txt)
    lang_codes: List[str] = payload.get("lang_codes") or LANG_CODES
    lang_codes = [str(c).lower() for c in lang_codes]
    # Clamp indices
    if primary_lang_idx >= len(lang_codes): primary_lang_idx = 0
    if secondary_lang_idx >= len(lang_codes): secondary_lang_idx = min(1, len(lang_codes)-1)

    # TTS per-role (Advanced)
    tts_primary   = (payload.get("tts_primary") or "gtts").lower()
    tts_secondary = (payload.get("tts_secondary") or "gtts").lower()

    # Premium gate (non-persistent default is FREE each run)
    unlocked = _is_premium_unlocked()
    edition = "premium" if (edition_requested == "premium" and unlocked) else "free"
    if edition_requested == "premium" and not unlocked:
        warnings.append("Premium edition requested but not activated; falling back to Free.")
    # In Free, lock providers to gTTS
    if not unlocked:
        if tts_primary != "gtts" or tts_secondary != "gtts":
            warnings.append("Premium TTS providers are locked; using gTTS for both tracks.")
        tts_primary = "gtts"
        tts_secondary = "gtts"

    # Compose provider map
    lang_map_dict = {i: code for i, code in enumerate(lang_codes)}
    primary_code = lang_map_dict.get(primary_lang_idx, "en")
    secondary_code = lang_map_dict.get(secondary_lang_idx, "fr")

    # default route: premium -> elevenlabs (global default); free -> gtts
    default_provider = "elevenlabs" if edition == "premium" else "gtts"
    provider_map = {"default": default_provider}
    provider_map[primary_code] = tts_primary
    provider_map[secondary_code] = tts_secondary

    # For Piper: if user selected Piper for a language that has no local model, revert to default
    if provider_map.get(primary_code) == "piper":
        p_caps = set(_piper_caps().keys())
        if primary_code not in p_caps:
            provider_map[primary_code] = default_provider
            warnings.append(f"Piper has no model for '{primary_code}'; using {default_provider}.")
    if provider_map.get(secondary_code) == "piper":
        p_caps = set(_piper_caps().keys())
        if secondary_code not in p_caps:
            provider_map[secondary_code] = default_provider
            warnings.append(f"Piper has no model for '{secondary_code}'; using {default_provider}.")

    # LLM model policy by edition
    openai_model = "gpt-5" if edition == "premium" else "gpt-4o-mini"

    subdir = VOCAB_SUBDIR if mode == "vocab" else SCENARIO_SUBDIR

    # Render settings_temp.py
    # Keep ALL keys that main.py/audio_utils/video_utils read.
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

# Piper (local) — defaults; can be overridden if you add more models
PIPER_BIN    = 'piper'
PIPER_MODEL  = 'voices/lb_LU-marylux-medium.onnx'
PIPER_CONFIG = 'voices/lb_LU-marylux-medium.onnx.json'
PIPER_MODEL_MAP = {{"lb": PIPER_MODEL}}
PIPER_LENGTH  = 1.0
PIPER_NOISE   = 0.5
PIPER_NOISE_W = 0.5

# ElevenLabs
ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"
ELEVENLABS_VOICE_MAP = {{
  "en": "CwhRBWXzGAHq8TQ4Fs17",
  "fr": "CwhRBWXzGAHq8TQ4Fs17",
  "de": "CwhRBWXzGAHq8TQ4Fs17",
  "es": "CwhRBWXzGAHq8TQ4Fs17",
  "it": "pFZP5JQG7iQjIQuC4Bku",
  "pt": "IKne3meq5aSn9XLyUdCD",
  "hi": "JBFqnCBsd6RMkjVDRZzb",
  "zh-cn": "EXAVITQu4vr4xnSDxMaL",
  "ru": "bIHbv24MWmeRgasZH58o",
  "lb": "bIHbv24MWmeRgasZH58o"
}}

# Subtitles / Font
FONT_NAME = "Segoe UI Semibold"
FONT_SIZE = 60

# Timing — Vocab (ms)
VOCAB_REPEAT = {{
    "primary": {vocab_primary},
    "secondary": {vocab_secondary},
    "pause_rep": {vocab_pause_rep},
    "pause_sent": {vocab_pause_sent}
}}

# Timing — Scenario (ms)
SCENARIO_REPEAT = {{
    "primary": {scen_primary},
    "secondary": {scen_secondary},
    "pause_rep": {scen_pause_rep},
    "pause_sent": {scen_pause_sent}
}}

# Background
BG_MODE   = "{bg_mode}"
BG_ENABLED= {str(bg_enabled)}
VIDEO_SIZE= "{video_size}"
VIDEO_FPS = {video_fps}

# LLM generation
GENERATE_WITH_LLM = {str(use_llm)}
LLM_TOPIC         = {json.dumps(llm_topic, ensure_ascii=False)}
LLM_MODEL         = "{openai_model}"
LLM_ITEMS         = {llm_items}

# FFmpeg fallback (bundled)
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
    try:
        is_premium, warnings = write_settings_temp(data)
        return jsonify({"ok": True, "premium": is_premium, "warnings": warnings})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def spawn_pipeline():
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-u", "main.py"],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return proc

@app.get("/api/run")
def api_run():
    try:
        proc = spawn_pipeline()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    def stream():
        try:
            for line in iter(proc.stdout.readline, ''):
                yield f"data: {line.rstrip()}\n\n"
            yield "data: [INFO] Process finished.\n\n"
        except GeneratorExit:
            pass
        except Exception as ex:
            try:
                yield f"data: [ERR] {ex}\n\n"
            except Exception:
                pass
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

    resp = Response(stream(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp

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
        for p in OUTPUT_DIR.glob("*"):
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            except Exception:
                pass
    return jsonify({"ok": True})

@app.get("/api/list-outputs")
def api_list_outputs():
    items = []
    if OUTPUT_DIR.exists():
        for p in sorted(OUTPUT_DIR.glob("*")):
            items.append({"name": p.name, "is_dir": p.is_dir()})
    return jsonify({"items": items})

@app.get("/download/<path:fname>")
def download_file(fname: str):
    safe = (OUTPUT_DIR / fname).resolve()
    if not safe.exists() or (OUTPUT_DIR not in safe.parents and safe != OUTPUT_DIR):
        return abort(404)
    return send_from_directory(str(OUTPUT_DIR), fname, as_attachment=True)

@app.get("/out/<path:path>")
def out_files(path: str):
    safe = (OUTPUT_DIR / path).resolve()
    if not safe.exists() or (OUTPUT_DIR not in safe.parents and safe != OUTPUT_DIR):
        return abort(404)
    rel = safe.relative_to(OUTPUT_DIR)
    return send_from_directory(str(OUTPUT_DIR), str(rel), as_attachment=False)

if __name__ == "__main__":
    print(">> Flask app running. Open http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
