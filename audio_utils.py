# audio_utils.py
# -------------------------------------------------------------
# Unified audio utilities for gTTS, ElevenLabs, Piper
# - Robust logs + safe fallbacks (never silent-crash)
# - Piper binary auto-detect (piper.exe / piper)
# - Uses TTS_PROVIDER_MAP from settings_temp.py if present
# -------------------------------------------------------------

from __future__ import annotations
import io, os, json, hashlib, subprocess, tempfile, shutil
from pathlib import Path
from typing import Optional, Dict, Any, List

# Prefer GUI settings_temp; fallback to settings.py
try:
    import settings_temp as _s
except Exception:
    import settings as _s

# Optional deps
try:
    import requests
except Exception:
    requests = None
try:
    from gtts import gTTS
except Exception:
    gTTS = None
try:
    from pydub import AudioSegment
except Exception:
    AudioSegment = None  # main.py checks this and exits if missing

# -----------------------------
# Constants / defaults
# -----------------------------
PAUSE_REP   = 800  # ms between repeats (fallback)
PAUSE_SENT  = 400
SAMPLE_RATE  = int(getattr(_s, "SAMPLE_RATE", 48000))
CHANNELS     = int(getattr(_s, "CHANNELS", 2))
SAMPLE_WIDTH = int(getattr(_s, "SAMPLE_WIDTH", 2))

CACHE_TTS_DIR = getattr(_s, "CACHE_TTS_DIR", Path(".cache_tts"))
if isinstance(CACHE_TTS_DIR, str):
    CACHE_TTS_DIR = Path(CACHE_TTS_DIR)
CACHE_TTS_DIR.mkdir(parents=True, exist_ok=True)

