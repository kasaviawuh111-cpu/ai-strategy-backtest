from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from ashare_lab.adapters.event_sources import EastmoneyAnnouncementError
from ashare_lab.adapters.event_sources.document_text import (
    DOCUMENT_TEXT_INCOMPLETE_EXIT_CODE,
    DocumentTextExtractionError,
    DocumentTextIncompleteError,
)
from ashare_lab.adapters.market_data.choice_snapshot import (
    CHOICE_DATA_INTEGRITY_EXIT_CODE,
    CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE,
)
from ashare_lab.adapters.market_data.internal_demo_preparer import (
    InternalDemoSnapshotPreparer,
    PreparationCommandResult,
    SubprocessPreparationCommandRunner,
)
from ashare_lab.adapters.market_data.on_demand_snapshot import (
    SnapshotPreparationDocumentTextIncompleteError,
    SnapshotPreparationFailedError,
    SnapshotPreparationUnsupportedError,
)
from ashare_lab.domain.shared import InstrumentId
from ashare_lab.ports.market_data import DataRequirements, DateRange
from scripts.prepare_event_snapshot import _has_document_text_incomplete_error

ANNUAL = "event.financial_results.annual_report"
REPURCHASE_CHANGE = "event.repurchase_capital.repurchase_change"


def test_subprocess_runner_exposes_repository_package_and_removes_proxy_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(
        "ashare_lab.adapters.market_data.internal_demo_preparer.subprocess.run",
        fake_run,
    )
    monkeypatch.setenv("PYTHONPATH", "existing-path")
    monkeypatch.setenv("HTTP_PROXY", "http://must-not-leak.invalid")

    result = SubprocessPreparationCommandRunner().run(("python", "script.py"), cwd=tmp_path)

    assert result.returncode == 0
    environment = observed["env"]
    assert isinstance(environment, dict)
    assert environment["PYTHONPATH"] == os.pathsep.join((str(tmp_path), "existing-path"))
    assert "HTTP_PROXY" not in environment


@pytest.mark.parametrize(
    "source_error",
    [
        subprocess.TimeoutExpired(cmd=("python", "script.py"), timeout=1),
        OSError("cannot start process"),
    ],
)
def test_subprocess_timeout_or_start_failure_remains_preparation_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_error: BaseException,
) -> None:
    def fail_run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del argv, kwargs
        raise source_error

    monkeypatch.setattr(
        "ashare_lab.adapters.market_data.internal_demo_preparer.subprocess.run",
        fail_run,
    )

    with pytest.raises(SnapshotPreparationFailedError, match=type(source_error).__name__):
        SubprocessPreparationCommandRunner(timeout_seconds=1).run(
            ("python", "script.py"),
            cwd=tmp_path,
        )


