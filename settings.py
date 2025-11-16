# settings.py
# -------------------------------------------------------------
# Global defaults for TTS + Subtitles + Video generator
# - Used when settings_temp.py is absent
# - Keep ALL keys that main.py fetches via getattr(...)
# -------------------------------------------------------------

from __future__ import annotations
from pathlib import Path
import os

# ------------------------------- #
#             I/O                 #
# ------------------------------- #
INPUT_DIR = Path("Text") / "Vocab" / "A1"
INPUT_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_DIR = Path("Output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Default input file (GUI usually overwrites this in settings_temp.py)
INPUT_FILENAME = "sample.txt"

# ------------------------------- #
#        Edition / Provider       #
# ------------------------------- #
# "free" -> gTTS only; "premium" -> ElevenLabs available
EDITION = "free"
TTS_PROVIDER = "gtts"   # "gtts" or "elevenlabs"

# ------------------------------- #
#            Languages            #
# ------------------------------- #
# main.py expects LANG_MAP + *_IDX + ENABLE_BILINGUAL
LANG_MAP = {
    0: "en",      # English
    1: "fr",      # French
    2: "de",      # German
    3: "es",      # Spanish
    4: "it",      # Italian
    5: "pt",      # Portuguese
    6: "hi",      # Hindi
    7: "zh-cn",   # Chinese (Simplified)
    8: "ru",      # Russian
    9: "lb",      # Luxembourgish
}

ENABLE_BILINGUAL   = True
PRIMARY_LANG_IDX   = 0     # column index in parsed lines
SECONDARY_LANG_IDX = 1     # index for translation column

# ------------------------------- #
#        Timing / Repeats         #
# ------------------------------- #
VOCAB_REPEAT = {
    "primary": 1,
    "secondary": 2,
    "pause_rep": 2500,
    "pause_sent": 2500
}
SCENARIO_REPEAT = {
    "primary": 1,
    "secondary": 2,
    "pause_rep": 2500,
    "pause_sent": 3500
}

# ------------------------------- #
#              LLM               #
# ------------------------------- #
# General provider + models (used only if GUI enables LLM mode)
LLM_PROVIDER   = "openai"      # "ollama" or "openai"
LLM_TOPIC      = ""            # empty -> infer from input filename
LLM_ITEMS      = 20            # number of sentences/items to generate

# Ollama (local)
OLLAMA_HOST     = "http://localhost:11434"
OLLAMA_BASE_URL = OLLAMA_HOST
OLLAMA_MODEL    = "llama3.1:8b"

# OpenAI (cloud) - only used if LLM_PROVIDER == "openai"
OPENAI_MODEL    = "gpt-5-mini"

# Backward-compat names (some older builds used these)
GENERATE_WITH_LLM = False
LLM_MODEL         = OLLAMA_MODEL

# ------------------------------- #
#         ElevenLabs (TTS)        #
# ------------------------------- #
# API key: read from ENV if present, otherwise use the static key below.
ELEVENLABS_API_KEY = os.getenv(
    "ELEVENLABS_API_KEY",
    "9461549b62542328f083ba842f18977902dbfc0e936bc3ef34a7791ad3ad8b3c"  # provided by user
)

# Default model
ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"

# ------------------------------- #
#   Language -> TTS provider map  #
# ------------------------------- #
# Route languages to specific providers. You can override in settings_temp.py.
# Valid providers: "piper", "gtts", "elevenlabs"
TTS_PROVIDER_MAP = {
    "lb": "piper",        # Luxembourgish -> Piper (local)
    "en": "gtts",         # English -> gTTS (free)
    "default": "elevenlabs"  # all others -> ElevenLabs (paid)
}


# Fallback voice if language not in map
ELEVENLABS_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # Sarah

# Per-language default voices (can be overridden by settings_temp.py)
ELEVENLABS_VOICE_MAP = {
    # English
    "en": "CwhRBWXzGAHq8TQ4Fs17",  # Roger
    # French
    "fr": "CwhRBWXzGAHq8TQ4Fs17",  # Roger
    # German
    "de": "CwhRBWXzGAHq8TQ4Fs17",  # Roger (also: Will=bIHbv24MWmeRgasZH58o)
    # Spanish
    "es": "CwhRBWXzGAHq8TQ4Fs17",  # Roger
    # Italian
    "it": "pFZP5JQG7iQjIQuC4Bku",  # Lily (Matilda=XrExE9yKIg1WjnnlVkGX)
    # Portuguese
    "pt": "IKne3meq5aSn9XLyUdCD",  # Charlie (River=SAz9YHcvj6GT2YYXdXww)
    # Hindi
    "hi": "JBFqnCBsd6RMkjVDRZzb",  # George (Sarah=EXAVITQu4vr4xnSDxMaL)
    # Chinese (Simplified)
    "zh-cn": "EXAVITQu4vr4xnSDxMaL",  # Sarah (Charlie=IKne3meq5aSn9XLyUdCD)
    # Russian
    "ru": "bIHbv24MWmeRgasZH58o",  # Will
    # Luxembourgish
    "lb": "bIHbv24MWmeRgasZH58o",  # Will
}

# ------------------------------- #
#            Video/Fonts          #
# ------------------------------- #
VIDEO_SIZE = "1920x1080"
VIDEO_FPS  = 30

FONT_NAME = "Segoe UI Semibold"
FONT_SIZE = 80

# ------------------------------- #
#       Background Music          #
# ------------------------------- #
# main.py reads BG_ENABLED + BG_MUSIC + BG_GAIN_DB
BG_ENABLED = True                   # enable/disable background music
BG_MUSIC   = "bg_music.mp3"         # path to bg music file (optional)
BG_GAIN_DB = -18                    # reduce bg volume (dB)

# Legacy compatibility keys (older GUI variants)
BG_MODE  = "loop"                   # "loop" or "none"



# ------------------------------- #
#           Piper (Local)         #
# ------------------------------- #
# Path to 'piper' binary (ensure it's installed and in PATH or set absolute path)
PIPER_BIN    = "piper"  # on Windows, you may need "piper.exe"

# Default Piper model/config (override in settings_temp.py via GUI)
# Place your models under a local "voices/" folder or provide absolute paths.
PIPER_MODEL  = "voices/lb_LU-marylux-medium.onnx"
PIPER_CONFIG = "voices/lb_LU-marylux-medium.onnx.json"

# Per-language model mapping. You can add more languages later.
PIPER_MODEL_MAP = {
    "lb": PIPER_MODEL
}

# Synthesis parameters (tune to taste)
PIPER_LENGTH  = 1.0   # length_scale (slower speech → >1.0)
PIPER_NOISE   = 0.5   # noise_scale
PIPER_NOISE_W = 0.5   # noise_w

# Image search defaults
PIXABAY_SAFESEARCH  = "true"   # "true" or "false"
AUTO_IMAGE_LANG     = "auto"   # auto-detect language from text
IMAGES_PER_SENTENCE = 1        # how many images we try per primary cue
IMAGE_TIMEOUT       = 12       # seconds per HTTP request
IMAGE_RETRIES       = 3        # HTTP retry count


# ------------------------------- #
#         Subtitles / Timing      #
# ------------------------------- #
# Read timing from external SRT instead of auto
READ_TIMING_FROM_EXTERNAL_SRT = False
EXTERNAL_SRT_PATH             = "timing_source.srt"

# Align to ASS 1/100 s grid when building audio
ALIGN_TO_ASS_CENTISECOND_GRID = True

# Time-stretch tolerance and max ratio for ffmpeg atempo
STRETCH_TOLERANCE_MS = 40
MAX_STRETCH_RATIO    = 0.25

# ------------------------------- #
#          Audio core I/O         #
# ------------------------------- #
SAMPLE_RATE  = 48000
CHANNELS     = 2
SAMPLE_WIDTH = 2  # bytes (16-bit)

# ------------------------------- #
#      FFmpeg fallback path       #
# ------------------------------- #
# If ffmpeg is not in PATH or bundled, audio_utils will use this
FFMPEG_FALLBACK = r".\ffmpeg\bin\ffmpeg.exe"

# ------------------------------- #
#            Cache Dirs           #
# ------------------------------- #
CACHE_TTS_DIR   = Path(".cache_tts")
CACHE_IMG_DIR   = Path(".cache_images")
CACHE_VIDEO_DIR = Path(".cache_video")

for d in (CACHE_TTS_DIR, CACHE_IMG_DIR, CACHE_VIDEO_DIR):
    d.mkdir(exist_ok=True)

# ------------------------------- #
#            App Info             #
# ------------------------------- #
APP_ID = "TTS-Video CFR multilingual"