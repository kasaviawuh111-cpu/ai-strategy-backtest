"""Exercise the launcher's real stop logic without keys or real process signals."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "scripts/start_local_fullstack_review.command"
ZSH = shutil.which("zsh")
pytestmark = pytest.mark.skipif(ZSH is None, reason="local review launcher uses zsh")


def _restart(tmp_path: Path, scenario: str, source: str | None = None):
    source = source or LAUNCHER.read_text()
    start = "owned_listeners()" if "owned_listeners()" in source else "stop_owned_listener()"
    stop_logic = source[source.index(start):source.index(': > "$backend_log"')]
    for binary in ("/usr/sbin/lsof", "/bin/ps", "/bin/kill", "/bin/sleep"):
        stop_logic = stop_logic.replace(binary, "fake_" + binary.rsplit("/", 1)[1])
    harness = r'''
set -euo pipefail
project_dir='/Users/mima0000/Documents/回测'
backend_port=8011
frontend_port=5184
fake_lsof() {
  if [[ "$1" == -tiTCP:8011 ]]; then
    [[ "$scenario" == empty ]] || print 11
  elif [[ "$1" == -tiTCP:5184 ]]; then
    [[ "$scenario" == empty ]] || print 12
  elif [[ "$4" == -d ]]; then
    if [[ "$3" == 11 ]]; then
      print -r -- 'n/Users/mima0000/Documents/\xe5\x9b\x9e\xe6\xb5\x8b'
    elif [[ "$scenario" == foreign ]]; then
      print -r -- 'n/another/project/web'
    elif [[ ! -f "$probe_dir/peer_exiting" && "$scenario" != disappearing ]]; then
      print -r -- 'n/Users/mima0000/Documents/\xe5\x9b\x9e\xe6\xb5\x8b/web'
    fi
  elif [[ "$scenario" != disappearing && ! -f "$probe_dir/peer_exiting" ]]; then
    print -r -- "$3"
  fi
  return 0
}
fake_ps() { return 0; }
fake_sleep() { return 0; }
fake_kill() {
  if [[ "$1" == -0 ]]; then
    [[ ! -f "$probe_dir/dead_$2" ]]
    return
  fi
  print -r -- "$2" >> "$probe_dir/signals"
  touch "$probe_dir/dead_$2"
  # Stopping the old backend starts its launcher's frontend cleanup. The
  # socket/cwd disappear before kill -0 stops recognizing that process.
  [[ "$2" != 11 ]] || touch "$probe_dir/peer_exiting"
}
'''
    result = subprocess.run(
        [ZSH, "-c", harness + stop_logic], text=True, capture_output=True,
        env={"PATH": "/usr/bin:/bin", "probe_dir": str(tmp_path), "scenario": scenario},
    )
    signals = tmp_path / "signals"
    return result, signals.read_text().splitlines() if signals.exists() else []


def test_restart_checks_both_owned_ports_before_old_launcher_cleans_peer(tmp_path):
    result, signals = _restart(tmp_path, "owned")
    assert result.returncode == 0, result.stderr
    assert signals == ["11", "12"]


def test_foreign_frontend_does_not_stop_healthy_backend(tmp_path):
    result, signals = _restart(tmp_path, "foreign")
    assert result.returncode != 0
    assert "其他项目" in result.stderr
    assert signals == []


@pytest.mark.parametrize("scenario", ["empty", "disappearing"])
def test_empty_or_disappearing_listener_is_not_foreign(tmp_path, scenario):
    result, signals = _restart(tmp_path, scenario)
    assert result.returncode == 0, result.stderr
    assert signals == ([] if scenario == "empty" else ["11"])


@pytest.mark.parametrize("dotenv,injected,expected", [
    ("", {}, ("true", "/Volumes/外地磁盘/stock_1min")),
    ("MINUTE_GRID_ENABLED=false\nEXTERNAL_MINUTE_ROOT=/configured/data\n", {},
     ("false", "/configured/data")),
    ("MINUTE_GRID_ENABLED=false\nEXTERNAL_MINUTE_ROOT=/configured/data\n",
     {"MINUTE_GRID_ENABLED": "true", "EXTERNAL_MINUTE_ROOT": "/injected/data"},
     ("true", "/injected/data")),
])
def test_backend_only_restart_keeps_full_local_minute_configuration(tmp_path, dotenv, injected, expected):
    source = (ROOT / "scripts/start_local_dialogue_review.command").read_text()
    start = source.index('local_minute_enabled=')
    end = source.index('unset local_minute_enabled local_minute_root')
    settings = source[start:end]
    (tmp_path / ".env.local").write_text(dotenv)
    result = subprocess.run(
        [ZSH, "-c", 'set -euo pipefail\n' + settings
         + '\nprint -r -- "$MINUTE_GRID_ENABLED" "$EXTERNAL_MINUTE_ROOT"'],
        cwd=tmp_path, text=True, capture_output=True,
        env={"PATH": "/usr/bin:/bin", **injected},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ' '.join(expected)
