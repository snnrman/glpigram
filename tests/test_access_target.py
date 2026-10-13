from __future__ import annotations

import pytest

from bot.services.access_target import classify_access_target


@pytest.mark.parametrize(
    "text",
    [
        "Нужен Claude",
        "доступ к Claud",
        "доступ к Клоду",
        "Клодом пользуется команда",
        "Нужен Клодик",
        "помогите с Клодиками",
        "claude.ai",
        "claude.com",
        "Claude Code",
    ],
)
def test_explicit_claude_names_are_classified(text: str) -> None:
    assert classify_access_target(text) == "claude"


@pytest.mark.parametrize(
    "text",
    [
        "клауд",
        "клоудом",
        "Clod",
        "Cloude",
        "Cluade",
        "Клава",
        "Клавдии",
        "Клавдием",
        "Anthropic",
        "Антропик",
        "Sonnet",
        "Сонет",
        "Соннет",
        "Opus",
        "Опус",
        "Haiku",
        "Хайку",
    ],
)
def test_ambiguous_names_need_clarification(text: str) -> None:
    assert classify_access_target(text) == "unclear"


@pytest.mark.parametrize(
    "text", ["Google Cloud", "iCloud", "Cloudflare", "Nextcloud", "облачные услуги"]
)
def test_named_cloud_services_are_other(text: str) -> None:
    assert classify_access_target(text) == "other"


def test_bare_cloud_is_unclear_and_other_service_is_other() -> None:
    assert classify_access_target("Нужен cloud") == "unclear"
    assert classify_access_target("Нужен ChatGPT") == "other"


@pytest.mark.parametrize(
    "text",
    [
        "Claude и Jira",
        "Нужен Figma, а Claude просто пишет ошибку",
        "Вместо Claude нужен Cursor",
        "ChatGPT или Claude — пока не решил",
    ],
)
def test_multiple_or_incidental_service_mentions_are_unclear(text: str) -> None:
    assert classify_access_target(text) == "unclear"


def test_explicit_claude_negation_never_selects_claude() -> None:
    assert classify_access_target("Claude не нужен, нужен ChatGPT") == "unclear"
    assert classify_access_target("Claude не нужен") == "unclear"
    assert classify_access_target("не Claude, нужен другой сервис") == "unclear"
    assert classify_access_target("доступ не к Клоду, а к Jira") == "unclear"
    assert classify_access_target("Claude не нужен, а нужна подписка") == "unclear"


@pytest.mark.parametrize(
    "other_name",
    ["Джира", "Фигма", "чатгпт", "чат гпт", "Курсор", "Миро"],
)
def test_cyrillic_other_service_names_mix_conservatively(other_name: str) -> None:
    assert classify_access_target(f"Claude и {other_name}") == "unclear"
    assert classify_access_target(other_name) == "other"


def test_names_match_at_word_boundaries() -> None:
    assert classify_access_target("claudette") == "other"
    assert classify_access_target("мклодикс") == "other"
    assert classify_access_target("(Claude), please") == "claude"


def test_nfkc_and_casefold_are_applied() -> None:
    assert classify_access_target("ＣＬＡＵＤＥ") == "claude"
