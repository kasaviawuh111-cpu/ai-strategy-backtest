#!/usr/bin/env python3
"""Write a deterministic provenance record for the Live web build."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def source_digest(repository: Path) -> str:
    paths = [
        path for path in sorted((repository / "web/src").rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]
    paths.extend(
        repository / name
        for name in ("web/package.json", "web/tsconfig.json", "web/tsconfig.app.json", "web/vite.same-origin.config.ts")
        if (repository / name).is_file()
    )
    records = [
        {"path": path.relative_to(repository).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(paths)
    ]
    return hashlib.sha256(json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dist", type=Path, default=Path(__file__).resolve().parents[1] / "web/dist")
    args = parser.parse_args()
    root = args.source_root.resolve()
    output = args.dist.resolve() / "build-provenance.json"
    output.write_text(json.dumps({
        "schemaVersion": "ashare-lab.web-build-provenance.v1",
        "sourceDigest": source_digest(root),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"WEB_BUILD_SOURCE_DIGEST={source_digest(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
