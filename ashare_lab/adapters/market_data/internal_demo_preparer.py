"""Submission-time orchestration for one immutable internal-Demo snapshot."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol, cast

from ashare_lab.adapters.event_sources.document_text import (
    DOCUMENT_TEXT_INCOMPLETE_EXIT_CODE,
)
from ashare_lab.adapters.event_sources.eastmoney import eastmoney_preparable_event_codes
from ashare_lab.domain.shared import InstrumentId
from ashare_lab.ports.market_data import DataRequirements, DateRange

from .choice_snapshot import CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE
from .local_parquet import normalize_instrument_id
from .on_demand_snapshot import (
    SnapshotPreparationDocumentTextIncompleteError,
    SnapshotPreparationFailedError,
    SnapshotPreparationResult,
    SnapshotPreparationUnsupportedError,
)

STRICT_EVENT_TIMESTAMP_FLOOR = date(2017, 1, 1)
_ALLOWED_DATASETS = frozenset({"daily_ohlcv", "corporate_actions", "events"})
# Compatibility exports for callers that display the startup-time inventory.
# Submission-time validation below deliberately recomputes the set from the
# concrete Eastmoney collector/classifier contract.  It must never be
# populated from the broader 69-code Catalog.
STRICT_INTERNAL_DEMO_EVENT_CODES = eastmoney_preparable_event_codes()
CURRENT_ON_DEMAND_EVENT_CODES = STRICT_INTERNAL_DEMO_EVENT_CODES
_PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_CHOICE_PROVIDER_CODE = re.compile(r"(?<![0-9])[0-9]{8}(?![0-9])")


@dataclass(frozen=True, slots=True)
class PreparationCommandResult:
    returncode: int
    stdout: str
    stderr: str


class PreparationCommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, cwd: Path) -> PreparationCommandResult: ...


class SubprocessPreparationCommandRunner:
    """Run acquisition CLIs with bounded output and without local proxy leakage."""

    def __init__(self, *, timeout_seconds: int = 600) -> None:
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        self._timeout_seconds = timeout_seconds

    def run(self, argv: Sequence[str], *, cwd: Path) -> PreparationCommandResult:
        environment = dict(os.environ)
        for name in _PROXY_VARIABLES:
            environment.pop(name, None)
        existing_python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(cwd)
            if not existing_python_path
            else os.pathsep.join((str(cwd), existing_python_path))
        )
        try:
            completed = subprocess.run(
                tuple(argv),
                cwd=cwd,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SnapshotPreparationFailedError(
                f"snapshot acquisition command could not complete: {type(exc).__name__}"
            ) from exc
        return PreparationCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class InternalDemoSnapshotPreparer:
    """Acquire reference, Choice daily, Event v2, and Composite v2 snapshots.

    Network-capable provider CLIs are invoked only while a fresh submission is
    being prepared.  They publish content-addressed producer directories.  The
    worker later reads those directories through the registry with network
    access disabled and an exact expected selection identity.
    """

    def __init__(
        self,
        repository_root: str | Path,
        *,
        python_executable: str | Path = sys.executable,
        choice_output_root: str | Path = "var/snapshots/choice",
        technical_output_root: str | Path = "var/snapshots/technical",
        event_output_root: str | Path = "var/snapshots/events",
        composite_output_root: str | Path = "var/snapshots/composite",
        temporary_root: str | Path = "var/preparations",
        command_runner: PreparationCommandRunner | None = None,
    ) -> None:
        self._repository_root = Path(repository_root).expanduser().resolve()
        executable = Path(python_executable).expanduser()
        if not executable.is_absolute():
            executable = self._repository_root / executable
        # A virtualenv interpreter is commonly a symlink. Resolving it would
        # silently escape the environment and lose installed dependencies.
        self._python_executable = str(executable.absolute())
        self._choice_output_root = _resolve_under(
            self._repository_root,
            choice_output_root,
        )
        self._technical_output_root = _resolve_under(
            self._repository_root,
            technical_output_root,
        )
        self._event_output_root = _resolve_under(
            self._repository_root,
            event_output_root,
        )
        self._composite_output_root = _resolve_under(
            self._repository_root,
            composite_output_root,
        )
        if (
            len(
                {
                    self._choice_output_root,
                    self._technical_output_root,
                    self._event_output_root,
                    self._composite_output_root,
                }
            )
            != 4
        ):
            raise ValueError(
                "Choice, technical, event, and composite snapshot roots must be distinct"
            )
        self._temporary_root = _resolve_under(self._repository_root, temporary_root)
        self._runner = command_runner or SubprocessPreparationCommandRunner()

    def prepare(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> SnapshotPreparationResult:
        instrument = _one_daily_instrument(requirements)
        needs_events = "events" in requirements.datasets
        if needs_events and period.start < STRICT_EVENT_TIMESTAMP_FLOOR:
            raise SnapshotPreparationUnsupportedError(
                "strict event replay cannot start before 2017-01-01 because the "
                "available Eastmoney historical first-availability clock is not credible"
            )
        # Recompute this capability at the submission boundary.  This makes a
        # changed/removed classifier acceptance contract fail closed without
        # requiring a process restart or trusting a stale Catalog count.
        supported_event_codes = eastmoney_preparable_event_codes()
        unsupported_event_codes = sorted(set(requirements.event_codes) - supported_event_codes)
        if unsupported_event_codes:
            raise SnapshotPreparationUnsupportedError(
                "internal Demo strict sources do not support event codes: "
                + ", ".join(unsupported_event_codes)
            )

        self._temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="snapshot-preparation-",
            dir=self._temporary_root,
        ) as raw_temporary:
            temporary = Path(raw_temporary)
            reference_path = temporary / "baostock-reference.json"
            self._run(
                (
                    self._python_executable,
                    "scripts/prepare_baostock_reference.py",
                    "--symbol",
                    str(instrument),
                    "--start",
                    period.start.isoformat(),
                    "--end",
                    period.end.isoformat(),
                    "--output",
                    str(reference_path),
                ),
                label="BaoStock reference acquisition",
            )
            reference = _json_object(reference_path, "BaoStock reference artifact")
            instrument_metadata = _mapping(reference, "instrument")
            if instrument_metadata.get("instrument_id") != str(instrument):
                raise SnapshotPreparationFailedError(
                    "BaoStock reference artifact belongs to a different instrument"
                )
            listing_date = _iso_date(instrument_metadata.get("listing_date"), "listing_date")
            board = _text(instrument_metadata.get("board"), "board")
            asset_type = _text(instrument_metadata.get("asset_type"), "asset_type")
            prefix_end = _prefix_end(period)
            corporate_action_path = temporary / "eastmoney-corporate-actions.json"
            if asset_type == "STOCK":
                self._run(
                    (
                        self._python_executable,
                        "scripts/prepare_eastmoney_corporate_actions.py",
                        "--symbol",
                        str(instrument),
                        "--start",
                        period.start.isoformat(),
                        "--end",
                        period.end.isoformat(),
                        "--output",
                        str(corporate_action_path),
                    ),
                    label="Eastmoney corporate-action acquisition",
                )
            elif asset_type == "ETF":
                if board != "stock_etf":
                    raise SnapshotPreparationFailedError(
                        "BaoStock ETF identity does not use the stock_etf rule board"
                    )
                # The ETF Choice attempt deliberately reuses the normal CLI.
                # A provider-unavailable result is the only condition that may
                # authorize the independently validated public-source fallback.
                corporate_action_path = reference_path
            else:
                raise SnapshotPreparationUnsupportedError(
                    f"security-master asset type is not executable: {asset_type}"
                )

            choice_command = (
                self._python_executable,
                "scripts/prepare_choice_snapshot.py",
                "--symbol",
                str(instrument),
                "--start",
                period.start.isoformat(),
                "--end",
                period.end.isoformat(),
                "--prefix-end",
                prefix_end.isoformat(),
                "--listing-date",
                listing_date.isoformat(),
                "--board",
                board,
                "--output-root",
                str(self._choice_output_root),
                "--session-reference-json",
                str(reference_path),
                "--corporate-actions-json",
                str(corporate_action_path),
            )
            choice_result = self._runner.run(choice_command, cwd=self._repository_root)
            if choice_result.returncode == 0:
                daily_payload = _stdout_json_object(
                    choice_result.stdout,
                    "Choice daily snapshot acquisition",
                )
                _daily_id, daily_path = _published_snapshot(
                    daily_payload,
                    id_key="snapshotId",
                    path_key="path",
                    prefix="choice",
                    output_root=self._choice_output_root,
                )
            elif choice_result.returncode == CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE:
                fallback_reason = _choice_fallback_reason(choice_result)
                fallback_script = (
                    "scripts/prepare_etf_snapshot.py"
                    if asset_type == "ETF"
                    else "scripts/prepare_eastmoney_snapshot.py"
                )
                fallback_command: list[str] = [
                    self._python_executable,
                    fallback_script,
                    "--symbol",
                    str(instrument),
                    "--start",
                    period.start.isoformat(),
                    "--end",
                    period.end.isoformat(),
                    "--prefix-end",
                    prefix_end.isoformat(),
                    "--output-root",
                    str(self._technical_output_root),
                    "--fallback-reason",
                    fallback_reason,
                ]
                if asset_type == "ETF":
                    fallback_command.extend(("--reference-json", str(reference_path)))
                else:
                    fallback_command.extend(
                        (
                            "--listing-date",
                            listing_date.isoformat(),
                            "--board",
                            board,
                            "--session-reference-json",
                            str(reference_path),
                            "--corporate-actions-json",
                            str(corporate_action_path),
                        )
                    )
                daily_payload = self._run_json(
                    tuple(fallback_command),
                    label=(
                        "strict public ETF daily snapshot fallback acquisition"
                        if asset_type == "ETF"
                        else "Eastmoney Push2 daily snapshot fallback acquisition"
                    ),
                )
                _daily_id, daily_path = _published_snapshot(
                    daily_payload,
                    id_key="snapshotId",
                    path_key="path",
                    prefix="technical",
                    output_root=self._technical_output_root,
                )
            else:
                diagnostic = _bounded_diagnostic(choice_result.stdout, choice_result.stderr)
                raise SnapshotPreparationFailedError(
                    "Choice daily snapshot failed integrity validation; Push2 fallback "
                    f"is forbidden: {diagnostic}"
                )
            command: list[str] = [
                self._python_executable,
                "scripts/prepare_event_snapshot.py",
                "--symbol",
                str(instrument),
                "--start",
                period.start.isoformat(),
                "--end",
                period.end.isoformat(),
                "--event-output",
                str(self._event_output_root),
                "--choice-snapshot",
                str(daily_path),
                "--composite-output",
                str(self._composite_output_root),
            ]
            if needs_events:
                for event_code in sorted(requirements.event_codes):
                    command.extend(("--event-code", event_code))
                if requirements.needs_event_document_text:
                    command.append("--extract-document-text")
            else:
                command.append("--no-event-required")
            composite_payload = self._run_json(
                tuple(command),
                label=(
                    "Eastmoney event and composite snapshot acquisition"
                    if needs_events
                    else "technical-only Event v2 and Composite v2 publication"
                ),
                incomplete_exit_codes=(
                    frozenset({DOCUMENT_TEXT_INCOMPLETE_EXIT_CODE})
                    if requirements.needs_event_document_text
                    else frozenset()
                ),
            )
            composite_id, composite_path = _published_snapshot(
                composite_payload,
                id_key="compositeSnapshotId",
                path_key="compositeSnapshotPath",
                prefix="composite",
                output_root=self._composite_output_root,
            )
            return SnapshotPreparationResult(composite_id, composite_path)

    def _run(
        self,
        argv: Sequence[str],
        *,
        label: str,
        incomplete_exit_codes: frozenset[int] = frozenset(),
    ) -> PreparationCommandResult:
        result = self._runner.run(argv, cwd=self._repository_root)
        if result.returncode in incomplete_exit_codes:
            raise SnapshotPreparationDocumentTextIncompleteError(
                f"{label} did not produce complete document-text coverage"
            )
        if result.returncode != 0:
            diagnostic = _bounded_diagnostic(result.stdout, result.stderr)
            raise SnapshotPreparationFailedError(f"{label} failed: {diagnostic}")
        return result

    def _run_json(
        self,
        argv: Sequence[str],
        *,
        label: str,
        incomplete_exit_codes: frozenset[int] = frozenset(),
    ) -> Mapping[str, object]:
        result = self._run(
            argv,
            label=label,
            incomplete_exit_codes=incomplete_exit_codes,
        )
        return _stdout_json_object(result.stdout, label)


def _one_daily_instrument(requirements: DataRequirements) -> InstrumentId:
    unsupported = sorted(set(requirements.datasets) - _ALLOWED_DATASETS)
    if unsupported or requirements.needs_minute or requirements.needs_tick:
        details = ", ".join(unsupported) if unsupported else "intraday/tick"
        raise SnapshotPreparationUnsupportedError(
            "internal Demo preparer supports daily datasets only: " + details
        )
    if requirements.needs_l2_queue:
        raise SnapshotPreparationUnsupportedError(
            "internal Demo preparer does not support L2 queues"
        )
    instruments = tuple(
        sorted(
            {normalize_instrument_id(item) for item in requirements.instruments},
            key=str,
        )
    )
    if len(instruments) != 1:
        raise SnapshotPreparationUnsupportedError(
            "internal Demo preparer requires exactly one A-share instrument"
        )
    instrument = instruments[0]
    if str(instrument).endswith(".BJ"):
        raise SnapshotPreparationUnsupportedError(
            "internal Demo cannot execute any Beijing Stock Exchange (.BJ) share: "
            "the current sources cannot yet prove historical BSE venue admission and "
            "cross-provider daily suspension/reference coverage; order quantity rules "
            "are implemented, but unproven market-data history still fails closed"
        )
    return instrument


def _prefix_end(period: DateRange) -> date:
    if (period.end - period.start).days < 2:
        raise SnapshotPreparationUnsupportedError(
            "Choice prefix-stability validation requires at least a three-day snapshot range"
        )
    return max(period.start + timedelta(days=1), period.end - timedelta(days=365))


def _choice_fallback_reason(result: PreparationCommandResult) -> str:
    """Return a bounded, non-secret reason suitable for snapshot provenance."""

    diagnostic = _bounded_diagnostic(result.stdout, result.stderr)
    match = _CHOICE_PROVIDER_CODE.search(diagnostic)
    provider_code = f"_provider_{match.group(0)}" if match is not None else ""
    return f"choice_daily_exit_{result.returncode}{provider_code}"


def _published_snapshot(
    payload: Mapping[str, object],
    *,
    id_key: str,
    path_key: str,
    prefix: str,
    output_root: Path,
) -> tuple[str, Path]:
    snapshot_id = _text(payload.get(id_key), id_key)
    if not snapshot_id.startswith(f"{prefix}:"):
        raise SnapshotPreparationFailedError(f"{id_key} has the wrong producer prefix")
    path = Path(_text(payload.get(path_key), path_key)).expanduser().resolve()
    if not path.is_relative_to(output_root) or not path.is_dir():
        raise SnapshotPreparationFailedError(f"{path_key} is outside its configured output root")
    if path.name != snapshot_id.split(":", maxsplit=1)[1]:
        raise SnapshotPreparationFailedError("published snapshot path and identity disagree")
    if not (path / "snapshot_manifest.json").is_file():
        raise SnapshotPreparationFailedError("published snapshot manifest is missing")
    return snapshot_id, path


def _stdout_json_object(stdout: str, label: str) -> Mapping[str, object]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(stdout):
        if character != "{":
            continue
        try:
            decoded, end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if stdout[index + end :].strip():
            continue
        if isinstance(decoded, Mapping):
            raw = cast(Mapping[object, object], decoded)
            if all(isinstance(key, str) for key in raw):
                return cast(Mapping[str, object], raw)
    raise SnapshotPreparationFailedError(f"{label} did not return one terminal JSON object")


def _json_object(path: Path, label: str) -> Mapping[str, object]:
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotPreparationFailedError(f"cannot read {label}") from exc
    if not isinstance(decoded, Mapping):
        raise SnapshotPreparationFailedError(f"{label} must be a JSON object")
    raw = cast(Mapping[object, object], decoded)
    if any(not isinstance(key, str) for key in raw):
        raise SnapshotPreparationFailedError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], raw)


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise SnapshotPreparationFailedError(f"{key} must be an object")
    raw = cast(Mapping[object, object], item)
    if any(not isinstance(name, str) for name in raw):
        raise SnapshotPreparationFailedError(f"{key} must be an object")
    return cast(Mapping[str, object], raw)


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotPreparationFailedError(f"{field_name} must be non-empty text")
    return value.strip()


def _iso_date(value: object, field_name: str) -> date:
    raw = _text(value, field_name)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise SnapshotPreparationFailedError(f"{field_name} must be an ISO date") from exc


def _bounded_diagnostic(stdout: str, stderr: str) -> str:
    text = " ".join(part.strip() for part in (stderr, stdout) if part.strip())
    if not text:
        return "command returned a non-zero status without diagnostics"
    return text[-800:]


def _resolve_under(repository_root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (repository_root / path).resolve()


__all__ = [
    "CURRENT_ON_DEMAND_EVENT_CODES",
    "STRICT_EVENT_TIMESTAMP_FLOOR",
    "STRICT_INTERNAL_DEMO_EVENT_CODES",
    "InternalDemoSnapshotPreparer",
    "PreparationCommandResult",
    "PreparationCommandRunner",
    "SubprocessPreparationCommandRunner",
]
