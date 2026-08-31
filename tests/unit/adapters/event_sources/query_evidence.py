from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date

from ashare_lab.adapters.event_sources.eastmoney import (
    ANNOUNCEMENT_CLASSIFIER_VERSION,
    eastmoney_event_coverage_contract,
)
from ashare_lab.domain.shared import InstrumentId


def eastmoney_query_evidence(
    *,
    instrument_id: InstrumentId,
    start: date,
    end: date,
    event_codes: tuple[str, ...],
    total_hits: int,
    event_counts: Mapping[str, int] | None = None,
) -> dict[str, object]:
    if event_counts is None:
        counts = {event_codes[0]: total_hits} if len(event_codes) == 1 and total_hits else {}
    else:
        counts = dict(event_counts)
    if any(code not in event_codes or count <= 0 for code, count in counts.items()):
        raise ValueError("fixture event_counts must be positive and requested")
    if sum(counts.values()) > total_hits:
        raise ValueError("fixture event_counts cannot exceed total_hits")
    request_parameters: dict[str, object] = {
        "sr": "-1",
        "pageSize": 100,
        "annType": "A",
        "clientSource": "web",
        "fNode": "0",
        "sNode": "0",
        "stockList": instrument_id.value.split(".", maxsplit=1)[0],
        "beginTime": start.isoformat(),
        "endTime": end.isoformat(),
    }
    pagination: dict[str, object] = {
        "pageSize": 100,
        "pageCount": 1,
        "totalHits": total_hits,
        "uniqueArtCodes": total_hits,
        "returnedRows": total_hits,
        "complete": True,
        "zeroResult": total_hits == 0,
        "pages": [
            {
                "pageIndex": 1,
                "rowCount": total_hits,
                "responseSha256": "1" * 64,
            }
        ],
    }
    pagination["evidenceSha256"] = _sha256(pagination)
    classification_summary: dict[str, object] = {
        "classifierVersion": ANNOUNCEMENT_CLASSIFIER_VERSION,
        "totalRows": total_hits,
        "classifiedRows": sum(counts.values()),
        "unclassifiedRows": total_hits - sum(counts.values()),
        "rowsByEventCode": dict(sorted(counts.items())),
        "classifiedRowsSha256": hashlib.sha256(
            json.dumps(counts, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    classification_summary["evidenceSha256"] = _sha256(classification_summary)
    by_code: dict[str, object] = {}
    for code in sorted(event_codes):
        contract = eastmoney_event_coverage_contract(code)
        by_code[code] = {
            "querySucceeded": contract.query_succeeded,
            "coverageBasis": contract.coverage_basis,
            "providerColumnCodes": list(contract.provider_column_codes),
            "titleRuleIds": list(contract.title_rule_ids),
            "classifierVersion": ANNOUNCEMENT_CLASSIFIER_VERSION,
            "classifierSha256": contract.classifier_sha256,
            "selectedRecordCount": counts.get(code, 0),
        }
    evidence: dict[str, object] = {
        "schemaVersion": "ashare-lab.eastmoney-announcement-query-evidence.v2",
        "provider": "eastmoney",
        "instrumentId": instrument_id.value,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "endpoint": "https://np-anotice-stock.eastmoney.com/api/security/ann",
        "requestParameters": request_parameters,
        "requestSha256": _sha256(request_parameters),
        "querySucceeded": True,
        "pagination": pagination,
        "classificationSummary": classification_summary,
        "eventCodeCoverage": by_code,
    }
    evidence["evidenceSha256"] = _sha256(evidence)
    return evidence


def _sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
