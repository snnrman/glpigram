"""Conservative service-name classification for access requests."""

from __future__ import annotations

import re
import unicodedata


def _word_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(sorted((re.escape(word) for word in words), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


# Explicit product spellings. Russian endings are listed so that names remain
# whole-word matches instead of matching arbitrary words with the same prefix.
_CLAUDE = _word_pattern(
    (
        "claude.ai",
        "claude.com",
        "claude code",
        "claude",
        "claud",
        "клод",
        "клода",
        "клоду",
        "клодом",
        "клоде",
        "клоды",
        "клодов",
        "клодам",
        "клодами",
        "клодах",
        "клодик",
        "клодика",
        "клодику",
        "клодиком",
        "клодике",
        "клодики",
        "клодиков",
        "клодикам",
        "клодиками",
        "клодиках",
    )
)
_AMBIGUOUS = _word_pattern(
    (
        "клауд",
        "клауда",
        "клауду",
        "клаудом",
        "клауде",
        "клауды",
        "клаудов",
        "клаудам",
        "клаудами",
        "клаудах",
        "клоуд",
        "клоуда",
        "клоуду",
        "клоудом",
        "клоуде",
        "клоуды",
        "клоудов",
        "клоудам",
        "клоудами",
        "клоудах",
        "clod",
        "cloude",
        "cluade",
        "клава",
        "клавы",
        "клаве",
        "клаву",
        "клавой",
        "клавою",
        "клав",
        "клавам",
        "клавами",
        "клавдия",
        "клавдии",
        "клавдие",
        "клавдию",
        "клавдией",
        "клавдию",
        "клавдиям",
        "клавдиями",
        "клавдиях",
        "клавдий",
        "клавдия",
        "клавдию",
        "клавдием",
        "клавдии",
        "клавдии",
        "anthropic",
        "антропик",
        "sonnet",
        "сонет",
        "соннет",
        "opus",
        "опус",
        "haiku",
        "хайку",
    )
)
_OTHER = _word_pattern(
    (
        "jira",
        "джира",
        "джиры",
        "джире",
        "джиру",
        "джирой",
        "джирою",
        "figma",
        "фигма",
        "фигмы",
        "фигме",
        "фигму",
        "фигмой",
        "фигмою",
        "chatgpt",
        "chat gpt",
        "чатгпт",
        "чат гпт",
        "cursor",
        "курсор",
        "курсора",
        "курсору",
        "курсором",
        "курсоре",
        "miro",
        "миро",
        "миры",
        "мире",
        "миру",
        "миром",
    )
)
_CLOUD_OTHER = _word_pattern(
    (
        "google cloud",
        "icloud",
        "cloudflare",
        "nextcloud",
        "облачные услуги",
        "облачную услугу",
        "облачный сервис",
        "облачные сервисы",
    )
)
_CLOUD = _word_pattern(
    (
        "cloud",
        "облако",
        "облака",
        "облаку",
        "облаком",
        "облаке",
        "облачный",
        "облачная",
        "облачное",
        "облачные",
    )
)
_CLAUDE_NEGATION = re.compile(
    r"(?:"
    r"(?<!\w)(?:не|без)(?:\s+\w+){0,3}\s+(?:claude|claud|клод(?:ик)?(?:а|у|ом|е|и|ов|ам|ами|ах)?)(?!\w)"
    r"|"
    r"(?<!\w)(?:claude|claud|клод(?:ик)?(?:а|у|ом|е|и|ов|ам|ами|ах)?)(?!\w).{0,32}?\bне\b"
    r")",
    re.IGNORECASE,
)


def classify_access_target(text: str) -> str:
    """Return ``claude``, ``other``, or ``unclear`` for an access request.

    The classifier is intentionally conservative: uncertain product names,
    mentions of multiple products, and an unspecified bare ``cloud`` need
    clarification instead of being treated as a Claude request.
    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    has_claude = _CLAUDE.search(normalized) is not None
    has_ambiguous = _AMBIGUOUS.search(normalized) is not None
    has_other = _OTHER.search(normalized) is not None

    if _CLAUDE_NEGATION.search(normalized):
        return "unclear"
    if has_claude and (has_ambiguous or has_other or _CLOUD_OTHER.search(normalized)):
        return "unclear"
    if has_ambiguous:
        return "unclear"
    if has_claude:
        return "claude"
    if has_other or _CLOUD_OTHER.search(normalized):
        return "other"
    if _CLOUD.search(normalized):
        return "unclear"
    return "other"
