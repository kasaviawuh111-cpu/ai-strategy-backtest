from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_ROOT = PROJECT_ROOT / "ashare_lab" / "domain"
APPLICATION_ROOT = PROJECT_ROOT / "ashare_lab" / "application"
PORTS_ROOT = PROJECT_ROOT / "ashare_lab" / "ports"
FORBIDDEN_DOMAIN_IMPORTS = (
    "alembic",
    "fastapi",
    "httpx",
    "psycopg",
    "redis",
    "rq",
    "sqlalchemy",
    "uvicorn",
    "ashare_lab.adapters",
    "ashare_lab.api",
    "ashare_lab.application",
    "ashare_lab.worker",
)
FORBIDDEN_CATCH_ALL_FILES = {"common.py", "helpers.py", "services.py", "utils.py"}
FORBIDDEN_INNER_LAYER_IMPORTS = (
    "alembic",
    "fastapi",
    "httpx",
    "psycopg",
    "redis",
    "rq",
    "sqlalchemy",
    "uvicorn",
    "ashare_lab.adapters",
    "ashare_lab.api",
    "ashare_lab.worker",
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_domain_has_no_infrastructure_imports() -> None:
    violations: list[str] = []
    for path in DOMAIN_ROOT.rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith(FORBIDDEN_DOMAIN_IMPORTS):
                violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {imported}")
    assert violations == []


def test_new_package_has_no_catch_all_modules() -> None:
    violations = [
        str(path.relative_to(PROJECT_ROOT))
        for path in (PROJECT_ROOT / "ashare_lab").rglob("*.py")
        if path.name in FORBIDDEN_CATCH_ALL_FILES
    ]
    assert violations == []


def test_application_and_ports_do_not_reach_out_to_infrastructure() -> None:
    violations: list[str] = []
    for root in (APPLICATION_ROOT, PORTS_ROOT):
        for path in root.rglob("*.py"):
            for imported in _imports(path):
                if imported.startswith(FORBIDDEN_INNER_LAYER_IMPORTS):
                    violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {imported}")
    assert violations == []


def test_new_mainline_cannot_call_the_legacy_engine_directly() -> None:
    violations: list[str] = []
    for path in (PROJECT_ROOT / "ashare_lab").rglob("*.py"):
        if "adapters/legacy" in path.as_posix():
            continue
        for imported in _imports(path):
            if imported == "astock_backtest" or imported.startswith("astock_backtest."):
                violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {imported}")
    assert violations == []
