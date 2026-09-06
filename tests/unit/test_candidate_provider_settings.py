from __future__ import annotations

import pytest
from pydantic import ValidationError

from ashare_lab.settings import AppSettings


def test_candidate_provider_is_disabled_by_default_and_secret_is_excluded() -> None:
    secret = "provider-secret-must-not-leak"
    settings = AppSettings(candidate_provider_api_key=secret)

    assert settings.candidate_provider_mode == "disabled"
    assert settings.candidate_provider_api_key is not None
    assert settings.candidate_provider_api_key.get_secret_value() == secret
    assert "candidate_provider_api_key" not in settings.model_dump()
    assert settings.candidate_provider_thinking == "disabled"
    assert settings.candidate_provider_reasoning_effort is None
    assert settings.plan_deep_provider_mode == "inherit_extract_fast"
    assert secret not in repr(settings)


@pytest.mark.parametrize(
    "values",
    [
        {"candidate_provider_model": "fixture-model"},
        {"candidate_provider_endpoint": "https://gateway.example.test/v1/chat/completions"},
    ],
)
def test_enabled_provider_requires_endpoint_and_model(values: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="requires endpoint and model"):
        AppSettings(candidate_provider_mode="openai_compatible", **values)


def test_enabled_provider_requires_non_empty_api_key() -> None:
    with pytest.raises(ValidationError, match="requires an API key"):
        AppSettings(
            candidate_provider_mode="openai_compatible",
            candidate_provider_endpoint="https://gateway.example.test/v1/chat/completions",
            candidate_provider_model="fixture-model",
        )


@pytest.mark.parametrize(
    "values",
    [
        {"research_provider_endpoint": "https://gateway.example.test/responses"},
        {"research_provider_model": "fixture-model"},
        {"research_provider_api_key": "fixture-secret"},
        {
            "research_provider_endpoint": "https://gateway.example.test/responses",
            "research_provider_model": "fixture-model",
        },
    ],
)
def test_research_provider_rejects_partial_configuration(values: dict[str, str]) -> None:
    with pytest.raises(
        ValidationError,
        match="requires endpoint, model and API key together",
    ):
        AppSettings(**values)


def test_complete_research_provider_configuration_keeps_secret_hidden() -> None:
    secret = "research-secret-must-not-leak"
    settings = AppSettings(
        research_provider_endpoint="https://gateway.example.test/responses",
        research_provider_model="deepseek-v4-flash",
        research_provider_api_key=secret,
    )

    assert settings.research_provider_model == "deepseek-v4-flash"
    assert settings.research_provider_timeout_seconds == 30.0
    assert settings.research_provider_api_key is not None
    assert secret not in repr(settings)
    assert secret not in str(settings.model_dump())


def test_volcengine_research_mode_only_requires_its_api_key() -> None:
    secret = "volcengine-search-secret-must-not-leak"
    settings = AppSettings(
        research_provider_mode="volcengine_web_search",
        research_provider_api_key=secret,
    )

    assert settings.research_provider_mode == "volcengine_web_search"
    assert settings.research_provider_endpoint is None
    assert settings.research_provider_model is None
    assert secret not in repr(settings)
    assert secret not in str(settings.model_dump())


def test_enabled_provider_configuration_is_environment_driven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "provider-secret-must-not-leak"
    monkeypatch.setenv("CANDIDATE_PROVIDER_MODE", "openai_compatible")
    monkeypatch.setenv(
        "CANDIDATE_PROVIDER_ENDPOINT",
        "https://gateway.example.test/v1/chat/completions",
    )
    monkeypatch.setenv("CANDIDATE_PROVIDER_NAME", "internal-gateway")
    monkeypatch.setenv("CANDIDATE_PROVIDER_MODEL", "fixture-model")
    monkeypatch.setenv("CANDIDATE_PROVIDER_API_KEY", secret)
    monkeypatch.setenv("CANDIDATE_PROVIDER_TIMEOUT_SECONDS", "1.25")
    monkeypatch.setenv("CANDIDATE_PROVIDER_RESPONSE_MODE", "json_object")

    settings = AppSettings(_env_file=None)

    assert settings.candidate_provider_mode == "openai_compatible"
    assert settings.candidate_provider_name == "internal-gateway"
    assert settings.candidate_provider_model == "fixture-model"
    assert settings.candidate_provider_timeout_seconds == 1.25
    assert settings.candidate_provider_response_mode == "json_object"
    assert settings.candidate_provider_api_key is not None
    assert settings.candidate_provider_api_key.get_secret_value() == secret
    assert secret not in repr(settings)
    assert secret not in str(settings.model_dump())


