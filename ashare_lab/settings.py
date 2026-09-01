"""Environment-backed configuration shared by API and worker composition roots."""

from __future__ import annotations

import re
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _working_tree_revision() -> str:
    """Return the exact Git revision and make dirty local runs unmistakable."""

    repository = Path(__file__).resolve().parents[1]
    try:
        revision = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ("git", "status", "--porcelain", "--untracked-files=normal"),
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return "unversioned"
    return f"{revision}+dirty" if dirty else revision


def _repository_has_git_metadata() -> bool:
    return (Path(__file__).resolve().parents[1] / ".git").exists()


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    app_env: Literal["local", "test", "staging", "production"] = "local"
    app_host: str = "0.0.0.0"
    app_port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"

    database_url: str = "sqlite+pysqlite:///./var/ashare.db"
    initialize_schema: bool = True
    redis_url: str = "redis://localhost:6379/0"
    queue_backend: Literal["thread", "rq"] = "thread"
    queue_name: str = "backtests"
    local_worker_threads: int = Field(default=1, ge=1, le=16)

    session_reference_mode: Literal["research_300059", "parquet"] = "research_300059"
    session_reference_path: Path = Path("var/data/instrument_sessions.parquet")
    market_data_profile: Literal[
        "generic_parquet",
        "choice_snapshot",
        "composite_snapshot",
        "on_demand_snapshot",
    ] = "generic_parquet"
    event_data_required: bool = False

    choice_snapshot_root: Path = Path("var/snapshots/internal-demo/choice")
    technical_snapshot_root: Path = Path("var/snapshots/internal-demo/technical")
    event_snapshot_root: Path = Path("var/snapshots/internal-demo/events")
    composite_snapshot_root: Path = Path("var/snapshots/internal-demo/composite")
    snapshot_preparation_root: Path = Path("var/snapshots/internal-demo/preparations")
    on_demand_refresh_each_submission: bool = True
    # Server-owned selection for the daily price producer.  A browser, model,
    # or strategy payload must never choose or override this policy.
    on_demand_daily_source: Literal[
        "choice_then_eastmoney",
        "baostock_stock_only",
    ] = "choice_then_eastmoney"

    data_root: Path = Field(
        default=Path("../astock-data-toolkit/astock_data"),
        validation_alias=AliasChoices("DATA_ROOT", "ASTOCK_DATA_DIR"),
    )
    catalog_root: Path = Path("catalogs")
    artifact_root: Path = Path("var/artifacts")
    code_revision: str = Field(default_factory=_working_tree_revision)
    engine_version: str = "2.0.0a0"
    max_body_bytes: int = Field(default=16 * 1024, ge=1, le=1_048_576)
    cors_allowed_origins: str = ""
    web_dist_root: Path | None = None

    candidate_provider_mode: Literal["disabled", "openai_compatible"] = "disabled"
    candidate_provider_name: str = Field(
        default="openai-compatible",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    candidate_provider_endpoint: AnyHttpUrl | None = None
    candidate_provider_api_key: SecretStr | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    candidate_provider_model: str | None = Field(default=None, min_length=1, max_length=128)
    candidate_provider_timeout_seconds: float = Field(default=8.0, ge=0.25, le=30.0)
    candidate_provider_response_mode: Literal["json_schema", "json_object"] = "json_schema"
    candidate_provider_max_request_bytes: int = Field(
        default=256 * 1024,
        ge=1_024,
        le=1_048_576,
    )
    candidate_provider_max_response_bytes: int = Field(
        default=256 * 1024,
        ge=1_024,
        le=1_048_576,
    )
    candidate_prompt_version: str = Field(
        default="ashare-lab.bounded-candidate.prompt.v1",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    candidate_schema_version: str = Field(
        default="ashare-lab.bounded-candidate.schema.v1",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )

    # Strategy v2 is opt-in and server-owned. These values locate immutable
    # content objects; no equivalent path, snapshot id or signing key is
    # accepted from HTTP or the language provider.
    strategy_v2_enabled: bool = False
    strategy_v2_security_master_root: Path = Path("var/snapshots/security_master")
    strategy_v2_security_master_snapshot_id: str | None = Field(
        default=None,
        pattern=r"^security_master:[0-9a-f]{64}$",
    )
    strategy_v2_composite_root: Path = Path("var/snapshots/composite")
    strategy_v2_choice_root: Path | None = None
    strategy_v2_technical_root: Path | None = None
    strategy_v2_producer_snapshot_id: str | None = Field(
        default=None,
        pattern=r"^composite:[0-9a-f]{64}$",
    )
    # Optional exact instrument pins for independently published immutable
    # producers. The browser/model never selects or overrides this map.
    strategy_v2_producer_snapshot_ids: dict[str, str] = Field(default_factory=dict)
    strategy_v2_receipt_signing_key: SecretStr | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    strategy_v2_receipt_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    strategy_v2_snapshot_max_age_days: int = Field(default=30, ge=1, le=3650)
    strategy_v2_commission_rate: Decimal = Field(
        default=Decimal("0.0003"),
        ge=Decimal("0"),
        le=Decimal("0.01"),
    )
    strategy_v2_minimum_commission_cny: Decimal = Field(
        default=Decimal("5"),
        ge=Decimal("0"),
        le=Decimal("1000"),
    )

    @model_validator(mode="after")
    def candidate_provider_configuration_is_safe(self) -> AppSettings:
        """Fail closed for partial provider configuration without exposing secrets."""

        self._validate_cors_origins()
        for symbol, snapshot_id in self.strategy_v2_producer_snapshot_ids.items():
            if re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", symbol) is None:
                raise ValueError("Strategy v2 producer map contains an invalid A-share symbol")
            if re.fullmatch(r"composite:[0-9a-f]{64}", snapshot_id) is None:
                raise ValueError("Strategy v2 producer map requires Composite content ids")
        if self.candidate_provider_mode == "disabled":
            return self
        if self.candidate_provider_endpoint is None or self.candidate_provider_model is None:
            raise ValueError("openai_compatible candidate provider requires endpoint and model")
        endpoint = self.candidate_provider_endpoint
        if endpoint.username is not None or endpoint.password is not None:
            raise ValueError("candidate provider endpoint cannot contain credentials")
        if endpoint.query is not None or endpoint.fragment is not None:
            raise ValueError("candidate provider endpoint cannot contain query or fragment")
        if self.is_production and endpoint.scheme != "https":
            raise ValueError("production candidate provider endpoint must use https")
        return self

    def _validate_cors_origins(self) -> None:
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("CORS_ALLOWED_ORIGINS entries must be absolute http(s) origins")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("CORS_ALLOWED_ORIGINS entries cannot contain credentials")
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError("CORS_ALLOWED_ORIGINS entries cannot contain paths or queries")
            if self.is_production and parsed.scheme != "https":
                raise ValueError("production CORS_ALLOWED_ORIGINS entries must use https")

    @property
    def cors_origins(self) -> tuple[str, ...]:
        """Return the explicit browser origins allowed to call this API."""

        return tuple(
            dict.fromkeys(
                item.strip().rstrip("/")
                for item in self.cors_allowed_origins.split(",")
                if item.strip()
            )
        )

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def event_backtest_enabled(self) -> bool:
        """Use the legacy readiness switch as the runtime event feature gate."""

        return self.event_data_required

    @property
    def code_revision_matches_workspace(self) -> bool:
        """Reject an injected pseudo revision when local Git evidence exists."""

        if not _repository_has_git_metadata():
            # Production images intentionally omit .git and receive the exact
            # revision as an immutable Docker build argument.
            return True
        return self.code_revision == _working_tree_revision()
