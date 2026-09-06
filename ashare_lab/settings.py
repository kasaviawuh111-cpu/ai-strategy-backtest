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
        "eastmoney_skill",
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
    # Session/ST/preclose evidence is selected independently from the OHLCV
    # producer.  The default preserves existing deployments; local MX-backed
    # runs can opt into the provider-neutral v3 artifact explicitly.
    on_demand_reference_source: Literal["baostock", "eastmoney_mx"] = "baostock"

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

    # 联网检索通道。和上面的候选生成通道是两条独立的路: 候选生成走
    # /chat/completions 的受限 JSON 契约, 这里走 Responses API 的
    # server-side web_search。三个字段都留空时检索整体关闭, 行为与接入前一致。
    # 默认保留既有 DeepSeek 配置语义。火山搜索只在服务端显式切换后启用。
    research_provider_mode: Literal[
        "disabled",
        "deepseek_responses",
        "volcengine_web_search",
    ] = "deepseek_responses"
    research_provider_endpoint: AnyHttpUrl | None = None
    research_provider_api_key: SecretStr | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    research_provider_model: str | None = Field(default=None, min_length=1, max_length=128)
    # Server-side web search performs one or more remote retrieval rounds and
    # needs a larger budget than the plain chat-completions translator.
    # Upper bound must match DeepSeekWebResearcher's own check (300s): if this
    # is stricter than the adapter, the env value is rejected at startup and
    # bootstrap silently disables search.
    research_provider_timeout_seconds: float = Field(default=30.0, ge=0.25, le=300.0)
    candidate_provider_response_mode: Literal["json_schema", "json_object"] = "json_schema"
    candidate_provider_thinking: Literal["disabled", "enabled"] = "disabled"
    candidate_provider_reasoning_effort: Literal["low", "high", "max"] | None = None
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

    # The existing candidate provider is the low-latency ``extract_fast``
    # profile.  ``plan_deep`` is a separately configurable transport for idea
    # formation and strategy advice.  Inheritance preserves the pre-profile
    # single-provider behaviour until an operator opts into the split.
    plan_deep_provider_mode: Literal[
        "inherit_extract_fast",
        "disabled",
        "openai_compatible",
    ] = "inherit_extract_fast"
    plan_deep_provider_name: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    plan_deep_provider_endpoint: AnyHttpUrl | None = None
    plan_deep_provider_api_key: SecretStr | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    plan_deep_provider_model: str | None = Field(default=None, min_length=1, max_length=128)
    # DeepSeek V4 Pro high-thinking requests can legitimately take much longer
    # than extraction.  extract_fast remains capped at 30 seconds above.
    plan_deep_provider_timeout_seconds: float = Field(default=120.0, ge=0.25, le=300.0)
    plan_deep_provider_response_mode: Literal["json_schema", "json_object"] = "json_object"
    plan_deep_provider_thinking: Literal["disabled", "enabled"] = "disabled"
    plan_deep_provider_reasoning_effort: Literal["low", "high", "max"] | None = None

    # MX discovery and Skill-backed historical data share the provider client.
    # Use the official finance Skill's per-request budget for large histories.
    mx_saas_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    mx_saas_timeout_seconds: float = Field(default=120.0, ge=1.0, le=120.0)
    provider_indicator_cache_root: Path = Path("var/cache/provider-indicators")
    skill_history_cache_root: Path = Path("var/cache/mx-daily-history")
    provider_indicator_cache_ttl_seconds: int = Field(
        default=86_400,
        ge=60,
        le=7 * 86_400,
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

        research_values = (
            self.research_provider_endpoint,
            self.research_provider_model,
            self.research_provider_api_key,
        )
        if self.research_provider_mode == "deepseek_responses":
            if any(value is not None for value in research_values) and not all(
                value is not None for value in research_values
            ):
                raise ValueError("research provider requires endpoint, model and API key together")
        elif self.research_provider_mode == "volcengine_web_search":
            if self.research_provider_api_key is None:
                raise ValueError("Volcengine web search requires an API key")
            endpoint = self.research_provider_endpoint
            if endpoint is not None:
                if endpoint.username is not None or endpoint.password is not None:
                    raise ValueError("research provider endpoint cannot contain credentials")
                if endpoint.query is not None or endpoint.fragment is not None:
                    raise ValueError("research provider endpoint cannot contain query or fragment")
                if (endpoint.path or "").rstrip("/") != "/search_api/web_search":
                    raise ValueError("Volcengine endpoint must target the Web Search API")
                if self.is_production and endpoint.scheme != "https":
                    raise ValueError("production research provider endpoint must use https")
        if (
            self.research_provider_api_key is not None
            and not self.research_provider_api_key.get_secret_value().strip()
        ):
            raise ValueError("research provider API key cannot be empty")

        self._validate_thinking_configuration(
            profile="extract_fast",
            provider_mode=self.candidate_provider_mode,
            provider_name=self.candidate_provider_name,
            thinking=self.candidate_provider_thinking,
            reasoning_effort=self.candidate_provider_reasoning_effort,
        )
        if self.candidate_provider_mode == "openai_compatible":
            if self.candidate_provider_endpoint is None or self.candidate_provider_model is None:
                raise ValueError("openai_compatible candidate provider requires endpoint and model")
            self._validate_language_provider_endpoint(
                self.candidate_provider_endpoint,
                profile="candidate",
            )
            if (
                self.candidate_provider_api_key is None
                or not self.candidate_provider_api_key.get_secret_value().strip()
            ):
                raise ValueError("openai_compatible candidate provider requires an API key")

        plan_connection_values = (
            self.plan_deep_provider_name,
            self.plan_deep_provider_endpoint,
            self.plan_deep_provider_model,
            self.plan_deep_provider_api_key,
        )
        self._validate_thinking_configuration(
            profile="plan_deep",
            provider_mode=self.plan_deep_provider_mode,
            provider_name=self.plan_deep_provider_name,
            thinking=self.plan_deep_provider_thinking,
            reasoning_effort=self.plan_deep_provider_reasoning_effort,
        )
        if self.plan_deep_provider_mode == "openai_compatible":
            if any(value is None for value in plan_connection_values):
                raise ValueError(
                    "openai_compatible plan_deep provider requires name, endpoint, "
                    "model and API key"
                )
            assert self.plan_deep_provider_endpoint is not None
            assert self.plan_deep_provider_api_key is not None
            self._validate_language_provider_endpoint(
                self.plan_deep_provider_endpoint,
                profile="plan_deep",
            )
            if not self.plan_deep_provider_api_key.get_secret_value().strip():
                raise ValueError(
                    "openai_compatible plan_deep provider requires a non-empty API key"
                )
        elif any(value is not None for value in plan_connection_values):
            raise ValueError(
                "PLAN_DEEP_PROVIDER_MODE must be openai_compatible when provider fields are set"
            )
        return self

    def _validate_language_provider_endpoint(
        self,
        endpoint: AnyHttpUrl,
        *,
        profile: str,
    ) -> None:
        if endpoint.username is not None or endpoint.password is not None:
            raise ValueError(f"{profile} provider endpoint cannot contain credentials")
        if endpoint.query is not None or endpoint.fragment is not None:
            raise ValueError(f"{profile} provider endpoint cannot contain query or fragment")
        if self.is_production and endpoint.scheme != "https":
            raise ValueError(f"production {profile} provider endpoint must use https")

    @staticmethod
    def _validate_thinking_configuration(
        *,
        profile: str,
        provider_mode: str,
        provider_name: str | None,
        thinking: Literal["disabled", "enabled"],
        reasoning_effort: Literal["low", "high", "max"] | None,
    ) -> None:
        if thinking == "disabled":
            if reasoning_effort is not None:
                raise ValueError(f"{profile} reasoning effort requires enabled thinking")
            return
        if provider_mode != "openai_compatible":
            raise ValueError(f"{profile} thinking requires an openai_compatible provider")
        if provider_name is None or not provider_name.casefold().startswith("deepseek"):
            raise ValueError(f"{profile} thinking is only supported for DeepSeek")
        if reasoning_effort is None:
            raise ValueError(f"{profile} enabled thinking requires a reasoning effort")

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

    def resolved_mx_saas_api_key(self) -> str | None:
        """Return a server-owned credential without logging or serialising it.

        Local development may use the credential file written by the installed
        妙想 skills.  Production must provide ``MX_SAAS_API_KEY`` explicitly;
        an arbitrary local file cannot silently become a deployed credential.
        """

        if self.mx_saas_api_key is not None:
            value = self.mx_saas_api_key.get_secret_value().strip()
            return value or None
        if self.is_production:
            return None
        credential_path = Path.home() / ".mx-skills" / "em_api_key"
        try:
            mode = credential_path.stat().st_mode
            if mode & 0o077:
                return None
            value = credential_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

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