ELEVENLABS_MODEL_ID = getattr(_s, "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
ELEVENLABS_VOICE_MAP: Dict[str, str] = getattr(_s, "ELEVENLABS_VOICE_MAP", {}) or {}
ELEVENLABS_VOICE_ID  = getattr(_s, "ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")  # fallback

TTS_PROVIDER_MAP: Dict[str, str] = getattr(_s, "TTS_PROVIDER_MAP", {}) or {}

# Piper config
PIPER_BIN    = getattr(_s, "PIPER_BIN", "piper")  # or absolute path / piper.exe on Windows
PIPER_MODEL  = getattr(_s, "PIPER_MODEL", "voices/lb_LU-marylux-medium.onnx")
PIPER_CONFIG = getattr(_s, "PIPER_CONFIG", "voices/lb_LU-marylux-medium.onnx.json")
PIPER_MODEL_MAP: Dict[str, str] = getattr(_s, "PIPER_MODEL_MAP", {}) or {"lb": PIPER_MODEL}
PIPER_LENGTH = float(getattr(_s, "PIPER_LENGTH", 1.0))
PIPER_NOISE  = float(getattr(_s, "PIPER_NOISE", 0.5))
PIPER_NOISE_W= float(getattr(_s, "PIPER_NOISE_W", 0.5))

_ELEVEN_WARNED_ONCE = False

# -----------------------------
# Normalization helpers
# -----------------------------
def _normalize(seg: AudioSegment) -> AudioSegment:
    if seg.frame_rate != SAMPLE_RATE:
        seg = seg.set_frame_rate(SAMPLE_RATE)
    if seg.channels != CHANNELS:
        seg = seg.set_channels(CHANNELS)
    if seg.sample_width != SAMPLE_WIDTH:
        seg = seg.set_sample_width(SAMPLE_WIDTH)
    return seg

# -----------------------------
# Keys / cache helpers
# -----------------------------
def _get_eleven_api_key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not key:
        key = (getattr(_s, "ELEVENLABS_API_KEY", "") or "").strip()
    if not key:
        for d in (Path.cwd(), Path(__file__).resolve().parent):
            f = d / "elevenlabs.key"
            if f.exists():
                try:
                    t = f.read_text(encoding="utf-8").strip()
                    if t:
                        return t
                except Exception:
                    pass
    return key

def _cache_key(provider: str, lang: str, text: str, extra: str = "") -> str:
    h = hashlib.sha256()
    h.update(provider.encode("utf-8")); h.update(b"|")
    h.update(lang.encode("utf-8"));     h.update(b"|")
    h.update(text.encode("utf-8"))
    if extra:
        h.update(b"|"); h.update(extra.encode("utf-8"))
    return h.hexdigest()

def _cache_path(key: str, ext: str = ".mp3") -> Path:
    return CACHE_TTS_DIR / f"{key}{ext}"

def _load_audio_from_file(p: Path) -> Optional[AudioSegment]:
    try:
        return _normalize(AudioSegment.from_file(p))
    except Exception:
        return None

def _save_bytes(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)

# -----------------------------
# gTTS
# -----------------------------
_GTTs_LANG_MAP = {
    "en":"en","fr":"fr","de":"de","es":"es","it":"it","pt":"pt","hi":"hi","zh-cn":"zh-CN","ru":"ru",
    # gTTS ندارد: lb → en
    "lb":"en"
}
def _tts_gtts(text: str, lang_code: str) -> AudioSegment:
    if gTTS is None or AudioSegment is None:
        return _normalize(AudioSegment.silent(duration=800))
    g_code = _GTTs_LANG_MAP.get(str(lang_code).lower(), "en")
    key = _cache_key("gtts", g_code, text)
    cache_mp3 = _cache_path(key, ".mp3")
    if cache_mp3.exists():
        seg = _load_audio_from_file(cache_mp3)
        if seg is not None:
            return seg
    try:
        tts = gTTS(text=text, lang=g_code)
        buf = io.BytesIO(); tts.write_to_fp(buf)
        data = buf.getvalue()
        _save_bytes(cache_mp3, data)
        seg = AudioSegment.from_file(io.BytesIO(data), format="mp3")
        print(f"[TTS] gTTS ok lang={g_code} len={len(seg)}ms")
        return _normalize(seg)
    except Exception as e:
        print(f"[ERROR] gTTS failed ({g_code}): {e}")
        return _normalize(AudioSegment.silent(duration=800))

# -----------------------------
# ElevenLabs
# -----------------------------
def _pick_voice_for_lang(lang_code: str) -> Optional[str]:
    if not ELEVENLABS_VOICE_MAP:
        return ELEVENLABS_VOICE_ID
    lang = str(lang_code).lower()
    if lang in ELEVENLABS_VOICE_MAP:
        return ELEVENLABS_VOICE_MAP[lang]
    base = lang.split("-")[0]
    for k,v in ELEVENLABS_VOICE_MAP.items():
        if k.split("-")[0] == base:
            return v
    return ELEVENLABS_VOICE_ID

def _tts_elevenlabs(text: str, lang_code: str, api_key: str) -> AudioSegment:
    if AudioSegment is None or requests is None:
        return _normalize(AudioSegment.silent(duration=800))
    voice_id = _pick_voice_for_lang(lang_code)
    model_id = ELEVENLABS_MODEL_ID or "eleven_multilingual_v2"
    extra = f"voice={voice_id}|model={model_id}"
    key = _cache_key("elevenlabs", str(lang_code).lower(), text, extra=extra)
    cache_mp3 = _cache_path(key, ".mp3")
    if cache_mp3.exists():
        seg = _load_audio_from_file(cache_mp3)
        if seg is not None:
            return seg
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "accept": "audio/mpeg", "content-type": "application/json"}
    payload = {"text": text, "model_id": model_id, "voice_settings": {"stability": 0.45, "similarity_boost": 0.7}}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=45)
        if r.status_code >= 400:
            print(f"[ERROR] ElevenLabs HTTP {r.status_code}: {r.text[:180]}")
            return _tts_gtts(text, lang_code)
        mp3_bytes = r.content
        _save_bytes(cache_mp3, mp3_bytes)
        seg = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
        print(f"[TTS] ElevenLabs ok lang={lang_code} model={model_id} voice={voice_id} len={len(seg)}ms")
        return _normalize(seg)
    except Exception as e:
        print(f"[ERROR] ElevenLabs failed: {e} → fallback gTTS")
        return _tts_gtts(text, lang_code)

