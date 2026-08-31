"""Research-only, snapshot-first A-share fund-flow acquisition."""

from .eastmoney import (
    DAILY_FIELD_CODES,
    EASTMONEY_FUND_FLOW_URL,
    EastmoneyFundFlowResearchSource,
    FundFlowCollection,
    FundFlowDailyRow,
    FundFlowSourceError,
)
from .snapshot import (
    FUND_FLOW_SNAPSHOT_SCHEMA_VERSION,
    FundFlowSnapshotError,
    FundFlowSnapshotResult,
    build_fund_flow_research_snapshot,
    load_fund_flow_research_snapshot,
)

__all__ = [
    "DAILY_FIELD_CODES",
    "EASTMONEY_FUND_FLOW_URL",
    "FUND_FLOW_SNAPSHOT_SCHEMA_VERSION",
    "EastmoneyFundFlowResearchSource",
    "FundFlowCollection",
    "FundFlowDailyRow",
    "FundFlowSnapshotError",
    "FundFlowSnapshotResult",
    "FundFlowSourceError",
    "build_fund_flow_research_snapshot",
    "load_fund_flow_research_snapshot",
]
