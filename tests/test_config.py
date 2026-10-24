from __future__ import annotations

from pathlib import Path

import pytest

from freelance_bot.config import ConfigError, load_settings

FAKE_TOKEN = "123456:TEST-not-a-real-token-abcdef"
FAKE_KEY = "sk-ant-test-not-a-real-key-0000"

MINIMAL_TOML = """
[sources.fl]
enabled = true
interval_sec = {fl_interval}
mode = "{fl_mode}"
daily_auto_limit = 0
urls = ["https://www.fl.ru/rss/?subcategory=798&category=5"]

[sources.kwork]
enabled = true
interval_sec = 300
mode = "draft"
daily_auto_limit = 0
urls = ["https://kwork.ru/projects?c=11"]

[directions.backend]
weight = 1.0
keywords = ["python"]

[directions.telegram]
weight = 1.0
keywords = ["telegram"]

[[intersections]]
directions = ["telegram", "backend"]
bonus = 15
"""


def write_settings(tmp_path: Path, fl_interval: int = 180, fl_mode: str = "draft") -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(MINIMAL_TOML.format(fl_interval=fl_interval, fl_mode=fl_mode), "utf-8")
    return path


def write_env(tmp_path: Path, **values: str) -> Path:
    path = tmp_path / ".env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), "utf-8")
    return path


def test_repo_settings_defaults(repo_root: Path, tmp_path: Path) -> None:
    s = load_settings(repo_root / "settings.toml", env_file=tmp_path / "missing.env")

    fl, kwork = s.sources["fl"], s.sources["kwork"]
    assert fl.urls == [
        f"https://www.fl.ru/rss/all.xml?category={n}" for n in (5, 31, 41, 34)
    ]
    assert (fl.interval_sec, fl.mode, fl.daily_auto_limit, fl.enabled) == (180, "draft", 0, True)
    assert kwork.urls == ["https://kwork.ru/projects?c=11"]
    assert kwork.enabled is True  # включён пользователем 2026-10-22 на свой риск (ADR 0003)
    assert (kwork.interval_sec, kwork.mode, kwork.daily_auto_limit) == (300, "draft", 0)
    assert s.kwork_connects_daily_limit == 0
    assert s.max_age_hours == 6
    assert s.requirement_penalty == 15
    assert s.offer_price_rub is None and s.offer_days is None
    assert 0 < s.min_score <= 100
    assert s.timezone == "Europe/Moscow"
    assert s.dry_run is False
    assert fl.only_free_responses is False  # пользователь 2026-10-22: платные отклики тоже

    assert set(s.directions) == {"backend", "telegram", "ai_agents", "automation", "parsing"}
    for direction in s.directions.values():
        assert direction.weight > 0
        assert any(not kw.isascii() for kw in direction.keywords), "нужны русские слова"
        assert any(kw.isascii() for kw in direction.keywords), "нужны английские слова"
    pairs = {frozenset(i.directions) for i in s.intersections}
    assert {
        frozenset({"telegram", "backend"}),
        frozenset({"ai_agents", "backend"}),
        frozenset({"automation", "backend"}),
    } <= pairs
    assert all(i.bonus > 0 for i in s.intersections)

    all_exclusions = " ".join(w for words in s.exclusions.values() for w in words).lower()
    for word in ("дизайн", "android", "php", "wordpress", "bitrix", "1с"):
        assert word in all_exclusions


def test_secrets_come_from_env_file(tmp_path: Path) -> None:
    env = write_env(
        tmp_path,
        TELEGRAM_BOT_TOKEN=FAKE_TOKEN,
        TELEGRAM_OWNER_ID="424242",
        ANTHROPIC_API_KEY=FAKE_KEY,
        LLM_MODEL="claude-test-model",
    )
    s = load_settings(write_settings(tmp_path), env_file=env)
    assert s.telegram_token == FAKE_TOKEN
    assert s.owner_id == 424242
    assert s.anthropic_api_key == FAKE_KEY
    assert s.llm_model == "claude-test-model"