class FakeRunner:
    def __init__(
        self,
        *,
        fail_label: str | None = None,
        fail_returncode: int = 1,
        unsafe_path: bool = False,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_label = fail_label
        self.fail_returncode = fail_returncode
        self.unsafe_path = unsafe_path

    def run(self, argv: Sequence[str], *, cwd: Path) -> PreparationCommandResult:
        command = tuple(str(item) for item in argv)
        self.calls.append(command)
        script = command[1]
        if self.fail_label is not None and self.fail_label in script:
            return PreparationCommandResult(
                self.fail_returncode,
                "provider failed",
                "network unavailable",
            )
        if script.endswith("prepare_baostock_reference.py"):
            instrument = command[command.index("--symbol") + 1]
            code = instrument.split(".", maxsplit=1)[0]
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                json.dumps(
                    {
                        "schemaVersion": "baostock.internal-demo-reference.v2",
                        "instrument": {
                            "instrument_id": instrument,
                            "listing_date": "2010-03-19",
                            "board": (
                                "stock_etf"
                                if code.startswith(("5", "1"))
                                else "chinext"
                                if code.startswith(("300", "301"))
                                else "star"
                                if code.startswith(("688", "689"))
                                else "main"
                            ),
                            "asset_type": ("ETF" if code.startswith(("5", "1")) else "STOCK"),
                        },
                        "actions": [],
                        "coverage": {},
                        "historicalSessions": {"rows": [], "coverage": {}},
                    }
                ),
                encoding="utf-8",
            )
            return PreparationCommandResult(0, "login success!\n{}", "")
        if script.endswith("prepare_eastmoney_corporate_actions.py"):
            instrument = command[command.index("--symbol") + 1]
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                json.dumps(
                    {
                        "schemaVersion": "eastmoney.corporate-action-reference.v1",
                        "instrumentId": instrument,
                        "actions": [],
                        "coverage": {},
                    }
                ),
                encoding="utf-8",
            )
            return PreparationCommandResult(0, json.dumps({"status": "ok"}), "")
        if script.endswith("prepare_choice_snapshot.py"):
            output_root = Path(command[command.index("--output-root") + 1])
            digest = "a" * 64
            path = output_root / digest
            path.mkdir(parents=True)
            (path / "snapshot_manifest.json").write_text("{}", encoding="utf-8")
            published = Path("/tmp/outside") if self.unsafe_path else path
            return PreparationCommandResult(
                0,
                "sdk log\n"
                + json.dumps(
                    {
                        "status": "ok",
                        "snapshotId": f"choice:{digest}",
                        "path": str(published),
                    }
                ),
                "",
            )
        if script.endswith("prepare_baostock_snapshot.py"):
            output_root = Path(command[command.index("--output-root") + 1])
            digest = "e" * 64
            path = output_root / digest
            path.mkdir(parents=True)
            (path / "snapshot_manifest.json").write_text("{}", encoding="utf-8")
            return PreparationCommandResult(
                0,
                json.dumps(
                    {
                        "status": "ok",
                        "snapshotId": f"technical:{digest}",
                        "path": str(path),
                    }
                ),
                "",
            )
        if script.endswith("prepare_eastmoney_snapshot.py"):
            output_root = Path(command[command.index("--output-root") + 1])
            digest = "c" * 64
            path = output_root / digest
            path.mkdir(parents=True)
            (path / "snapshot_manifest.json").write_text("{}", encoding="utf-8")
            return PreparationCommandResult(
                0,
                json.dumps(
                    {
                        "status": "ok",
                        "snapshotId": f"technical:{digest}",
                        "path": str(path),
                    }
                ),
                "",
            )
        if script.endswith("prepare_etf_snapshot.py"):
            output_root = Path(command[command.index("--output-root") + 1])
            digest = "d" * 64
            path = output_root / digest
            path.mkdir(parents=True)
            (path / "snapshot_manifest.json").write_text("{}", encoding="utf-8")
            return PreparationCommandResult(
                0,
                json.dumps(
                    {
                        "status": "ok",
                        "snapshotId": f"technical:{digest}",
                        "path": str(path),
                    }
                ),
                "",
            )
        if script.endswith("prepare_event_snapshot.py"):
            output_root = Path(command[command.index("--composite-output") + 1])
            digest = "b" * 64
            path = output_root / digest
            path.mkdir(parents=True)
            (path / "snapshot_manifest.json").write_text("{}", encoding="utf-8")
            return PreparationCommandResult(
                0,
                json.dumps(
                    {
                        "compositeSnapshotId": f"composite:{digest}",
                        "compositeSnapshotPath": str(path),
                    }
                ),
                "",
            )
        raise AssertionError(f"unexpected command: {command}")


def _preparer(
    tmp_path: Path,
    runner: FakeRunner,
    *,
    daily_source: str = "choice_then_eastmoney",
) -> InternalDemoSnapshotPreparer:
    return InternalDemoSnapshotPreparer(
        tmp_path,
        python_executable=Path("/usr/bin/python3"),
        choice_output_root="choice",
        technical_output_root="technical",
        event_output_root="events",
        composite_output_root="composite",
        temporary_root="temporary",
        daily_source=daily_source,
        command_runner=runner,
    )


