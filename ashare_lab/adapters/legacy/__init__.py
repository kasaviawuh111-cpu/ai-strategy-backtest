"""Read-only adapters around the retired ``astock_backtest`` implementation.

Nothing in this package is wired into the production signal, execution, or
accounting paths.  The adapters exist solely to characterize and differentially
test mature legacy calculations while the canonical engine remains authoritative.
"""

from .signal_oracle import LegacyAstockSignalOracle, LegacyDifferentialResult

__all__ = ["LegacyAstockSignalOracle", "LegacyDifferentialResult"]
