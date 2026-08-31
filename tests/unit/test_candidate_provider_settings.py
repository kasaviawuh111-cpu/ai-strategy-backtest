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