def test_split_deepseek_profiles_are_environment_driven_and_secrets_stay_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extract_secret = "extract-secret-must-not-leak"
    plan_secret = "plan-secret-must-not-leak"
    environment = {
        "CANDIDATE_PROVIDER_MODE": "openai_compatible",
        "CANDIDATE_PROVIDER_ENDPOINT": "https://api.deepseek.com/chat/completions",
        "CANDIDATE_PROVIDER_NAME": "deepseek",
        "CANDIDATE_PROVIDER_MODEL": "deepseek-v4-flash",
        "CANDIDATE_PROVIDER_API_KEY": extract_secret,
        "CANDIDATE_PROVIDER_RESPONSE_MODE": "json_object",
        "PLAN_DEEP_PROVIDER_MODE": "openai_compatible",
        "PLAN_DEEP_PROVIDER_ENDPOINT": "https://api.deepseek.com/chat/completions",
        "PLAN_DEEP_PROVIDER_NAME": "deepseek",
        "PLAN_DEEP_PROVIDER_MODEL": "deepseek-v4-pro",
        "PLAN_DEEP_PROVIDER_API_KEY": plan_secret,
        "PLAN_DEEP_PROVIDER_RESPONSE_MODE": "json_object",
        "PLAN_DEEP_PROVIDER_THINKING": "enabled",
        "PLAN_DEEP_PROVIDER_REASONING_EFFORT": "high",
        "PLAN_DEEP_PROVIDER_TIMEOUT_SECONDS": "180",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    settings = AppSettings(_env_file=None)

    assert settings.candidate_provider_model == "deepseek-v4-flash"
    assert settings.candidate_provider_thinking == "disabled"
    assert settings.plan_deep_provider_model == "deepseek-v4-pro"
    assert settings.plan_deep_provider_thinking == "enabled"
    assert settings.plan_deep_provider_reasoning_effort == "high"
    assert settings.plan_deep_provider_timeout_seconds == 180.0
    assert settings.plan_deep_provider_api_key is not None
    assert settings.plan_deep_provider_api_key.get_secret_value() == plan_secret
    rendered = f"{settings!r} {settings.model_dump()}"
    assert extract_secret not in rendered
    assert plan_secret not in rendered


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"candidate_provider_reasoning_effort": "high"},
            "extract_fast reasoning effort requires enabled thinking",
        ),
        (
            {
                "candidate_provider_mode": "openai_compatible",
                "candidate_provider_endpoint": "https://gateway.example.test/chat/completions",
                "candidate_provider_name": "fixture-gateway",
                "candidate_provider_model": "fixture-model",
                "candidate_provider_api_key": "fixture-secret",
                "candidate_provider_thinking": "enabled",
                "candidate_provider_reasoning_effort": "high",
            },
            "only supported for DeepSeek",
        ),
        (
            {
                "plan_deep_provider_mode": "openai_compatible",
                "plan_deep_provider_name": "deepseek",
                "plan_deep_provider_endpoint": "https://api.deepseek.com/chat/completions",
                "plan_deep_provider_model": "deepseek-v4-pro",
                "plan_deep_provider_api_key": "fixture-secret",
                "plan_deep_provider_thinking": "enabled",
            },
            "enabled thinking requires a reasoning effort",
        ),
        (
            {"plan_deep_provider_model": "silently-ignored-model"},
            "PLAN_DEEP_PROVIDER_MODE must be openai_compatible",
        ),
    ],
)
def test_language_profile_configuration_fails_closed(
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        AppSettings(**values)


def test_plan_deep_timeout_is_independent_from_extract_fast_limit() -> None:
    settings = AppSettings(plan_deep_provider_timeout_seconds=300.0)

    assert settings.candidate_provider_timeout_seconds == 8.0
    assert settings.plan_deep_provider_timeout_seconds == 300.0
    with pytest.raises(ValidationError):
        AppSettings(candidate_provider_timeout_seconds=30.01)
    with pytest.raises(ValidationError):
        AppSettings(plan_deep_provider_timeout_seconds=300.01)


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("https://user:password@gateway.example.test/v1/chat/completions", "credentials"),
        (
            "https://gateway.example.test/v1/chat/completions?api_key=unsafe",
            "query or fragment",
        ),
    ],
)
def test_provider_endpoint_cannot_embed_credentials_or_query(
    endpoint: str,
    message: str,
) -> None:
    secret = "provider-secret-must-not-leak"

    with pytest.raises(ValidationError, match=message) as caught:
        AppSettings(
            candidate_provider_mode="openai_compatible",
            candidate_provider_endpoint=endpoint,
            candidate_provider_model="fixture-model",
            candidate_provider_api_key=secret,
        )

    assert secret not in str(caught.value)


def test_production_provider_endpoint_must_use_https() -> None:
    with pytest.raises(ValidationError, match="must use https"):
        AppSettings(
            app_env="production",
            candidate_provider_mode="openai_compatible",
            candidate_provider_endpoint="http://127.0.0.1:11434/v1/chat/completions",
            candidate_provider_model="fixture-model",
        )
