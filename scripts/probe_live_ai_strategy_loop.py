#!/usr/bin/env python3
"""Prove the live model -> real data -> backtest -> model review loop.

This is an acceptance probe, not a fixture test.  It rejects disabled/local
model provenance, requires fresh baseline and optimization runs, and can read
the local SQLite store in read-only mode to prove provider indicator evidence
was frozen into both work items.  It never reads or records credentials.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
_NON_LIVE_PROVIDERS = frozenset({"", "disabled", "local", "rule", "rule_based", "fixture"})


def _request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = raw.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"local API returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"local API request failed: {type(exc).__name__}") from exc
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("local API returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("local API returned a non-object JSON root")
    return cast(dict[str, Any], decoded)


def _wait_for_run(
    base_url: str,
    run_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = _request_json(
            "GET",
            f"{base_url}/api/v1/backtest-runs/{run_id}",
            timeout_seconds=min(30.0, timeout_seconds),
        )
        if status.get("state") in _TERMINAL_STATES:
            return status
        if time.monotonic() >= deadline:
            raise RuntimeError(f"backtest did not finish within {timeout_seconds:g} seconds")
        time.sleep(poll_interval_seconds)


def _require_live_model(provenance: object, *, stage: str) -> dict[str, Any]:
    if not isinstance(provenance, dict):
        raise RuntimeError(f"{stage} did not expose model provenance")
    provider = str(provenance.get("provider", "")).strip().casefold()
    model = str(provenance.get("model", "")).strip()
    if provider in _NON_LIVE_PROVIDERS or not model or model == "unconfigured":
        raise RuntimeError(f"{stage} used a disabled or local substitute instead of a live model")
    return cast(dict[str, Any], provenance)


def _provider_evidence(database_path: Path, run_id: str) -> dict[str, Any]:
    resolved = database_path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"SQLite database does not exist: {resolved}")
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT result_json, state FROM backtest_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError(f"run is missing from SQLite database: {run_id}")
    if row[1] != "succeeded":
        raise RuntimeError("persisted backtest is not successful")
    result = json.loads(str(row[0]))
    evidence = result.get("summary", {}).get("dataProvenance")
    if not isinstance(evidence, dict):
        raise RuntimeError("Skill data provenance is missing from the persisted result")
    if evidence.get("provider") != "eastmoney_mx_finance_data":
        raise RuntimeError("backtest did not use the Eastmoney finance Skill")
    if evidence.get("historyRows", 0) <= 0:
        raise RuntimeError("backtest has no actual history")
    derived = evidence.get("derivedIndicatorEvidence", [])
    verified_derived = isinstance(derived, list) and bool(derived) and all(
        isinstance(item, dict)
        and item.get("source") == "local_formula_on_eastmoney_skill_ohlcv"
        and item.get("indicatorId") in {"technical.ma", "price.rolling_high", "volume.relative"}
        and item.get("definitionVersion") == "1.0.0"
        and item.get("parameters")
        and item.get("formula")
        for item in derived
    )
    if evidence.get("indicatorPoints", 0) <= 0 and not verified_derived:
        raise RuntimeError("backtest has no provider indicator points or verified derived metadata")
    return {"persisted": True, **evidence}


def _submit_and_wait(
    base_url: str,
    strategy: dict[str, Any],
    *,
    slippage_bps: float,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any], float]:
    started = time.monotonic()
    created = _request_json(
        "POST",
        f"{base_url}/api/v1/backtest-runs",
        payload={
            "strategy": strategy,
            "config": {"slippageBps": slippage_bps},
        },
        timeout_seconds=min(timeout_seconds, 300.0),
    )
    if created.get("replayed") is not False:
        raise RuntimeError("acceptance requires a fresh backtest run, not an idempotent replay")
    run_id = created.get("id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("backtest submission returned no run id")
    final = _wait_for_run(
        base_url,
        run_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if final.get("state") != "succeeded":
        raise RuntimeError(f"backtest ended in {final.get('state')}: {final.get('error')}")
    summary = _request_json(
        "GET",
        f"{base_url}/api/v1/backtest-runs/{run_id}/summary",
    )
    return final, summary, round(time.monotonic() - started, 3)


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    parent_existed = resolved.parent.exists()
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        resolved.parent.chmod(0o700)
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    resolved.chmod(0o600)


def _read_json_object(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        decoded = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"initial draft is not readable JSON: {resolved}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("initial draft JSON root must be an object")
    return cast(dict[str, Any], decoded)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--utterance", required=True)
    parser.add_argument(
        "--initial-draft",
        type=Path,
        help="reuse a previously captured real /strategy-drafts response",
    )
    parser.add_argument("--instrument-context")
    parser.add_argument("--as-of-date", type=date.fromisoformat, required=True)
    parser.add_argument("--local-api-base", default="http://127.0.0.1:8019")
    parser.add_argument("--proposal-index", type=int, default=0)
    parser.add_argument("--optimization-index", type=int, default=0)
    parser.add_argument("--baseline-slippage-bps", type=float, default=5.0)
    parser.add_argument("--optimized-slippage-bps", type=float, default=5.0)
    parser.add_argument("--require-web", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=1_200.0)
    parser.add_argument("--model-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    parser.add_argument("--sqlite-db", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 <= args.proposal_index <= 2 or not 0 <= args.optimization_index <= 2:
        parser.error("proposal indexes must be between zero and two")
    if args.timeout_seconds <= 0 or args.model_timeout_seconds <= 0:
        parser.error("timeouts must be positive")
    if args.poll_interval_seconds <= 0:
        parser.error("poll interval must be positive")
    for value in (args.baseline_slippage_bps, args.optimized_slippage_bps):
        if not 0 <= value <= 1_000:
            parser.error("slippage must be between zero and 1000 bps")
    if args.baseline_slippage_bps != args.optimized_slippage_bps:
        parser.error("use the same slippage to compare a model strategy revision fairly")
    return args


def main() -> int:
    args = _parse_args()
    base_url = args.local_api_base.rstrip("/")
    started = time.monotonic()
    initial_source = "captured_live_response" if args.initial_draft is not None else "live_api"
    initial = (
        _read_json_object(args.initial_draft)
        if args.initial_draft is not None
        else _request_json(
            "POST",
            f"{base_url}/api/v1/strategy-drafts",
            payload={
                "utterance": args.utterance,
                "instrument_context": args.instrument_context,
                "as_of_date": args.as_of_date.isoformat(),
            },
            timeout_seconds=args.model_timeout_seconds,
        )
    )
    generation_path = "direct_ready"
    research: object = None
    proposals: list[object] = []
    selected: dict[str, Any] | None = None
    if initial.get("status") == "ready":
        if args.require_web:
            raise RuntimeError("direct strategy compilation has no web research evidence")
        generation_provenance = _require_live_model(
            initial.get("candidate_provenance"),
            stage="initial strategy compilation",
        )
        baseline_strategy = initial.get("strategy")
        if not isinstance(baseline_strategy, dict):
            raise RuntimeError("ready initial draft has no executable strategy")
    else:
        generation_path = "idea_route"
        route = initial.get("idea_route")
        if not isinstance(route, dict):
            raise RuntimeError(
                "initial model route failed: "
                + json.dumps(
                    {
                        key: initial.get(key)
                        for key in (
                            "status",
                            "diagnostic_code",
                            "clarification",
                            "candidate_provenance",
                        )
                    },
                    ensure_ascii=False,
                )
            )
        research = route.get("research")
        if args.require_web and (
            not isinstance(research, dict) or not research.get("sources")
        ):
            raise RuntimeError("live web research returned no source evidence")
        generation_provenance = _require_live_model(
            route.get("provenance"),
            stage="strategy generation",
        )
        raw_proposals = route.get("proposals")
        if not isinstance(raw_proposals, list) or len(raw_proposals) < 2:
            raise RuntimeError("live strategy model returned fewer than two proposals")
        proposals = raw_proposals
        try:
            raw_selected = proposals[args.proposal_index]
        except IndexError as exc:
            raise RuntimeError("selected strategy proposal does not exist") from exc
        if not isinstance(raw_selected, dict) or not isinstance(raw_selected.get("id"), str):
            raise RuntimeError("selected strategy proposal is invalid")
        selected = cast(dict[str, Any], raw_selected)
        draft_id = initial.get("draft_id")
        revision = initial.get("revision")
        if not isinstance(draft_id, str) or not isinstance(revision, int):
            raise RuntimeError("initial strategy draft identity is missing")
        accepted = _request_json(
            "POST",
            (
                f"{base_url}/api/v1/strategy-drafts/{draft_id}/revisions/"
                f"{revision}/clarification-answers"
            ),
            payload={"answer": selected["id"]},
            timeout_seconds=args.model_timeout_seconds,
        )
        accepted_draft = accepted.get("draft")
        if not isinstance(accepted_draft, dict) or accepted_draft.get("status") != "ready":
            raise RuntimeError("selected live-model proposal did not compile to a ready strategy")
        baseline_strategy = accepted_draft.get("strategy")
        if not isinstance(baseline_strategy, dict):
            raise RuntimeError("accepted draft has no executable strategy")

    baseline_final, baseline_summary, baseline_seconds = _submit_and_wait(
        base_url,
        cast(dict[str, Any], baseline_strategy),
        slippage_bps=args.baseline_slippage_bps,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    baseline_id = cast(str, baseline_final["id"])
    review = _request_json(
        "POST",
        f"{base_url}/api/v1/backtest-runs/{baseline_id}/review",
        timeout_seconds=args.model_timeout_seconds,
    )
    review_provenance = _require_live_model(
        review.get("modelProvenance"),
        stage="backtest review",
    )
    optimization_candidates = review.get("optimizationCandidates")
    if not isinstance(optimization_candidates, list) or len(optimization_candidates) < 2:
        raise RuntimeError("live review model returned fewer than two compiler-valid revisions")
    try:
        optimization = optimization_candidates[args.optimization_index]
    except IndexError as exc:
        raise RuntimeError("selected optimization proposal does not exist") from exc
    if not isinstance(optimization, dict) or not isinstance(optimization.get("strategy"), dict):
        raise RuntimeError("selected optimization proposal has no compiled strategy")
    if optimization["strategy"] == baseline_strategy:
        raise RuntimeError("model optimization did not change the strategy")
    for field in ("instrument", "backtest"):
        if optimization["strategy"].get(field) != baseline_strategy.get(field):
            raise RuntimeError("optimization changed instrument or test period/capital")

    optimized_final, optimized_summary, optimized_seconds = _submit_and_wait(
        base_url,
        cast(dict[str, Any], optimization["strategy"]),
        slippage_bps=args.optimized_slippage_bps,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    optimized_id = cast(str, optimized_final["id"])
    evidence = None
    if args.sqlite_db is not None:
        evidence = {
            "baseline": _provider_evidence(args.sqlite_db, baseline_id),
            "optimized": _provider_evidence(args.sqlite_db, optimized_id),
        }
    result: dict[str, Any] = {
        "generatedAt": datetime.now(UTC).isoformat(),
        "status": "succeeded",
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "naturalLanguageInput": args.utterance,
        "initialDraftSource": initial_source,
        "webResearch": research,
        "strategyGeneration": {
            "path": generation_path,
            "provider": generation_provenance.get("provider"),
            "model": generation_provenance.get("model"),
            "promptVersion": generation_provenance.get("prompt_version"),
            "schemaVersion": generation_provenance.get("schema_version"),
            "proposalCount": len(proposals),
            "selectedId": None if selected is None else selected["id"],
            "selectedTitle": None if selected is None else selected.get("title"),
            "selectedUtterance": (
                None if selected is None else selected.get("suggested_utterance")
            ),
        },
        "baseline": {
            "runId": baseline_id,
            "replayed": baseline_final.get("replayed", False),
            "elapsedSeconds": baseline_seconds,
            "summary": baseline_summary,
        },
        "modelReview": {
            "provider": review_provenance.get("provider"),
            "model": review_provenance.get("model"),
            "promptVersion": review_provenance.get("promptVersion"),
            "schemaVersion": review_provenance.get("schemaVersion"),
            "responseHash": review_provenance.get("responseHash"),
            "sourceResultHash": review.get("sourceResultHash"),
            "evidenceGrade": review.get("evidenceGrade"),
            "evidenceReasons": review.get("evidenceReasons"),
            "analysis": review.get("analysis"),
            "conclusion": review.get("conclusion"),
            "candidateCount": len(optimization_candidates),
        },
        "optimization": {
            "selectedId": optimization.get("id"),
            "selectedTitle": optimization.get("title"),
            "changeDimension": optimization.get("changeDimension"),
            "suggestedUtterance": optimization.get("suggestedUtterance"),
            "runId": optimized_id,
            "replayed": optimized_final.get("replayed", False),
            "elapsedSeconds": optimized_seconds,
            "summary": optimized_summary,
        },
        "providerIndicatorEvidence": evidence,
    }
    if args.output is not None:
        _write_private(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
