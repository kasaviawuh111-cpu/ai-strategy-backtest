from __future__ import annotations

import pytest
from pydantic import ValidationError

from ashare_lab.settings import AppSettings


def test_cors_origins_are_normalized_and_deduplicated() -> None:
    settings = AppSettings(
        _env_file=None,
        cors_allowed_origins=(
            "https://example.workbuddy.link/, https://api.example.cn, "
            "https://example.workbuddy.link"
        ),
    )

    assert settings.cors_origins == (
        "https://example.workbuddy.link",
        "https://api.example.cn",
    )


@pytest.mark.parametrize(
    "origin",
    (
        "*",
        "example.workbuddy.link",
        "https://user:secret@example.workbuddy.link",
        "https://example.workbuddy.link/path",
    ),
)
def test_cors_origins_reject_unsafe_values(origin: str) -> None:
    with pytest.raises(ValidationError, match="CORS_ALLOWED_ORIGINS"):
        AppSettings(_env_file=None, cors_allowed_origins=origin)


def test_production_cors_requires_https() -> None:
    with pytest.raises(ValidationError, match="must use https"):
        AppSettings(
            _env_file=None,
            app_env="production",
            cors_allowed_origins="http://example.workbuddy.link",
        )