def test_empty_env_values_mean_absent_and_model_has_default(tmp_path: Path) -> None:
    env = write_env(tmp_path, TELEGRAM_BOT_TOKEN="", TELEGRAM_OWNER_ID="", ANTHROPIC_API_KEY="")
    s = load_settings(write_settings(tmp_path), env_file=env)
    assert s.telegram_token is None
    assert s.owner_id is None
    assert s.anthropic_api_key is None
    assert s.llm_model == "claude-sonnet-5-5"


def test_process_env_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = write_env(tmp_path, TELEGRAM_OWNER_ID="1")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "2")
    assert load_settings(write_settings(tmp_path), env_file=env).owner_id == 2


def test_non_numeric_owner_id_is_config_error(tmp_path: Path) -> None:
    env = write_env(tmp_path, TELEGRAM_OWNER_ID="@gleb")
    with pytest.raises(ConfigError, match="TELEGRAM_OWNER_ID"):
        load_settings(write_settings(tmp_path), env_file=env)


def test_repr_and_str_do_not_leak_secrets(tmp_path: Path) -> None:
    env = write_env(tmp_path, TELEGRAM_BOT_TOKEN=FAKE_TOKEN, ANTHROPIC_API_KEY=FAKE_KEY)
    s = load_settings(write_settings(tmp_path), env_file=env)
    for text in (repr(s), str(s), f"{s}", f"{s!r}"):
        assert FAKE_TOKEN not in text
        assert FAKE_KEY not in text
        assert "TEST-not-a-real" not in text


def test_interval_below_60_is_raised_to_60(tmp_path: Path) -> None:
    s = load_settings(write_settings(tmp_path, fl_interval=5), env_file=tmp_path / "none.env")
    assert s.sources["fl"].interval_sec == 60
    assert s.sources["kwork"].interval_sec == 300


def test_invalid_mode_is_clear_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings(write_settings(tmp_path, fl_mode="turbo"), env_file=tmp_path / "none.env")
    message = str(exc.value)
    assert "sources.fl.mode" in message
    assert "turbo" in message
    assert "notify" in message and "draft" in message and "auto" in message


def test_missing_settings_file_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="settings"):
        load_settings(tmp_path / "nope" / "settings.toml", env_file=tmp_path / "none.env")


