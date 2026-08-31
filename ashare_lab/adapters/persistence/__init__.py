"""Persistence adapters for durable backtest work items."""

from sqlalchemy import MetaData

from .backtest_runs import (
    BACKTEST_RUN_METADATA,
    BacktestRunConflictError,
    BacktestRunNotFoundError,
    BacktestRunPersistenceError,
    IllegalBacktestRunTransitionError,
    InMemoryBacktestRunStore,
    SQLAlchemyBacktestRunStore,
    create_backtest_run_engine,
    create_backtest_run_schema,
    create_schema,
)
from .strategy_v2_artifacts import (
    STRATEGY_V2_ARTIFACT_METADATA,
    ImmutableArtifactConflictError,
    MissingArtifactDependencyError,
    SQLAlchemyStrategyV2ArtifactStore,
    StrategyV2ArtifactPersistenceError,
    copy_strategy_v2_artifact_metadata,
    create_strategy_v2_artifact_schema,
)

PERSISTENCE_METADATA = MetaData()
for _table in BACKTEST_RUN_METADATA.sorted_tables:
    _table.to_metadata(PERSISTENCE_METADATA)
copy_strategy_v2_artifact_metadata(PERSISTENCE_METADATA)

__all__ = [
    "BACKTEST_RUN_METADATA",
    "PERSISTENCE_METADATA",
    "STRATEGY_V2_ARTIFACT_METADATA",
    "BacktestRunConflictError",
    "BacktestRunNotFoundError",
    "BacktestRunPersistenceError",
    "IllegalBacktestRunTransitionError",
    "ImmutableArtifactConflictError",
    "InMemoryBacktestRunStore",
    "MissingArtifactDependencyError",
    "SQLAlchemyBacktestRunStore",
    "SQLAlchemyStrategyV2ArtifactStore",
    "StrategyV2ArtifactPersistenceError",
    "create_backtest_run_engine",
    "create_backtest_run_schema",
    "create_schema",
    "create_strategy_v2_artifact_schema",
]