def test_preparer_preserves_virtualenv_interpreter_symlink(tmp_path: Path) -> None:
    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(Path("/usr/bin/python3"))

    preparer = InternalDemoSnapshotPreparer(
        tmp_path,
        python_executable=interpreter,
        command_runner=FakeRunner(),
    )

    assert preparer._python_executable == str(interpreter)


def _requirements(
    *,
    events: bool = False,
    minute: bool = False,
    instrument: str = "300059.SZ",
    event_code: str = ANNUAL,
    document_text: bool = False,
) -> DataRequirements:
    return DataRequirements(
        instruments=(InstrumentId(instrument),),
        datasets=(
            ("daily_ohlcv", "corporate_actions", "events")
            if events
            else ("daily_ohlcv", "corporate_actions")
        ),
        event_codes=(event_code,) if events else (),
        needs_event_document_text=document_text,
        needs_minute=minute,
    )


def test_technical_request_publishes_no_event_event_v2_and_composite_v2(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    preparer = _preparer(tmp_path, runner)

    result = preparer.prepare(
        _requirements(),
        DateRange(date(2016, 1, 1), date(2026, 1, 1)),
    )

    assert result.producer_snapshot_id == f"composite:{'b' * 64}"
    assert [Path(call[1]).name for call in runner.calls] == [
        "prepare_baostock_reference.py",
        "prepare_eastmoney_corporate_actions.py",
        "prepare_choice_snapshot.py",
        "prepare_event_snapshot.py",
    ]
    choice = runner.calls[2]
    assert choice[choice.index("--prefix-end") + 1] == "2025-01-01"
    assert "--session-reference-json" in choice
    assert "--corporate-actions-json" in choice
    assert "--research-assume-never-st" not in choice
    event_command = runner.calls[3]
    assert "--no-event-required" in event_command
    assert "--event-code" not in event_command


def test_explicit_baostock_daily_source_uses_only_one_stock_daily_acquisition(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    result = _preparer(
        tmp_path,
        runner,
        daily_source="baostock_stock_only",
    ).prepare(
        _requirements(),
        DateRange(date(2021, 1, 1), date(2026, 1, 1)),
    )

    assert result.producer_snapshot_id == f"composite:{'b' * 64}"
    assert [Path(call[1]).name for call in runner.calls] == [
        "prepare_baostock_reference.py",
        "prepare_eastmoney_corporate_actions.py",
        "prepare_baostock_snapshot.py",
        "prepare_event_snapshot.py",
    ]
    assert not any("prepare_choice_snapshot.py" in call[1] for call in runner.calls)
    assert not any("prepare_eastmoney_snapshot.py" in call[1] for call in runner.calls)
    baostock_daily = runner.calls[2]
    assert baostock_daily[baostock_daily.index("--symbol") + 1] == "300059.SZ"


def test_explicit_baostock_daily_source_does_not_silently_fallback(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(fail_label="prepare_baostock_snapshot.py")

    with pytest.raises(SnapshotPreparationFailedError, match="network unavailable"):
        _preparer(
            tmp_path,
            runner,
            daily_source="baostock_stock_only",
        ).prepare(
            _requirements(),
            DateRange(date(2021, 1, 1), date(2026, 1, 1)),
        )

    assert [Path(call[1]).name for call in runner.calls] == [
        "prepare_baostock_reference.py",
        "prepare_eastmoney_corporate_actions.py",
        "prepare_baostock_snapshot.py",
    ]


def test_explicit_baostock_daily_source_rejects_etf_instead_of_masquerading(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    with pytest.raises(SnapshotPreparationUnsupportedError, match="does not cover ETFs"):
        _preparer(
            tmp_path,
            runner,
            daily_source="baostock_stock_only",
        ).prepare(
            _requirements(instrument="510300.SH"),
            DateRange(date(2021, 1, 1), date(2026, 1, 1)),
        )

    assert [Path(call[1]).name for call in runner.calls] == [
        "prepare_baostock_reference.py",
    ]


def test_event_request_publishes_composite_with_exact_requested_codes(tmp_path: Path) -> None:
    runner = FakeRunner()
    preparer = _preparer(tmp_path, runner)

    result = preparer.prepare(
        _requirements(events=True),
        DateRange(date(2021, 1, 1), date(2026, 1, 1)),
    )

    assert result.producer_snapshot_id == f"composite:{'b' * 64}"
    assert [Path(call[1]).name for call in runner.calls] == [
        "prepare_baostock_reference.py",
        "prepare_eastmoney_corporate_actions.py",
        "prepare_choice_snapshot.py",
        "prepare_event_snapshot.py",
    ]
    event_command = runner.calls[-1]
    assert event_command[event_command.index("--event-code") + 1] == ANNUAL
    assert "--no-event-required" not in event_command


def test_provider_column_repurchase_change_is_forwarded_to_event_acquisition(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    _preparer(tmp_path, runner).prepare(
        _requirements(events=True, event_code=REPURCHASE_CHANGE),
        DateRange(date(2021, 1, 1), date(2026, 1, 1)),
    )

    event_command = runner.calls[-1]
    assert event_command[event_command.index("--event-code") + 1] == REPURCHASE_CHANGE
    assert "--extract-document-text" not in event_command


def test_document_metric_request_enables_complete_report_text_extraction(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    _preparer(tmp_path, runner).prepare(
        _requirements(events=True, document_text=True),
        DateRange(date(2021, 1, 1), date(2026, 1, 1)),
    )

    assert "--extract-document-text" in runner.calls[-1]


def test_document_text_incomplete_cause_chain_is_classified_structurally() -> None:
    extraction_error = DocumentTextIncompleteError("page 3 has no embedded text")
    provider_wrapper = EastmoneyAnnouncementError("provider wrapper")
    provider_wrapper.__cause__ = extraction_error
    collector_wrapper = RuntimeError("collector wrapper")
    collector_wrapper.__cause__ = provider_wrapper

    assert _has_document_text_incomplete_error(collector_wrapper)
    assert not _has_document_text_incomplete_error(RuntimeError("network unavailable"))
    assert not _has_document_text_incomplete_error(
        DocumentTextExtractionError("pypdfium2 dependency unavailable")
    )


def test_document_text_incomplete_exit_maps_to_snapshot_incomplete(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        fail_label="prepare_event_snapshot.py",
        fail_returncode=DOCUMENT_TEXT_INCOMPLETE_EXIT_CODE,
    )

    with pytest.raises(
        SnapshotPreparationDocumentTextIncompleteError,
        match="complete document-text coverage",
    ):
        _preparer(tmp_path, runner).prepare(
            _requirements(events=True, document_text=True),
            DateRange(date(2021, 1, 1), date(2026, 1, 1)),
        )


def test_document_text_provider_failure_remains_snapshot_failed(tmp_path: Path) -> None:
    runner = FakeRunner(
        fail_label="prepare_event_snapshot.py",
        fail_returncode=1,
    )

    with pytest.raises(SnapshotPreparationFailedError, match="network unavailable"):
        _preparer(tmp_path, runner).prepare(
            _requirements(events=True, document_text=True),
            DateRange(date(2021, 1, 1), date(2026, 1, 1)),
        )


def test_non_document_exit_code_is_not_reclassified_as_document_incomplete(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        fail_label="prepare_event_snapshot.py",
        fail_returncode=DOCUMENT_TEXT_INCOMPLETE_EXIT_CODE,
    )

    with pytest.raises(SnapshotPreparationFailedError, match="network unavailable"):
        _preparer(tmp_path, runner).prepare(
            _requirements(),
            DateRange(date(2021, 1, 1), date(2026, 1, 1)),
        )


def test_event_history_before_credible_timestamp_floor_fails_before_network(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    with pytest.raises(SnapshotPreparationUnsupportedError, match="2017-01-01"):
        _preparer(tmp_path, runner).prepare(
            _requirements(events=True),
            DateRange(date(2016, 8, 30), date(2026, 8, 30)),
        )

    assert runner.calls == []


def test_choice_failure_falls_back_to_offline_push2_snapshot_pipeline(tmp_path: Path) -> None:
    runner = FakeRunner(
        fail_label="prepare_choice_snapshot.py",
        fail_returncode=CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE,
    )

    result = _preparer(tmp_path, runner).prepare(
        _requirements(),
        DateRange(date(2021, 1, 1), date(2026, 1, 1)),
    )

    assert result.producer_snapshot_id == f"composite:{'b' * 64}"
    assert [Path(call[1]).name for call in runner.calls] == [
        "prepare_baostock_reference.py",
        "prepare_eastmoney_corporate_actions.py",
        "prepare_choice_snapshot.py",
        "prepare_eastmoney_snapshot.py",
        "prepare_event_snapshot.py",
    ]
    fallback = runner.calls[3]
    assert fallback[fallback.index("--fallback-reason") + 1] == "choice_daily_exit_75"
    fallback_root = Path(fallback[fallback.index("--output-root") + 1])
    choice_root = Path(runner.calls[2][runner.calls[2].index("--output-root") + 1])
    assert fallback_root == tmp_path / "technical"
    assert choice_root == tmp_path / "choice"
    assert fallback_root != choice_root
    event_command = runner.calls[4]
    selected = Path(event_command[event_command.index("--choice-snapshot") + 1])
    assert selected.name == "c" * 64


def test_etf_uses_choice_first_then_strict_etf_fallback_and_composite(tmp_path: Path) -> None:
    runner = FakeRunner(
        fail_label="prepare_choice_snapshot.py",
        fail_returncode=CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE,
    )

    result = _preparer(tmp_path, runner).prepare(
        _requirements(instrument="510300.SH"),
        DateRange(date(2021, 1, 1), date(2026, 1, 1)),
    )

    assert result.producer_snapshot_id == f"composite:{'b' * 64}"
    assert [Path(call[1]).name for call in runner.calls] == [
        "prepare_baostock_reference.py",
        "prepare_choice_snapshot.py",
        "prepare_etf_snapshot.py",
        "prepare_event_snapshot.py",
    ]
    assert not any(
        call[1].endswith("prepare_eastmoney_corporate_actions.py") for call in runner.calls
    )
    fallback = runner.calls[2]
    assert fallback[fallback.index("--reference-json") + 1].endswith("baostock-reference.json")
    assert fallback[fallback.index("--fallback-reason") + 1] == "choice_daily_exit_75"
    event = runner.calls[3]
    selected = Path(event[event.index("--choice-snapshot") + 1])
    assert selected.name == "d" * 64
    assert "--no-event-required" in event


def test_choice_and_push2_failure_remains_snapshot_failed(tmp_path: Path) -> None:
    class BothDailyProvidersFail(FakeRunner):
        def run(self, argv: Sequence[str], *, cwd: Path) -> PreparationCommandResult:
            command = tuple(str(item) for item in argv)
            if command[1].endswith("prepare_eastmoney_snapshot.py"):
                self.calls.append(command)
                return PreparationCommandResult(1, "", "push2 unavailable")
            return super().run(argv, cwd=cwd)

    runner = BothDailyProvidersFail(
        fail_label="prepare_choice_snapshot.py",
        fail_returncode=CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE,
    )

    with pytest.raises(SnapshotPreparationFailedError, match="push2 unavailable"):
        _preparer(tmp_path, runner).prepare(
            _requirements(),
            DateRange(date(2021, 1, 1), date(2026, 1, 1)),
        )


def test_choice_integrity_failure_never_falls_back_to_push2(tmp_path: Path) -> None:
    runner = FakeRunner(
        fail_label="prepare_choice_snapshot.py",
        fail_returncode=CHOICE_DATA_INTEGRITY_EXIT_CODE,
    )

    with pytest.raises(
        SnapshotPreparationFailedError,
        match="integrity validation; Push2 fallback is forbidden",
    ):
        _preparer(tmp_path, runner).prepare(
            _requirements(),
            DateRange(date(2021, 1, 1), date(2026, 1, 1)),
        )

    assert [Path(call[1]).name for call in runner.calls] == [
        "prepare_baostock_reference.py",
        "prepare_eastmoney_corporate_actions.py",
        "prepare_choice_snapshot.py",
    ]


def test_published_path_must_be_inside_the_configured_content_store(tmp_path: Path) -> None:
    runner = FakeRunner(unsafe_path=True)

    with pytest.raises(SnapshotPreparationFailedError, match="outside"):
        _preparer(tmp_path, runner).prepare(
            _requirements(),
            DateRange(date(2021, 1, 1), date(2026, 1, 1)),
        )


def test_intraday_request_is_rejected_before_any_provider_call(tmp_path: Path) -> None:
    runner = FakeRunner()

    with pytest.raises(SnapshotPreparationUnsupportedError, match="daily datasets only"):
        _preparer(tmp_path, runner).prepare(
            _requirements(minute=True),
            DateRange(date(2021, 1, 1), date(2026, 1, 1)),
        )

    assert runner.calls == []


def test_uncovered_event_source_is_rejected_before_any_provider_call(tmp_path: Path) -> None:
    runner = FakeRunner()

    with pytest.raises(SnapshotPreparationUnsupportedError, match="do not support event codes"):
        _preparer(tmp_path, runner).prepare(
            _requirements(
                events=True,
                event_code="event.macro_policy_industry.license_approval",
            ),
            DateRange(date(2021, 1, 1), date(2026, 1, 1)),
        )

    assert runner.calls == []


def test_authoritative_issuer_major_award_is_forwarded_to_event_acquisition(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    result = _preparer(tmp_path, runner).prepare(
        _requirements(
            events=True,
            event_code="event.contracts_orders.major_contract_won",
        ),
        DateRange(date(2021, 1, 1), date(2026, 1, 1)),
    )

    assert result.producer_snapshot_id == f"composite:{'b' * 64}"
    event_command = runner.calls[-1]
    assert event_command[event_command.index("--event-code") + 1] == (
        "event.contracts_orders.major_contract_won"
    )
    assert "--no-event-required" not in event_command


@pytest.mark.parametrize("instrument", ["430047.BJ", "830799.BJ", "920000.BJ"])
def test_every_bse_code_space_is_rejected_before_any_provider_call(
    tmp_path: Path,
    instrument: str,
) -> None:
    runner = FakeRunner()

    with pytest.raises(
        SnapshotPreparationUnsupportedError,
        match=r"any Beijing Stock Exchange \(\.BJ\).*historical BSE venue admission",
    ):
        _preparer(tmp_path, runner).prepare(
            _requirements(instrument=instrument),
            DateRange(date(2021, 1, 1), date(2026, 1, 1)),
        )

    assert runner.calls == []


@pytest.mark.parametrize(
    "instrument",
    [
        "600519.SH",
        "688001.SH",
        "689001.SH",
        "000001.SZ",
        "300059.SZ",
        "301001.SZ",
    ],
)
def test_shanghai_and_shenzhen_boards_are_supported(
    tmp_path: Path,
    instrument: str,
) -> None:
    runner = FakeRunner()

    result = _preparer(tmp_path, runner).prepare(
        _requirements(instrument=instrument),
        DateRange(date(2021, 1, 1), date(2026, 1, 1)),
    )

    assert result.producer_snapshot_id == f"composite:{'b' * 64}"
    assert [Path(call[1]).name for call in runner.calls] == [
        "prepare_baostock_reference.py",
        "prepare_eastmoney_corporate_actions.py",
        "prepare_choice_snapshot.py",
        "prepare_event_snapshot.py",
    ]
    assert "--no-event-required" in runner.calls[-1]
