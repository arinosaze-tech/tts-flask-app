#!/usr/bin/env python3
# -------------------------------------------------------------
# TTS + Subtitles + Video Generator (LLM-aware)
# - Prefers settings_temp.py (GUI), fallback to settings.py
# - LLM: OpenAI (Responses + Chat fallback for non-gpt-5) / Ollama
# - Input: LLM generated or Text file
# - Hashtag extraction for image search (PRIMARY sentence tail)
# - Builds SRT/ASS + audio + (optional) video background
# -------------------------------------------------------------

from __future__ import annotations

import os
import sys
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

# --- safer console UTF-8
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# -------------------------------------------------------------
# Load settings (GUI temp preferred)
# -------------------------------------------------------------
try:
    import settings_temp as settings
    print("[INFO] Using settings_temp.py (from GUI)")
except Exception:
    import settings
    print("[INFO] Using settings.py (fallback)")

# -------------------------------------------------------------
# Optional deps
# -------------------------------------------------------------
try:
    import requests
except Exception:
    requests = None

try:
    from pydub import AudioSegment
except Exception:
    AudioSegment = None

# -------------------------------------------------------------
# Local modules (tolerant import)
# -------------------------------------------------------------
try:
    from audio_utils import (
        safe_tts_to_segment,
        _normalize,
        load_bg_music,
        build_audio_snapped_to_cues,  # new name in your utils
        PAUSE_REP as _AU_PAUSE_REP,
        PAUSE_SENT as _AU_PAUSE_SENT,
    )
except Exception:
    safe_tts_to_segment = None
    _normalize = None
    load_bg_music = None
    build_audio_snapped_to_cues = None
    _AU_PAUSE_REP = 800
    _AU_PAUSE_SENT = 400

# keep backward alias if old code calls it
if 'build_audio_from_cues_repeat_all' not in globals() and build_audio_snapped_to_cues is not None:
    build_audio_from_cues_repeat_all = build_audio_snapped_to_cues

try:
    from subtitles import (
        parse_srt,
        round_to_ass_grid,
        write_srt_from_cues,
        write_ass_from_cues,
    )
except Exception:
    parse_srt = None
    def round_to_ass_grid(x: int) -> int: return x
    write_srt_from_cues = None
    write_ass_from_cues = None

try:
    from video_utils import (
        render_video_single_or_none,
        sentence_to_query_extras,   # may be None in some builds
        get_images_for_cues,        # per-sentence images
        build_slideshow_video_cfr,  # slideshow builder
        mux_subs_and_audio_on_video # final mux
    )
except Exception:
    render_video_single_or_none = None
    sentence_to_query_extras = None
    get_images_for_cues = None
    build_slideshow_video_cfr = None
    mux_subs_and_audio_on_video = None

# -------------------------------------------------------------
# FFmpeg detection
# -------------------------------------------------------------
def detect_ffmpeg_path(fallback_path: str) -> str:
    import shutil as _sh
    p = _sh.which("ffmpeg") or _sh.which("ffmpeg.exe")
    if p:
        return p
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        cand = os.path.join(sys._MEIPASS, "ffmpeg", "bin", "ffmpeg.exe")
        if os.path.exists(cand):
            return cand
    return fallback_path

# -------------------------------------------------------------
# Small utils
# -------------------------------------------------------------
def _slug(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r'\s+', '_', s)
    s = re.sub(r'[^a-z0-9_\-]+', '', s)
    return s or "output"

# remove bullets/numbers like "- ", "1) ", "a. "
_BULLET_PREFIX = re.compile(r'^\s*([\-–—•●·*]|(\d+|[a-zA-Z])[\.\)\]:])\s+')
def strip_bullet_prefix(s: str) -> str:
    return _BULLET_PREFIX.sub('', s, count=1)

