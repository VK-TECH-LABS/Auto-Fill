"""Container memory is cgroup usage, not a sum of per-process RSS."""

from __future__ import annotations

from pathlib import Path

from autofill.service.runner import (
    _DEFAULT_MEMORY_LIMIT_BYTES,
    container_memory_bytes,
    memory_allows_session,
    memory_limit_bytes,
    proportional_memory_bytes,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_cgroup_v2_subtracts_inactive_file(tmp_path: Path):
    cgroup = tmp_path / "v2"
    _write(cgroup / "memory.current", "1000\n")
    _write(cgroup / "memory.stat", "anon 100\ninactive_file 400\n")
    assert container_memory_bytes(v2_dir=cgroup) == 600


def test_cgroup_v1_is_the_fallback(tmp_path: Path):
    empty = tmp_path / "v2"
    empty.mkdir()
    v1 = tmp_path / "v1"
    _write(v1 / "memory.usage_in_bytes", "1000\n")
    _write(v1 / "memory.stat", "total_inactive_file 100\n")
    assert container_memory_bytes(v2_dir=empty, v1_dir=v1) == 900


def test_pss_does_not_add_shared_rss(tmp_path: Path):
    proc = tmp_path / "proc"
    _write(proc / "10" / "stat", "10 (service) S 1 0\n")
    _write(proc / "10" / "smaps_rollup", "Rss: 8000 kB\nPss: 100 kB\n")
    _write(proc / "11" / "stat", "11 (chrome) S 10 0\n")
    _write(proc / "11" / "smaps_rollup", "Rss: 9000 kB\nPrivate_Clean: 20 kB\nPrivate_Dirty: 5 kB\n")
    _write(proc / "12" / "stat", "12 (other) S 1 0\n")
    _write(proc / "12" / "smaps_rollup", "Pss: 5000 kB\n")
    assert proportional_memory_bytes(proc, 10) == (100 + 25) * 1024
    assert container_memory_bytes(proc_root=proc, start_pid=10) == (100 + 25) * 1024


def test_limit_is_headroom_or_the_environment(tmp_path: Path, monkeypatch):
    cgroup = tmp_path / "v2"
    _write(cgroup / "memory.max", "1000\n")
    monkeypatch.delenv("AUTOFILL_MEMORY_LIMIT_MB", raising=False)
    assert memory_limit_bytes(cgroup) == 850
    _write(cgroup / "memory.max", "max\n")
    assert memory_limit_bytes(cgroup) == _DEFAULT_MEMORY_LIMIT_BYTES
    monkeypatch.setenv("AUTOFILL_MEMORY_LIMIT_MB", "512")
    assert memory_limit_bytes(cgroup) == 512 * 1024 * 1024


def test_over_limit_rechecks_then_refuses(monkeypatch):
    readings = iter([100, 100, 100])

    monkeypatch.setattr("autofill.service.runner.container_memory_bytes", lambda: next(readings))
    monkeypatch.setattr("autofill.service.runner.memory_limit_bytes", lambda: 10)
    pauses: list[float] = []
    monkeypatch.setattr("autofill.service.runner.time.sleep", pauses.append)
    assert memory_allows_session() is False
    assert pauses == [0.25, 0.25]


def test_a_short_drop_allows_the_session(monkeypatch):
    readings = iter([100, 5])
    monkeypatch.setattr("autofill.service.runner.container_memory_bytes", lambda: next(readings))
    monkeypatch.setattr("autofill.service.runner.memory_limit_bytes", lambda: 10)
    pauses: list[float] = []
    monkeypatch.setattr("autofill.service.runner.time.sleep", pauses.append)
    assert memory_allows_session() is True
    assert pauses == [0.25]
