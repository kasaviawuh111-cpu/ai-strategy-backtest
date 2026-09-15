"""Optional, user-owned run settings, separate from fixed strategy timing rules."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ashare_lab.domain.execution import CapacityMode, LimitHandling


class ExecutionSettingsPatch(BaseModel):
    """Missing/null preserves a setting; zero is an explicit value, never a default."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    slippage_bps: Decimal | None = Field(default=None, ge=0, le=1_000)
    slippage_cny: Decimal | None = Field(default=None, ge=0)
    commission_rate: Decimal | None = Field(default=None, ge=0)
    minimum_commission_cny: Decimal | None = Field(default=None, ge=0)
    participation_rate: Decimal | None = Field(default=None, gt=0, le=1)
    allocation_ratio: Decimal | None = Field(default=None, gt=0, le=1)
    limit_handling: LimitHandling | None = None
    capacity_mode: CapacityMode | None = None
    retry_unfilled_exits: bool | None = Field(default=None, strict=True)
    max_exit_attempts: int | None = Field(default=None, ge=1, le=1_000, strict=True)
    warmup_calendar_days: int | None = Field(default=None, ge=0, le=3_650, strict=True)
    settlement_extension_days: int | None = Field(default=None, ge=1, le=365, strict=True)
    run_robustness: bool | None = Field(default=None, strict=True)

    def merged(self, patch: "ExecutionSettingsPatch") -> "ExecutionSettingsPatch":
        return type(self).model_validate({
            **self.model_dump(exclude_none=True),
            **patch.model_dump(exclude_none=True),
        })

    def validate_evidence(self, evidence: dict[str, str], utterance: str) -> None:
        """Models interpret units; the host verifies exact source and field coverage."""
        fields = set(self.model_dump(exclude_none=True))
        if fields != set(evidence):
            raise ValueError("execution setting evidence must match changed fields")
        if any(not value.strip() or value not in utterance for value in evidence.values()):
            raise ValueError("execution setting evidence must quote the current input")