def _extract_hashtags_and_clean(s: str) -> Tuple[str, List[str]]:
    """
    Extract hashtags (e.g., #kitchen), remove them from text, but KEEP punctuation
    for better TTS prosody.
    """
    if not s or not isinstance(s, str):
        return "", []
    text = s.strip()
    tags = [m.group(1).strip() for m in re.finditer(r'#([^\s#.,;:!?()]+)', text)]
    # remove hashtags themselves (not punctuation)
    cleaned = re.sub(r'\s*#([^\s#.,;:!?()]+)', '', text)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    return cleaned, tags

def _lang_code(idx: int, default: str = "en") -> str:
    lang_map = getattr(settings, "LANG_MAP", ["en", "fr", "de"])
    try:
        if isinstance(lang_map, dict):
            return str(lang_map.get(idx, default))
        return str(lang_map[idx])
    except Exception:
        return default

# -------------------------------------------------------------
# OpenAI – Responses primary, Chat fallback for non-gpt-5
# -------------------------------------------------------------
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

def _resolve_openai_key() -> str:
    """
    Try in order:
      - ENV OPENAI_API_KEY
      - settings.OPENAI_API_KEY
      - ./openai.key next to script
    """
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        key = (getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if not key:
        for d in (Path.cwd(), Path(__file__).resolve().parent):
            f = d / "openai.key"
            if f.exists():
                try:
                    t = f.read_text(encoding="utf-8").strip()
                    if t:
                        return t
                except Exception:
                    pass
    return key

def _extract_text_from_responses(resp) -> str:
    """
    Robust extractor across GPT-4/5 Responses output.
    Priority:
      - resp.output_text
      - dump["output"][i]["content"][j] where type in {"text","output_text","summary_text"}
      - dump["message"]["content"][k]["text"]
      - final sweep: any 'text'/'output_text'/'summary_text' fields
    """
    t = getattr(resp, "output_text", None)
    if isinstance(t, str) and t.strip():
        return t.strip()

    try:
        dump = resp.model_dump()
    except Exception:
        dump = None

    def _collect_from_output(d: dict) -> List[str]:
        out = d.get("output")
        parts: List[str] = []
        if isinstance(out, list):
            for item in out:
                if isinstance(item, dict) and item.get("type") == "message":
                    for c in item.get("content", []) or []:
                        if isinstance(c, dict):
                            ctype = c.get("type")
                            if ctype in ("text", "output_text", "summary_text"):
                                val = (c.get("text") or c.get("output_text") or c.get("summary_text") or "").strip()
                                if val:
                                    parts.append(val)
        return parts

    def _collect_from_message(d: dict) -> List[str]:
        msg = d.get("message")
        parts: List[str] = []
        if isinstance(msg, dict):
            mc = msg.get("content")
            if isinstance(mc, list):
                for c in mc:
                    if isinstance(c, dict) and c.get("type") == "text":
                        txt = (c.get("text") or "").strip()
                        if txt:
                            parts.append(txt)
        return parts

    if isinstance(dump, dict):
        parts = _collect_from_output(dump)
        if parts:
            return "\n".join(parts).strip()
        parts = _collect_from_message(dump)
        if parts:
            return "\n".join(parts).strip()

        # last-chance sweep
        text_candidates: List[str] = []
        def _walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k in ("text", "output_text", "summary_text") and isinstance(v, str) and v.strip():
                        text_candidates.append(v.strip())
                    _walk(v)
            elif isinstance(x, list):
                for it in x:
                    _walk(it)
        _walk(dump)
        if text_candidates:
            return "\n".join(text_candidates).strip()

    return ""

def _openai_generate(model: str, prompt: str, max_out_tokens: int = 5000, request_timeout: int = 500) -> str:
    """
    - GPT-5 family: Responses API with input=str ONLY (proven to return text).
      If empty, retry once with larger max_output_tokens and compact verbosity;
      else write a debug dump file.
    - Others: Responses first → fallback Chat (max_completion_tokens).
    """
    if OpenAI is None:
        raise RuntimeError("openai package not installed. Run: pip install --upgrade openai")

    api_key = _resolve_openai_key()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing (ENV/settings/openai.key).")

    client = OpenAI(api_key=api_key)
    is_gpt5 = model.strip().startswith("gpt-5")

    def _resp_call(max_tokens: int, compact: bool = False) -> str:
        kwargs = dict(
            model=model.strip(),
            input=prompt,
            max_output_tokens=max_tokens,
            timeout=request_timeout,
            # کم کردن بودجه‌ی reasoning تا متن زودتر بیاید
            reasoning={"effort": "low"},
        )
        # بعضی snapshotها فیلد text را قبول دارند؛ compact کمک می‌کند خروجی کوتاه‌تر و مستقیم‌تر باشد
        if compact:
            kwargs["text"] = {"format": {"type": "text"}, "verbosity": "compact"}

        resp = client.responses.create(**kwargs)
        txt = _extract_text_from_responses(resp)
        if txt:
            return txt.strip()

        # write debug dump when empty
        try:
            dbg = resp.model_dump() if hasattr(resp, "model_dump") else {"repr": repr(resp)}
            Path("last_openai_response.json").write_text(json.dumps(dbg, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[DEBUG] wrote last_openai_response.json (empty extract)")
        except Exception:
            pass
        return ""

    # ensure enough tokens for gpt-5 to reach text (avoid reasoning-only)
    if is_gpt5 and max_out_tokens < 1024:
        max_out_tokens = 1024

    # --- Responses API (input=str) ---
    try:
        out = _resp_call(max_out_tokens, compact=False)
        if out:
            return out
        if is_gpt5:
            # یک بار دیگر با بودجه‌ی بالاتر و compact
            out2 = _resp_call(max(1536, max_out_tokens), compact=True)
            if out2:
                return out2
            return ""
    except Exception as e:
        if is_gpt5:
            print(f"[WARN] OpenAI Responses error on {model}: {e}")
            return ""
        # else fall through to Chat fallback

    # --- Fallback: Chat Completions (ONLY for non-gpt-5) ---
    if not is_gpt5:
        try:
            cc = client.chat.completions.create(
                model=model.strip(),
                messages=[
                    {"role": "system", "content": "Return plain text only; exactly the requested lines."},
                    {"role": "user",   "content": prompt},
                ],
                max_completion_tokens=max_out_tokens,
                timeout=request_timeout,
            )
            if cc and getattr(cc, "choices", None):
                m = cc.choices[0].message
                if m and getattr(m, "content", None):
                    return m.content.strip()
        except Exception as e:
            print(f"[WARN] OpenAI Chat fallback error on {model}: {e}")

    return ""

# -------------------------------------------------------------
# Ollama
# -------------------------------------------------------------
def _ollama_generate(host: str, model: str, prompt: str,
                     timeout: int = 500) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Call local Ollama /api/generate. Returns (extracted_text, raw_body, parsed_json)."""
    if requests is None:
        print("[WARN] 'requests' not available; cannot call Ollama.")
        return "", "", None

    url = host.rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "max_output_tokens": 1024}
    }
    try:
        r = requests.post(url, json=payload, timeout=timeout)
    except Exception as e:
        print(f"[ERROR] Ollama HTTP error: {e}")
        return "", "", None

    raw_txt = r.text or ""
    try:
        parsed = r.json()
    except Exception:
        parsed = None

    extracted = ""
    if parsed and isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
        extracted = parsed["response"].strip()
    if not extracted and raw_txt:
        extracted = raw_txt.strip()

    return extracted, raw_txt, parsed

# -------------------------------------------------------------
# Prompt builder – punctuation + mandatory hashtags
# -------------------------------------------------------------
def _llm_build_prompt(topic: str, level: str, mode: str,
                      primary_code: str, secondary_code: str, n_items: int) -> str:
    mode = (mode or "").strip().lower()

    if mode == "vocab":
        # --- VOCAB MODE: produce single words / short noun phrases (no sentences) ---
        examples = [
            # primary (word/phrase) + visual hashtags  |  secondary (translation)
            "coffee #cafe #cup #barista #indoor | Kaffee",
            "park bench #park #bench #outdoor | banco del parque",
        ]
        examples_text = "\n".join(examples)
        return f"""
You are a strict formatter for LANGUAGE-LEARNING VOCAB lists.

TASK:
- Generate EXACTLY {n_items} everyday VOCAB items for the TOPIC: "{topic}".
- CEFR level: {level}
- Output languages: primary={primary_code} ; secondary={secondary_code}

WHAT TO OUTPUT (MANDATORY):
- Each item MUST be a SINGLE WORD or a VERY SHORT NOUN PHRASE (1–3 words) in the PRIMARY language.
- NO full sentences. NO trailing punctuation at the end of the primary term.
- After the primary term, append 2–6 ENGLISH **visual** hashtags (nouns/adjectives we can photograph).
- On the same line, add a pipe `|` and give the SECONDARY translation of that term.

FORMAT (EXACTLY):
- Return exactly {n_items} non-empty lines.
- Each line: <primary term + hashtags> | <secondary translation>
- No numbering, bullets, extra commentary, or blank lines.
- Do NOT wrap anything in quotes. Follow normal capitalization rules of each language
  (e.g., German nouns capitalized; otherwise lowercase unless proper nouns).

HASHTAGS (MANDATORY):
- 2–6 English visual tags after the term (space-separated), e.g. #cup #kitchen #indoor.
- Prefer concrete objects, places, roles. Avoid verbs/abstract tags; avoid duplicates.
- If relevant, include one of #indoor or #outdoor.

SELECTION GUIDELINES:
- Keep vocabulary appropriate to CEFR {level}.
- Prefer concrete, high-frequency words/short noun phrases related to "{topic}".
- Avoid rare/proper names unless the topic truly requires them.

FEW-SHOT EXAMPLES (FORMAT ONLY; adjust languages to primary/secondary codes above):
{examples_text}

Return only {n_items} lines, nothing else.
""".strip()

    # --- SCENARIO MODE (sentences) ---
    examples = [
        "I need a coffee. #coffee #cafe | J'ai besoin d'un café.",
        "The shop is closed today. #shop #closed | Le magasin est fermé aujourd'hui.",
    ]
    examples_text = "\n".join(examples)
    return f"""
You are a strict formatter for language-learning phrases.

TASK:
- Generate EXACTLY {n_items} short, natural sentences for the TOPIC: "{topic}".
- CEFR level: {level}
- Output languages: primary={primary_code} ; secondary={secondary_code}

OUTPUT FORMAT (MANDATORY):
- Respond with exactly {n_items} non-empty lines.
- Each line MUST be exactly: <Primary sentence with hashtags> | <Secondary translation>
- Do NOT add extra commentary, numbering, bullets, or blank lines.
- Do NOT wrap sentences in quotes.
- DO NOT include chain-of-thought, analysis, or explanations; OUTPUT ONLY THE FINAL LINES.

PUNCTUATION (MANDATORY):
- Use proper, standard punctuation in BOTH primary and secondary (commas, periods, question/exclamation marks).
- End each sentence with appropriate terminal punctuation (., ?, !).
- Keep punctuation INSIDE the sentence; hashtags come AFTER the sentence (separated by a space).

HASHTAGS (MANDATORY, FOR IMAGE SEARCH):
- Append 3–6 English visual hashtags at the END of the PRIMARY sentence (AFTER the closing punctuation).
- Visual means nouns/adjectives we can photograph (e.g., #grandfather #storybook #livingroom #sofa #indoor).
- Include at least one of: a concrete object, a place, a person/role, and if relevant: #indoor or #outdoor.
- Avoid verbs, abstract terms, and duplicates. Lowercase; no punctuation in tags.

STYLE:
- Primary/secondary should be short, everyday sentences suitable for CEFR {level}.
- Keep vocabulary and grammar aligned with {level}.

FEW-SHOT EXAMPLES (follow THIS format strictly):
{examples_text}

Return only {n_items} lines, nothing else.
""".strip()


# -------------------------------------------------------------
# LLM dispatcher → returns N lines "<Primary> | <Secondary>"
# -------------------------------------------------------------
def _generate_pipe_lines_with_llm() -> Tuple[List[str], str, str, str]:
    topic = getattr(settings, "LLM_TOPIC", "") or Path(getattr(settings, "INPUT_FILENAME", "topic")).stem
    level = getattr(settings, "LEVEL", "A1")
    mode  = getattr(settings, "MODE", "scenario")
    n     = int(getattr(settings, "LLM_ITEMS", 8))

    lang_map = getattr(settings, "LANG_MAP", ["en", "fr", "de"])

    def code_at(idx: int, default: str = "en") -> str:
        if isinstance(lang_map, dict):
            return lang_map.get(idx, default)
        try:
            return lang_map[idx]
        except Exception:
            return default

    p_idx = int(getattr(settings, "PRIMARY_LANG_IDX", 0))
    s_idx = int(getattr(settings, "SECONDARY_LANG_IDX", 1))
    primary_code = code_at(p_idx, "en")
    secondary_code = code_at(s_idx, "fr")

    prompt = _llm_build_prompt(topic, level, mode, primary_code, secondary_code, n)

    provider = str(getattr(settings, "LLM_PROVIDER", "ollama")).lower().strip()
    raw_text: str = ""

    if provider == "openai":
        openai_model = getattr(settings, "OPENAI_MODEL", "gpt-4o-mini")
        print(f"[INFO] Requesting LLM (provider=openai, model={openai_model}) topic='{topic}' items={n}")
        try:
            raw_text = _openai_generate(openai_model, prompt, max_out_tokens=5000, request_timeout=500).strip()
        except Exception as e:
            print(f"[WARN] OpenAI call failed ({e}); falling back to Ollama...")
            provider = "ollama"

    if provider != "openai":
        host  = getattr(settings, "OLLAMA_BASE_URL", getattr(settings, "OLLAMA_HOST", "http://localhost:11434"))
        model = getattr(settings, "OLLAMA_MODEL", getattr(settings, "LLM_MODEL", "llama3.1:8b"))
        print(f"[INFO] Requesting LLM (provider=ollama, model={model}) topic='{topic}' items={n}")
        extracted, raw_body, _ = _ollama_generate(host, model, prompt, timeout=500)
        raw_text = (extracted if extracted else (raw_body or "")).strip()

    if not raw_text:
        print("[WARN] LLM returned no usable text.")
        return [], topic, primary_code, secondary_code

    # Strict parse: "<A> | <B>" per line
    lines: List[str] = []
    for m in re.finditer(r'^\s*(.+?)\s*\|\s*(.+?)\s*$', raw_text, flags=re.MULTILINE):
        a = re.sub(r'\s+', ' ', m.group(1)).strip()
        b = re.sub(r'\s+', ' ', m.group(2)).strip()
        if a and b:
            lines.append(f"{a} | {b}")

    if len(lines) >= n:
        final, seen = [], set()
        for ln in lines:
            if ln in seen:
                continue
            seen.add(ln)
            final.append(ln)
            if len(final) >= n:
                break
        return final, topic, primary_code, secondary_code

    # Fallbacks: pair adjacent lines if needed
    raw_lines = [ln.strip() for ln in re.split(r'\r?\n', raw_text) if ln.strip()]
    i = 0
    while i < len(raw_lines) and len(lines) < n:
        cur = raw_lines[i]
        if '|' in cur:
            parts = [p.strip() for p in re.split(r'\s*\|\s*', cur) if p.strip()]
            if len(parts) >= 2:
                lines.append(f"{parts[0]} | {parts[1]}")
                i += 1
                continue
        if i + 1 < len(raw_lines):
            a, b = cur, raw_lines[i + 1]
            if '|' in b and '|' not in a:
                parts = [p.strip() for p in re.split(r'\s*\|\s*', b) if p.strip()]
                if len(parts) >= 2:
                    lines.append(f"{a} | {parts[-1]}")
                    i += 2
                    continue
            lines.append(f"{a} | {b}")
            i += 2
            continue
        i += 1

    final, seen = [], set()
    for ln in lines:
        if ln in seen:
            continue
        seen.add(ln)
        final.append(ln)
        if len(final) >= n:
            break

    return final, topic, primary_code, secondary_code

# -------------------------------------------------------------
# Main pipeline
# -------------------------------------------------------------
def main() -> None:
    # sanity
    if AudioSegment is None:
        print("[ERROR] pydub is not available. Install requirements and try again.")
        sys.exit(1)
    if write_srt_from_cues is None or write_ass_from_cues is None:
        print("[ERROR] subtitles module functions are unavailable.")
        sys.exit(1)
    if build_audio_snapped_to_cues is None:
        print("[ERROR] audio_utils builder is unavailable.")
        sys.exit(1)
    if render_video_single_or_none is None:
        print("[ERROR] video_utils is unavailable.")
        sys.exit(1)

    # pydub ffmpeg
    AudioSegment.converter = detect_ffmpeg_path(getattr(settings, "FFMPEG_FALLBACK", "ffmpeg"))

    MODE  = str(getattr(settings, "MODE", "scenario")).lower()
    LEVEL = str(getattr(settings, "LEVEL", "A1"))

    # repeat config
    repeat_conf = settings.VOCAB_REPEAT if MODE == "vocab" else settings.SCENARIO_REPEAT
    PRIMARY_REPEAT_CNT   = int(repeat_conf.get("primary", 1))
    SECONDARY_REPEAT_CNT = int(repeat_conf.get("secondary", 2))
    PAUSE_REP_MS         = int(repeat_conf.get("pause_rep", _AU_PAUSE_REP))
    PAUSE_SENT_MS        = int(repeat_conf.get("pause_sent", _AU_PAUSE_SENT))

    provider_selected = getattr(settings, "TTS_PROVIDER", "gtts")

    # Input source
    use_llm = bool(getattr(settings, "GENERATE_WITH_LLM", False))
    raw_lines: List[str] = []
    scenario_stem: Optional[str] = None

    if use_llm:
        try:
            raw_lines, topic_used, llm_primary_code, llm_secondary_code = _generate_pipe_lines_with_llm()
            scenario_stem = _slug(topic_used or "generated")
            if not raw_lines:
                print("[WARN] LLM returned no valid lines; falling back to file input.")
                use_llm = False
        except Exception as e:
            print(f"[ERROR] LLM generation failed: {e}")
            use_llm = False

    if not use_llm:
        input_dir  = Path(getattr(settings, "INPUT_DIR", "Text"))
        input_name = Path(getattr(settings, "INPUT_FILENAME", "input.txt"))
        input_txt  = (input_dir / input_name).resolve()
        if not input_txt.exists():
            print(f"[ERROR] Input text not found: {input_txt}")
            sys.exit(1)
        print(f"[INFO] Using input file: {input_txt} | MODE={MODE}")
        raw_lines = [line.strip() for line in input_txt.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
        scenario_stem = _slug(input_txt.stem)

    # parse lines into triples (primary, secondary, tags)
    logical_lines: List[Tuple[str, str, List[str]]] = []
    p_idx = int(getattr(settings, "PRIMARY_LANG_IDX", 0))
    s_idx = int(getattr(settings, "SECONDARY_LANG_IDX", 1))
    max_idx = max(p_idx, s_idx)

    def _safe_part(parts: List[str], idx: int) -> str:
        try:
            return parts[idx]
        except Exception:
            return ""

    for line in raw_lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) > max_idx:
            primary_raw   = _safe_part(parts, p_idx)
            secondary_raw = _safe_part(parts, s_idx)
        elif len(parts) >= 2:
            primary_raw, secondary_raw = parts[0], parts[1]
        else:
            print(f"[WARN] Skipping malformed line: {line}")
            continue

        primary_raw = strip_bullet_prefix(primary_raw)

        primary_clean, tags = _extract_hashtags_and_clean(primary_raw)
        primary_clean = primary_clean or primary_raw.strip()

        # local auto-tags fallback if LLM didn't provide
        if not tags:
            try:
                if sentence_to_query_extras is not None:
                    extra = sentence_to_query_extras(
                        primary_clean,
                        topic=(scenario_stem or "").replace('_', ' '),
                        level=LEVEL,
                    )
                    cand = []
                    for w in (extra or []):
                        w = str(w).strip().lower()
                        w = "".join(ch for ch in w if ch.isalnum() or ch in ("-",))
                        if 2 < len(w) < 24:
                            cand.append(w)
                        if len(cand) >= 6:
                            break
                    if cand:
                        tags = cand
            except Exception:
                pass

        logical_lines.append((primary_clean, secondary_raw, tags))

    if not logical_lines:
        print("[WARN] No valid lines to process.")
        sys.exit(0)

    # TTS timing + draft cues
    silence_rep  = _normalize(AudioSegment.silent(duration=PAUSE_REP_MS))
    silence_sent = _normalize(AudioSegment.silent(duration=PAUSE_SENT_MS))

    cues_draft: List[Dict[str, Any]] = []
    t = 0  # ms timeline

    def _tts(text: str, lang_code: str):
        if safe_tts_to_segment is None:
            return _normalize(AudioSegment.silent(duration=800))
        try:
            return safe_tts_to_segment(text, lang_code, provider=provider_selected)
        except TypeError:
            return safe_tts_to_segment(text, lang_code)

    primary_code = _lang_code(p_idx, "en")
    secondary_code = _lang_code(s_idx, "fr")

    for primary, secondary, tags in logical_lines:
        seg_one = _tts(primary, primary_code) or _normalize(AudioSegment.silent(duration=800))
        dur_one = len(seg_one)
        total_ms_primary = PRIMARY_REPEAT_CNT * dur_one + max(0, PRIMARY_REPEAT_CNT - 1) * len(silence_rep)

        cues_draft.append({
            "start": t,
            "end":   t + total_ms_primary,
            "text":  primary,
            "lang":  primary_code,
            "repeat": PRIMARY_REPEAT_CNT,
            "is_primary": True,
            "tags": tags or [],
        })
        t += total_ms_primary

        # gap before translation
        t += max(len(silence_sent), 1700)

        if getattr(settings, "ENABLE_BILINGUAL", True) and secondary:
            seg_two = _tts(secondary, secondary_code) or _normalize(AudioSegment.silent(duration=800))
            dur_two = len(seg_two)
            total_ms_secondary = SECONDARY_REPEAT_CNT * dur_two + max(0, SECONDARY_REPEAT_CNT - 1) * len(silence_rep)

            cues_draft.append({
                "start": t,
                "end":   t + total_ms_secondary,
                "text":  secondary,
                "lang":  secondary_code,
                "repeat": SECONDARY_REPEAT_CNT,
                "is_primary": False,
                "tags": [],
            })
            t += total_ms_secondary

        # gap after each pair
        t += len(silence_sent)

    # Output paths
    out_base = (Path(getattr(settings, "OUTPUT_DIR", "Output")) / (scenario_stem or "output")).resolve()
    OUT_MP3 = str(out_base) + ".mp3"
    OUT_WAV = str(out_base) + ".wav"
    OUT_SRT = str(out_base) + ".srt"
    OUT_ASS = str(out_base) + ".ass"
    OUT_MP4 = str(out_base) + ".mp4"

    # SRT draft
    write_srt_from_cues(cues_draft, OUT_SRT)
    print(f"[OK] SRT draft written: {OUT_SRT}")

    # choose timing source
    if getattr(settings, "READ_TIMING_FROM_EXTERNAL_SRT", False) and os.path.exists(getattr(settings, "EXTERNAL_SRT_PATH", "")):
        cues_src = parse_srt(getattr(settings, "EXTERNAL_SRT_PATH"))
        print(f"[INFO] Using external SRT: {getattr(settings, 'EXTERNAL_SRT_PATH')}")
    else:
        cues_src = cues_draft
        print("[INFO] Using draft SRT (derived from TTS).")

    # snap to ASS grid (optional)
    if getattr(settings, "ALIGN_TO_ASS_CENTISECOND_GRID", False):
        for c in cues_src:
            c["start"] = round_to_ass_grid(c["start"])
            c["end"]   = round_to_ass_grid(c["end"])

    # Final audio
    final_audio = build_audio_from_cues_repeat_all(cues_src, pause_rep_ms=PAUSE_REP_MS)

    # BG music
    bg = None
    if bool(getattr(settings, "BG_ENABLED", True)):
        bg = load_bg_music(getattr(settings, "BG_MUSIC", "bg_music.mp3"), len(final_audio), getattr(settings, "BG_GAIN_DB", -18))

    mixed = final_audio.overlay(bg) if bg else final_audio
    mixed.export(OUT_WAV, format="wav")
    try:
        mixed.export(OUT_MP3, format="mp3", bitrate="192k")
    except Exception as e:
        print(f"[WARN] mp3 export failed: {e}")
    print(f"[OK] Audio written: {OUT_WAV}, {OUT_MP3}")

    # ASS
    vw, vh = map(int, str(getattr(settings, "VIDEO_SIZE", "1920x1080")).split("x"))
    write_ass_from_cues(
        cues_src, OUT_ASS, vw, vh,
        base_font=getattr(settings, "FONT_NAME", "Arial"),
        base_fs=getattr(settings, "FONT_SIZE", 48),
    )
    print(f"[OK] ASS written: {OUT_ASS}")

    # Video render
    try:
        bg_mode = str(getattr(settings, "BG_MODE", "single")).lower().strip()
        if bg_mode == "none":
            render_video_single_or_none(
                OUT_WAV, OUT_ASS, OUT_MP4,
                size=getattr(settings, "VIDEO_SIZE", "1920x1080"),
                fps=getattr(settings, "VIDEO_FPS", 30),
                bg_image=None
            )
        elif bg_mode == "single":
            render_video_single_or_none(
                OUT_WAV, OUT_ASS, OUT_MP4,
                size=getattr(settings, "VIDEO_SIZE", "1920x1080"),
                fps=getattr(settings, "VIDEO_FPS", 30),
                bg_image=getattr(settings, "BG_IMAGE", "bg.jpg")
            )
        elif bg_mode == "per_sentence":
            if get_images_for_cues is None or build_slideshow_video_cfr is None or mux_subs_and_audio_on_video is None:
                raise RuntimeError("Per-sentence image pipeline unavailable (video_utils missing functions).")

            primary_cues = [c for c in cues_src if c.get("is_primary", True)]
            images = get_images_for_cues(primary_cues)

            expanded_images: List[Optional[str]] = []
            last_img = None
            for c in cues_src:
                if c.get("is_primary", True):
                    img = images.pop(0) if images else None
                    last_img = img
                    expanded_images.append(img)
                else:
                    expanded_images.append(last_img)

            slideshow = build_slideshow_video_cfr(
                cues=cues_src,
                per_sentence_images=expanded_images,
                total_audio_ms=len(mixed),
                size=getattr(settings, "VIDEO_SIZE", "1920x1080"),
                fps=getattr(settings, "VIDEO_FPS", 30)
            )
            mux_subs_and_audio_on_video(slideshow, Path(OUT_ASS).resolve(), Path(OUT_WAV).resolve(), OUT_MP4)
        else:
            print(f"[WARN] Unknown BG_MODE={bg_mode}; rendering black background.")
            render_video_single_or_none(
                OUT_WAV, OUT_ASS, OUT_MP4,
                size=getattr(settings, "VIDEO_SIZE", "1920x1080"),
                fps=getattr(settings, "VIDEO_FPS", 30),
                bg_image=None
            )

        print(f"[OK] Final video written: {OUT_MP4}")

    except Exception as e:
        print(f"[ERROR] FFmpeg video render failed: {e}")

# -------------------------------------------------------------
# Entrypoint
# -------------------------------------------------------------
def run():
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Aborted by user.")
    except Exception as e:
        print(f"[FATAL] {e}")

if __name__ == "__main__":
    run()