# -----------------------------
# Piper (local)
# -----------------------------
def _resolve_piper_bin() -> Optional[str]:
    # absolute
    if PIPER_BIN and Path(PIPER_BIN).exists():
        return str(PIPER_BIN)
    # PATH
    for cand in [PIPER_BIN, "piper", "piper.exe"]:
        if not cand:
            continue
        found = shutil.which(cand)
        if found:
            return found
    return None

def _resolve_piper_model_for_lang(lang_code: str) -> (str, str):
    lang = str(lang_code or "en").lower()
    model = PIPER_MODEL_MAP.get(lang) or PIPER_MODEL or ""
    conf = ""
    if model:
        mp = Path(model)
        if not mp.suffix.lower().endswith(".onnx"):
            if mp.is_dir():
                ons = list(mp.glob("*.onnx"))
                if ons:
                    mp = ons[0]
        model = str(mp)
        cand = PIPER_CONFIG or (str(mp) + ".json")
        if Path(cand).exists():
            conf = cand
    else:
        conf = PIPER_CONFIG or ""
    return model, conf

def _tts_piper(text: str, lang_code: str) -> AudioSegment:
    if AudioSegment is None:
        return _normalize(AudioSegment.silent(duration=800))
    bin_path = _resolve_piper_bin()
    if not bin_path:
        print("[ERROR] Piper binary not found in PATH; set settings.PIPER_BIN to full path.")
        return _normalize(AudioSegment.silent(duration=800))
    model_path, config_path = _resolve_piper_model_for_lang(lang_code)
    if not model_path or not Path(model_path).exists():
        print(f"[ERROR] Piper model not found for '{lang_code}'.")
        return _normalize(AudioSegment.silent(duration=800))
    extra = f"model={model_path}|len={PIPER_LENGTH}|nz={PIPER_NOISE}|nw={PIPER_NOISE_W}"
    key = _cache_key("piper", str(lang_code).lower(), text, extra=extra)
    cache_wav = _cache_path(key, ".wav")
    if cache_wav.exists():
        seg = _load_audio_from_file(cache_wav)
        if seg is not None:
            return seg
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        in_txt = td / "in.txt"; out_wav = td / "out.wav"
        in_txt.write_text(text, encoding="utf-8")
        cmd = [str(bin_path), "--model", str(model_path), "--output_file", str(out_wav), "--input_file", str(in_txt)]
        if config_path and Path(config_path).exists():
            cmd += ["--config", str(config_path)]
        if PIPER_LENGTH: cmd += ["--length_scale", str(float(PIPER_LENGTH))]
        if PIPER_NOISE is not None: cmd += ["--noise_scale", str(float(PIPER_NOISE))]
        if PIPER_NOISE_W is not None: cmd += ["--noise-w-scale", str(float(PIPER_NOISE_W))]
        try:
            print(f"[TTS] Piper run: {' '.join(cmd[:6])} ...")
            r = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if out_wav.exists():
                _save_bytes(cache_wav, out_wav.read_bytes())
                seg = AudioSegment.from_file(cache_wav)
                print(f"[TTS] Piper ok lang={lang_code} len={len(seg)}ms")
                return _normalize(seg)
            else:
                print("[ERROR] Piper finished but no output wav produced.")
        except Exception as e:
            err = ""
            try:
                err = (r.stderr or b"").decode("utf-8", "ignore")[:220]
            except Exception:
                pass
            print(f"[ERROR] Piper failed: {e} | {err}")
    return _normalize(AudioSegment.silent(duration=800))

