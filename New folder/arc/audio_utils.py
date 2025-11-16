# audio_utils.py
# -------------------------------------------------------------
# Unified audio utilities for gTTS and ElevenLabs
# - Reads ElevenLabs API key from ENV -> settings.py -> elevenlabs.key
# - Caches TTS results in CACHE_TTS_DIR
# - Normalizes all audio to target SAMPLE_RATE/CHANNELS/SAMPLE_WIDTH
# - Provides load_bg_music and build_audio_snapped_to_cues used by main.py
# -------------------------------------------------------------

from __future__ import annotations

import io
import os
import json
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any, List
import subprocess, tempfile

# Prefer GUI settings_temp; fallback to settings.py
try:
    import settings_temp as _s
except Exception:
    import settings as _s

# Optional deps (safe imports)
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
    AudioSegment = None  # main.py will check and exit if missing


# -----------------------------
# Constants / defaults
# -----------------------------
# These are only fallbacks. main.py passes exact timings from settings.
PAUSE_REP = 800    # ms between repeats (fallback default)
PAUSE_SENT = 400   # ms between sentences (fallback default)

# Required targets (taken from settings with safe defaults)
SAMPLE_RATE  = int(getattr(_s, "SAMPLE_RATE", 48000))
CHANNELS     = int(getattr(_s, "CHANNELS", 2))
SAMPLE_WIDTH = int(getattr(_s, "SAMPLE_WIDTH", 2))  # bytes per sample (2=16-bit)

CACHE_TTS_DIR = getattr(_s, "CACHE_TTS_DIR", Path(".cache_tts"))
if isinstance(CACHE_TTS_DIR, str):
    CACHE_TTS_DIR = Path(CACHE_TTS_DIR)
CACHE_TTS_DIR.mkdir(parents=True, exist_ok=True)

# ElevenLabs model + voice map (provided by GUI settings)
ELEVENLABS_MODEL_ID = getattr(_s, "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
ELEVENLABS_VOICE_MAP: Dict[str, str] = getattr(_s, "ELEVENLABS_VOICE_MAP", {}) or {}
# Language -> provider routing map (overrides provider hint)
TTS_PROVIDER_MAP: Dict[str, str] = getattr(_s, "TTS_PROVIDER_MAP", {}) or {}
# Piper (local) configuration (read from settings or defaults)
PIPER_BIN          = getattr(_s, "PIPER_BIN", "piper")  # or "piper.exe" on Windows
PIPER_MODEL        = getattr(_s, "PIPER_MODEL", "voices/lb_LU-marylux-medium.onnx")     # default model .onnx path
PIPER_CONFIG       = getattr(_s, "PIPER_CONFIG", "voices/lb_LU-marylux-medium.onnx.json")    # default model .json path
PIPER_MODEL_MAP: Dict[str, str] = getattr(_s, "PIPER_MODEL_MAP", {}) or {"lb": PIPER_MODEL}
PIPER_LENGTH       = float(getattr(_s, "PIPER_LENGTH", 1.0))     # length_scale
PIPER_NOISE        = float(getattr(_s, "PIPER_NOISE", 0.5))      # noise_scale
PIPER_NOISE_W      = float(getattr(_s, "PIPER_NOISE_W", 0.5))    # noise_w


# Non-spam warning flag
_ELEVEN_WARNED_ONCE = False


# -----------------------------
# Normalization helpers
# -----------------------------
def _normalize(seg: AudioSegment) -> AudioSegment:
    """Force segment to SAMPLE_RATE/CHANNELS/SAMPLE_WIDTH."""
    if seg.frame_rate != SAMPLE_RATE:
        seg = seg.set_frame_rate(SAMPLE_RATE)
    if seg.channels != CHANNELS:
        seg = seg.set_channels(CHANNELS)
    if seg.sample_width != SAMPLE_WIDTH:
        seg = seg.set_sample_width(SAMPLE_WIDTH)
    return seg


# -----------------------------
# ElevenLabs key resolver
# -----------------------------
def _get_eleven_api_key() -> str:
    """Resolve ElevenLabs API key: ENV -> settings -> elevenlabs.key file."""
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


# -----------------------------
# Caching
# -----------------------------
def _cache_key(provider: str, lang: str, text: str, extra: str = "") -> str:
    h = hashlib.sha256()
    h.update(provider.encode("utf-8"))
    h.update(b"|")
    h.update(lang.encode("utf-8"))
    h.update(b"|")
    h.update(text.encode("utf-8"))
    if extra:
        h.update(b"|")
        h.update(extra.encode("utf-8"))
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
# gTTS implementation
# -----------------------------
# Map project lang codes to gTTS codes (best effort)
_GTTs_LANG_MAP = {
    # Identity mappings (gTTS language codes)
    "af":"af","ar":"ar","bn":"bn","ca":"ca","cs":"cs","da":"da","de":"de","el":"el","en":"en","es":"es",
    "fi":"fi","fr":"fr","hi":"hi","hu":"hu","id":"id","it":"it","ja":"ja","ko":"ko","ms":"ms","nl":"nl",
    "no":"no","pl":"pl","pt":"pt","ro":"ro","ru":"ru","sk":"sk","sv":"sv","ta":"ta","th":"th","tr":"tr",
    "uk":"uk","vi":"vi",
    # Chinese
    "zh-cn":"zh-CN","zh-tw":"zh-TW",
    # Project-specific codes fallback
    "lb":"en"
}}

