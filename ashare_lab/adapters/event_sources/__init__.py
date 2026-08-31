"""Optional announcement-source adapters normalized to EventObservation."""

from .collector import (
    EventCollectionRequest,
    EventCollectionResult,
    EventFetchBatch,
    EventSourceCollection,
    build_event_acquisition_coverage,
    collect_event_observations,
)
from .eastmoney import (
    EastmoneyAnnouncementError,
    EastmoneyAnnouncementSource,
    EastmoneyEventCoverageContract,
    eastmoney_event_coverage_contract,
    eastmoney_preparable_event_codes,
)
from .ifind import IFindEventRowError, IFindEventSourceAdapter, normalize_ifind_row
from .rqdata import RQDataEventRowError, RQDataEventSourceAdapter, normalize_rqdata_row
from .tushare import TushareEventRowError, TushareEventSourceAdapter, normalize_tushare_row
from .web_evidence import (
    LICENSE_APPROVAL,
    MAJOR_CONTRACT_WON,
    SUPPORTED_EVIDENCE_PROVIDERS,
    SUPPORTED_WEB_EVENT_CODES,
    TIMING_LICENSED_VENDOR,
    TIMING_PAGE_METADATA,
    TIMING_PROSPECTIVE_ARCHIVE,
    TIMING_SEARCH_INDEX,
    DiscoveryOnlyEvidenceError,
    SearchHit,
    WebEvidenceError,
    normalize_web_evidence_row,
)

__all__ = [
    "LICENSE_APPROVAL",
    "MAJOR_CONTRACT_WON",
    "SUPPORTED_EVIDENCE_PROVIDERS",
    "SUPPORTED_WEB_EVENT_CODES",
    "TIMING_LICENSED_VENDOR",
    "TIMING_PAGE_METADATA",
    "TIMING_PROSPECTIVE_ARCHIVE",
    "TIMING_SEARCH_INDEX",
    "DiscoveryOnlyEvidenceError",
    "EastmoneyAnnouncementError",
    "EastmoneyAnnouncementSource",
    "EastmoneyEventCoverageContract",
    "EventCollectionRequest",
    "EventCollectionResult",
    "EventFetchBatch",
    "EventSourceCollection",
    "IFindEventRowError",
    "IFindEventSourceAdapter",
    "RQDataEventRowError",
    "RQDataEventSourceAdapter",
    "SearchHit",
    "TushareEventRowError",
    "TushareEventSourceAdapter",
    "WebEvidenceError",
    "build_event_acquisition_coverage",
    "collect_event_observations",
    "eastmoney_event_coverage_contract",
    "eastmoney_preparable_event_codes",
    "normalize_ifind_row",
    "normalize_rqdata_row",
    "normalize_tushare_row",
    "normalize_web_evidence_row",
]