# -----------------------------
# Provider resolver
# -----------------------------
def _resolve_provider_for_lang(lang_code: str, provider_hint: str = "gtts") -> str:
    try:
        lang = (lang_code or "").lower().strip()
    except Exception:
        lang = "en"
    prov_map = TTS_PROVIDER_MAP or {}
    chosen = prov_map.get(lang) or prov_map.get("default") or prov_map.get("_default")
    if not chosen:
        chosen = provider_hint or getattr(_s, "TTS_PROVIDER", "gtts")
    return str(chosen).lower().strip()

# -----------------------------
# Public: unified TTS wrapper
# -----------------------------
def safe_tts_to_segment(text: str, lang_code: str, provider: str = "gtts") -> AudioSegment:
    if AudioSegment is None:
        raise RuntimeError("pydub not available; install requirements.")
    chosen = _resolve_provider_for_lang(lang_code, provider_hint=provider)
    print(f"[TTS] selected provider={chosen} lang={lang_code}")
    if chosen == "piper":
        return _tts_piper(text, lang_code)
    if chosen == "elevenlabs":
        api_key = _get_eleven_api_key()
        if not api_key:
            global _ELEVEN_WARNED_ONCE
            if not _ELEVEN_WARNED_ONCE:
                print("[ERROR] ELEVENLABS_API_KEY missing (fallback to gTTS).")
                _ELEVEN_WARNED_ONCE = True
            return _tts_gtts(text, lang_code)
        return _tts_elevenlabs(text, lang_code, api_key=api_key)
    return _tts_gtts(text, lang_code)

# -----------------------------
# Background music
# -----------------------------
def load_bg_music(path: str, total_ms: int, gain_db: float = -18.0) -> Optional[AudioSegment]:
    if AudioSegment is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        bg = AudioSegment.from_file(p)
        if gain_db:
            bg = bg + float(gain_db)
        bg = _normalize(bg)
        if len(bg) <= 0:
            return None
        reps = max(1, int(total_ms // len(bg)) + 1)
        out = AudioSegment.silent(duration=0)
        for _ in range(reps):
            out += bg
        if len(out) > total_ms:
            out = out[:total_ms]
        return out
    except Exception:
        return None

# -----------------------------
# Builder: cues -> final audio
# -----------------------------
def build_audio_snapped_to_cues(cues: List[Dict[str, Any]], pause_rep_ms: int = PAUSE_REP) -> AudioSegment:
    if AudioSegment is None:
        raise RuntimeError("pydub not available; install requirements.")
    out = AudioSegment.silent(duration=0)
    total = len(cues)
    for idx, cue in enumerate(cues, start=1):
        start_ms = int(cue.get("start", 0))
        end_ms   = int(cue.get("end", start_ms))
        target_ms = max(0, end_ms - start_ms)
        text   = str(cue.get("text", "") or "")
        lang   = str(cue.get("lang", "en") or "en")
        repeat = max(1, int(cue.get("repeat", 1)))
        provider = getattr(_s, "TTS_PROVIDER", "gtts")
        seg_one = safe_tts_to_segment(text, lang, provider=provider)
        gap = _normalize(AudioSegment.silent(duration=max(0, int(pause_rep_ms))))
        built = AudioSegment.silent(duration=0)
        for r in range(repeat):
            if r > 0: built += gap
            built += seg_one
        built_len = len(built)
        if target_ms > 0:
            if built_len < target_ms:
                built += AudioSegment.silent(duration=target_ms - built_len)
            elif built_len > target_ms:
                built = built[:target_ms]
        built_len = len(built)
        print(f"[AUDIO] cue {idx}/{total} [{start_ms}→{end_ms}] target={target_ms}ms built={built_len}ms")
        pad_needed = max(0, start_ms - len(out))
        if pad_needed:
            out += AudioSegment.silent(duration=pad_needed)
        out += built
    return _normalize(out)
