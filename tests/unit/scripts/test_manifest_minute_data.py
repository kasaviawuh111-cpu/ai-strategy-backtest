import hashlib

import pytest

from scripts.manifest_minute_data import build_manifest


def test_manifest_hashes_all_markets_without_changing_sources(tmp_path):
    for name in ("000001.SZ.parquet", "600000.SH.parquet", "920001.BJ.parquet"):
        (tmp_path / name).write_bytes(name.encode())
    (tmp_path / "._000001.SZ.parquet").write_bytes(b"mac metadata")
    result = build_manifest(tmp_path)
    assert result["fileCount"] == 3
    for item in result["files"]:
        data = (tmp_path / item["path"]).read_bytes()
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
        assert item["bytes"] == len(data)
    assert build_manifest(tmp_path) == result


def test_empty_or_symlink_source_rejected(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        build_manifest(tmp_path)
    (tmp_path / "000001.SZ.parquet").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="Unexpected"):
        build_manifest(tmp_path)