def _tts_gtts(text: str, lang_code: str) -> AudioSegment:
    """Generate TTS via gTTS (always returns AudioSegment)."""
    if gTTS is None or AudioSegment is None:
        # Fallback: 800ms silence
        return _normalize(AudioSegment.silent(duration=800))

    g_code = _GTTs_LANG_MAP.get(lang_code.lower(), "en")
    key = _cache_key("gtts", g_code, text)
    cache_mp3 = _cache_path(key, ".mp3")

    # Cache hit
    if cache_mp3.exists():
        seg = _load_audio_from_file(cache_mp3)
        if seg is not None:
            return seg

    # Generate fresh
    try:
        tts = gTTS(text=text, lang=g_code)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        data = buf.getvalue()
        _save_bytes(cache_mp3, data)
        seg = AudioSegment.from_file(io.BytesIO(data), format="mp3")
        return _normalize(seg)
    except Exception:
        # Last resort: silence
        return _normalize(AudioSegment.silent(duration=800))


# -----------------------------
# ElevenLabs implementation
# -----------------------------
def _pick_voice_for_lang(lang_code: str) -> Optional[str]:
    """Pick a voice_id for a given lang code from ELEVENLABS_VOICE_MAP."""
    if not ELEVENLABS_VOICE_MAP:
        return None
    # exact lang match
    vid = ELEVENLABS_VOICE_MAP.get(lang_code.lower())
    if vid:
        return vid
    # coarse: primary part before dash
    base = lang_code.split("-")[0]
    for k, v in ELEVENLABS_VOICE_MAP.items():
        if k.split("-")[0] == base:
            return v
    # fallback: first entry
    try:
        return next(iter(ELEVENLABS_VOICE_MAP.values()))
    except Exception:
        return None

def _tts_elevenlabs(text: str, lang_code: str, api_key: str) -> AudioSegment:
    """Generate TTS via ElevenLabs (returns AudioSegment)."""
    if AudioSegment is None or requests is None:
        return _normalize(AudioSegment.silent(duration=800))

    voice_id = _pick_voice_for_lang(lang_code) or _pick_voice_for_lang("en")
    if not voice_id:
        return _normalize(AudioSegment.silent(duration=800))

    model_id = ELEVENLABS_MODEL_ID or "eleven_multilingual_v2"

    # Cache
    extra = f"voice={voice_id}|model={model_id}"
    key = _cache_key("elevenlabs", lang_code, text, extra=extra)
    cache_mp3 = _cache_path(key, ".mp3")
    if cache_mp3.exists():
        seg = _load_audio_from_file(cache_mp3)
        if seg is not None:
            return seg

    # HTTP call
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        # minimal voice settings; keep default if you prefer
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.7}
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=45)
        r.raise_for_status()
        mp3_bytes = r.content
        _save_bytes(cache_mp3, mp3_bytes)
        seg = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
        return _normalize(seg)
    except Exception:
        # On any failure, fallback to gTTS
        return _tts_gtts(text, lang_code)



# -----------------------------
# Piper (local CLI) implementation
# -----------------------------
def _resolve_piper_model_for_lang(lang_code: str) -> (str, str):
    """
    Return (model_path, config_path) for given lang_code.
    Resolution order:
      - PIPER_MODEL_MAP[lang_code]
      - PIPER_MODEL (and PIPER_CONFIG)
    """
    lang = (lang_code or "en").lower()
    model = PIPER_MODEL_MAP.get(lang) or PIPER_MODEL or ""
    conf  = ""
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
    model_path, config_path = _resolve_piper_model_for_lang(lang_code)
    if not model_path or not Path(model_path).exists():
        # Model not found; return short silence (prevents crash)
        return _normalize(AudioSegment.silent(duration=800))

    extra = f"model={model_path}|len={PIPER_LENGTH}|nz={PIPER_NOISE}|nw={PIPER_NOISE_W}"
    key = _cache_key("piper", lang_code, text, extra=extra)
    cache_wav = _cache_path(key, ".wav")
    seg = None
    if cache_wav.exists():
        seg = _load_audio_from_file(cache_wav)
        if seg is not None:
            return seg

    import tempfile, subprocess
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        in_txt = td / "in.txt"
        out_wav = td / "out.wav"
        in_txt.write_text(text, encoding="utf-8")
        cmd = [str(PIPER_BIN), "--model", str(model_path), "--output_file", str(out_wav), "--input_file", str(in_txt)]
        if config_path and Path(config_path).exists():
            cmd.extend(["--config", str(config_path)])
        if PIPER_LENGTH:
            cmd.extend(["--length_scale", str(float(PIPER_LENGTH))])
        if PIPER_NOISE is not None:
            cmd.extend(["--noise_scale", str(float(PIPER_NOISE))])
        if PIPER_NOISE_W is not None:
            cmd.extend(["--noise-w-scale", str(float(PIPER_NOISE_W))])
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if out_wav.exists():
                _save_bytes(cache_wav, out_wav.read_bytes())
                seg = AudioSegment.from_file(cache_wav)
                return _normalize(seg)
        except Exception:
            pass
    return _normalize(AudioSegment.silent(duration=800))


