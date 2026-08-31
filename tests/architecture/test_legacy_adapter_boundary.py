"""Keep the legacy engine behind its one-way differential-test adapter boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "ashare_lab"
LEGACY_IMPORT = "ashare_lab.adapters.legacy"


def _module_package(path: Path) -> tuple[str, ...]:
    module_parts = path.relative_to(PROJECT_ROOT).with_suffix("").parts
    return module_parts[:-1]


def _resolved_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _module_package(path)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            keep = len(package) - (node.level - 1)
            prefix = package[:keep]
            base_parts = (*prefix, *((node.module or "").split(".")))
        else:
            base_parts = tuple((node.module or "").split("."))
        base = ".".join(part for part in base_parts if part)
        if base:
            imports.add(base)
        for alias in node.names:
            if alias.name != "*" and base:
                imports.add(f"{base}.{alias.name}")
    return imports


def test_formal_mainline_cannot_import_the_legacy_adapter() -> None:
    violations: list[str] = []
    legacy_root = PACKAGE_ROOT / "adapters" / "legacy"
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path.is_relative_to(legacy_root):
            continue
        for imported in _resolved_imports(path):
            if imported == LEGACY_IMPORT or imported.startswith(f"{LEGACY_IMPORT}."):
                violations.append(f"{path.relative_to(PROJECT_ROOT)} imports {imported}")

    assert violations == []