def test_broken_toml_is_config_error(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text("[sources.fl\nmode = ", "utf-8")
    with pytest.raises(ConfigError):
        load_settings(path, env_file=tmp_path / "none.env")


def test_intersection_with_unknown_direction_is_config_error(tmp_path: Path) -> None:
    path = write_settings(tmp_path)
    path.write_text(
        path.read_text("utf-8") + '\n[[intersections]]\ndirections = ["telegram", "cooking"]\n'
        "bonus = 5\n",
        "utf-8",
    )
    with pytest.raises(ConfigError, match="cooking"):
        load_settings(path, env_file=tmp_path / "none.env")


def test_offer_values_are_parsed_when_set(tmp_path: Path) -> None:
    path = write_settings(tmp_path)
    path.write_text(path.read_text("utf-8") + "\n[offer]\nprice_rub = 15000\ndays = 5\n", "utf-8")
    s = load_settings(path, env_file=tmp_path / "none.env")
    assert (s.offer_price_rub, s.offer_days) == (15000, 5)


def test_negative_limit_is_config_error(tmp_path: Path) -> None:
    path = write_settings(tmp_path)
    path.write_text(path.read_text("utf-8") + "\n[kwork]\nconnects_daily_limit = -1\n", "utf-8")
    with pytest.raises(ConfigError, match="connects_daily_limit"):
        load_settings(path, env_file=tmp_path / "none.env")


def test_missing_platform_section_defaults_to_disabled_draft(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('[sources.fl]\nurls = ["https://www.fl.ru/rss/all.xml"]\n', "utf-8")
    s = load_settings(path, env_file=tmp_path / "none.env")
    assert s.sources["fl"].enabled is True
    assert s.sources["fl"].mode == "draft"
    assert s.sources["kwork"].enabled is False
    assert s.sources["kwork"].mode == "draft"
    assert s.sources["kwork"].daily_auto_limit == 0
    assert s.kwork_connects_daily_limit == 0


def test_only_free_responses_defaults_to_true_and_can_be_switched_off(tmp_path: Path) -> None:
    s = load_settings(write_settings(tmp_path), tmp_path / "missing.env")
    assert s.sources["fl"].only_free_responses is True

    path = tmp_path / "off.toml"
    path.write_text(
        MINIMAL_TOML.format(fl_interval=180, fl_mode="draft").replace(
            "[sources.kwork]", "only_free_responses = false\n\n[sources.kwork]"
        ),
        encoding="utf-8",
    )
    assert load_settings(path, tmp_path / "missing.env").sources["fl"].only_free_responses is False


def test_only_free_responses_must_be_bool(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(
        MINIMAL_TOML.format(fl_interval=180, fl_mode="draft").replace(
            "[sources.kwork]", 'only_free_responses = "yes"\n\n[sources.kwork]'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="sources.fl.only_free_responses"):
        load_settings(path, tmp_path / "missing.env")


def test_groq_key_and_model_come_from_env_file(tmp_path: Path) -> None:
    env = write_env(
        tmp_path, GROQ_API_KEY="gsk_test-not-a-real-key", GROQ_MODEL="openai/gpt-oss-20b"
    )
    s = load_settings(write_settings(tmp_path), env_file=env)
    assert s.groq_api_key == "gsk_test-not-a-real-key" and s.groq_model == "openai/gpt-oss-20b"
    assert s.has_llm_key and "gsk_test" not in repr(s)
    empty = load_settings(write_settings(tmp_path), env_file=write_env(tmp_path, GROQ_API_KEY=""))
    assert empty.groq_api_key is None and empty.groq_model == "openai/gpt-oss-120b"
    assert not empty.has_llm_key


def test_openai_key_and_model_come_from_env_file(tmp_path: Path) -> None:
    env = write_env(tmp_path, OPENAI_API_KEY="sk-test-not-a-real-key", OPENAI_MODEL="gpt-test")
    s = load_settings(write_settings(tmp_path), env_file=env)
    assert s.openai_api_key == "sk-test-not-a-real-key" and s.openai_model == "gpt-test"
    assert "sk-test" not in repr(s)
    empty = load_settings(write_settings(tmp_path), env_file=write_env(tmp_path, OPENAI_API_KEY=""))
    assert empty.openai_api_key is None and empty.openai_model == "gpt-5-mini"


def test_digest_section_parsed_and_interval_clamped(tmp_path: Path) -> None:
    path = write_settings(tmp_path)
    assert load_settings(path, tmp_path / "no.env").digest_enabled is False  # нет секции
    path.write_text(
        path.read_text() + "\n[digest]\nenabled = true\nstart_hour = 9\ninterval_min = 5\n"
    )
    s = load_settings(path, tmp_path / "no.env")
    assert (s.digest_enabled, s.digest_start_hour, s.digest_interval_min) == (True, 9, 15)
    path.write_text(path.read_text().replace("start_hour = 9", "start_hour = 24"))
    with pytest.raises(ConfigError, match="digest.start_hour"):
        load_settings(path, tmp_path / "no.env")


def test_general_directions_select_active_and_skip_their_intersections(tmp_path: Path) -> None:
    path = write_settings(tmp_path)
    full = load_settings(path, tmp_path / "no.env")
    assert set(full.directions) == {"backend", "telegram"} and len(full.intersections) == 1
    text = '[general]\ndirections = ["backend"]\n' + path.read_text()
    path.write_text(text)
    only = load_settings(path, tmp_path / "no.env")
    assert list(only.directions) == ["backend"] and only.intersections == []
    path.write_text(text.replace('["backend"]', '["frontend"]'))
    with pytest.raises(ConfigError, match="general.directions: неизвестное направление frontend"):
        load_settings(path, tmp_path / "no.env")