# -----------------------------
# Provider resolver (lang-aware)
# -----------------------------
def _resolve_provider_for_lang(lang_code: str, provider_hint: str = "gtts") -> str:
    """
    Decide provider using settings.TTS_PROVIDER_MAP with fallback to `provider_hint` or settings.TTS_PROVIDER.
    Map example:
        {"lb":"piper", "en":"gtts", "default":"elevenlabs"}
    """
    try:
        lang = (lang_code or "").lower().strip()
    except Exception:
        lang = "en"
    prov_map = TTS_PROVIDER_MAP or {}
    chosen = prov_map.get(lang)
    if not chosen:
        chosen = prov_map.get("default") or prov_map.get("_default")
    if not chosen:
        chosen = provider_hint or getattr(_s, "TTS_PROVIDER", "gtts")
    return str(chosen).lower().strip()

# -----------------------------
# Public: unified TTS wrapper
# -----------------------------

def safe_tts_to_segment(text: str, lang_code: str, provider: str = "gtts") -> AudioSegment:
    """
    Generate speech for `text` in `lang_code` using provider routing.
    - provider: optional hint ("gtts" | "elevenlabs" | "piper"); overridden by TTS_PROVIDER_MAP if present.
    - Never raises; always returns a normalized AudioSegment.
    - If ElevenLabs key missing, prints error ONCE and falls back to gTTS.
    """
    if AudioSegment is None:
        raise RuntimeError("pydub not available; install requirements.")

    # Decide provider (map overrides hint)
    chosen = _resolve_provider_for_lang(lang_code, provider_hint=provider)

    if chosen == "piper":
        return _tts_piper(text, lang_code)

    if chosen == "elevenlabs":
        api_key = _get_eleven_api_key()
        if not api_key:
            global _ELEVEN_WARNED_ONCE
            if not _ELEVEN_WARNED_ONCE:
                print("[ERROR] ELEVENLABS_API_KEY missing.")
                _ELEVEN_WARNED_ONCE = True
            return _tts_gtts(text, lang_code)
        return _tts_elevenlabs(text, lang_code, api_key=api_key)

    # Default to gTTS
    return _tts_gtts(text, lang_code)

# -----------------------------
# Background music loader
# -----------------------------
def load_bg_music(path: str, total_ms: int, gain_db: float = -18.0) -> Optional[AudioSegment]:
    """
    Load background track, loop to cover total length, apply gain, trim to total_ms.
    Returns normalized segment or None if not available.
    """
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

        # Loop to cover total length
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
    """
    Build final audio by repeating each cue's TTS with a short gap between repeats,
    then pad/trim to exactly match target (end - start).
    Cue format (as used in main.py):
      - "start": ms
      - "end": ms
      - "text": str
      - "lang": str
      - "repeat": int
    """
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

        # Generate one utterance
        # NOTE: provider is decided by settings in main.py and passed to safe_tts_to_segment there,
        # but we rebuild here using selected provider again to keep consistency.
        provider = getattr(_s, "TTS_PROVIDER", "gtts")
        seg_one = safe_tts_to_segment(text, lang, provider=provider)
        gap = _normalize(AudioSegment.silent(duration=max(0, int(pause_rep_ms))))

        # Repeat with gaps
        built = AudioSegment.silent(duration=0)
        for r in range(repeat):
            if r > 0:
                built += gap
            built += seg_one

        # Fit to target: pad or trim
        built_len = len(built)
        if target_ms > 0:
            if built_len < target_ms:
                built += AudioSegment.silent(duration=target_ms - built_len)
            elif built_len > target_ms:
                built = built[:target_ms]
        built_len = len(built)

        # Logging (similar to your sample)
        print(f"[AUDIO] cue {idx}/{total} [{start_ms}→{end_ms}] target={target_ms}ms built={built_len}ms")

        # Append to final output; ensure timeline continuity by padding if needed
        pad_needed = max(0, start_ms - len(out))
        if pad_needed:
            out += AudioSegment.silent(duration=pad_needed)
        out += built

    return _normalize(out)
