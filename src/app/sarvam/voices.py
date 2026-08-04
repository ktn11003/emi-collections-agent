"""Language -> Bulbul voice mapping, and the languages this deployment offers.

The speaker list is the one bulbul:v3 actually accepts (the API rejects
bulbul:v2-only speakers such as ``anushka`` with a 400 — confirmed on
2026-07-30). Warm, unhurried female voices are chosen deliberately: in
collections, tone changes recovery outcomes.
"""

from __future__ import annotations

# What this deployment offers. Narrower than what Bulbul can speak, on purpose:
# every offered language needs a tested prompt, a tested disclosure line and a
# reviewed guardrail list, so scope is a decision rather than a capability dump.
ACTIVE_LANGUAGES: tuple[str, ...] = ("hi-IN", "en-IN")

# Retained so existing callers keep working; prefer ACTIVE_LANGUAGES.
SOW_LANGUAGES: tuple[str, ...] = ACTIVE_LANGUAGES

# Bulbul v3 covers 11 languages; Saaras v3 covers 23. Translate bridges the gap.
TTS_LANGUAGES: tuple[str, ...] = (
    "hi-IN", "en-IN", "ta-IN", "te-IN", "ml-IN", "kn-IN",
    "bn-IN", "gu-IN", "mr-IN", "pa-IN", "od-IN",
)

BULBUL_V3_SPEAKERS: frozenset[str] = frozenset({
    "aditya", "ritu", "ashutosh", "priya", "neha", "rahul", "pooja", "rohan",
    "simran", "kavya", "amit", "dev", "ishita", "shreya", "ratan", "varun",
    "manan", "sumit", "roopa", "kabir", "aayan", "shubh", "advait", "anand",
    "tanya", "tarun", "sunny", "mani", "gokul", "vijay", "shruti", "suhani",
    "mohit", "kavitha", "rehan", "soham", "rupali", "niharika",
})

# One warm female voice per language, with a male alternate for A/B testing.
VOICE_BY_LANGUAGE: dict[str, str] = {
    "hi-IN": "priya",
    "en-IN": "neha",
    "ta-IN": "kavitha",
    "te-IN": "shruti",
    "ml-IN": "roopa",
    "kn-IN": "suhani",
    "bn-IN": "rupali",
    "gu-IN": "tanya",
    "mr-IN": "ishita",
    "pa-IN": "simran",
    "od-IN": "niharika",
}

ALT_VOICE_BY_LANGUAGE: dict[str, str] = {
    "hi-IN": "aditya",
    "en-IN": "rahul",
    "ta-IN": "gokul",
    "te-IN": "vijay",
    "ml-IN": "advait",
    "kn-IN": "mani",
}

LANGUAGE_NAMES: dict[str, str] = {
    "hi-IN": "Hindi", "en-IN": "English", "ta-IN": "Tamil", "te-IN": "Telugu",
    "ml-IN": "Malayalam", "kn-IN": "Kannada", "bn-IN": "Bengali",
    "gu-IN": "Gujarati", "mr-IN": "Marathi", "pa-IN": "Punjabi", "od-IN": "Odia",
}

DEFAULT_VOICE = "priya"
DEFAULT_LANGUAGE = "hi-IN"


def voice_for(language: str, *, male: bool = False) -> str:
    table = ALT_VOICE_BY_LANGUAGE if male else VOICE_BY_LANGUAGE
    candidate = table.get(language) or VOICE_BY_LANGUAGE.get(language) or DEFAULT_VOICE
    return candidate if candidate in BULBUL_V3_SPEAKERS else DEFAULT_VOICE


def tts_language_for(language: str) -> str:
    """Nearest language Bulbul can actually speak.

    Saaras understands 23 languages but Bulbul speaks 11. If a borrower answers
    in, say, Maithili, we still reply — in Hindi — rather than failing the turn.
    """
    return language if language in TTS_LANGUAGES else DEFAULT_LANGUAGE


def language_name(language: str) -> str:
    return LANGUAGE_NAMES.get(language, language)
