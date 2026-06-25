"""memory/retrieval_config_store.py — promoted RetrievalConfig + optimize history.

The single source of truth for the live retrieval policy is
`{store_root}/retrieval_config.json` (the active SimpleMem project-hash store root —
docs/22 §5.1). This replaces the old markdown-centric `maintenance.evolve_config`
lifecycle (deleted in Phase 7); there is exactly one config truth source.

Layout under the store root:
    retrieval_config.json
    retrieval_config.<timestamp>.<run_id>.bak   (rotated backups of the previous config)
    optimize/history.jsonl                       (append-only one-line-per-run audit)
    optimize/runs/<run_id>/summary.json          (per-run summary)
    optimize/runs/<run_id>/cases.jsonl           (per-question eval report)

Writes are atomic: each goes to a UNIQUE same-dir temp file then `os.replace`
(unique so two concurrent optimize tasks for the same store never clobber a shared
temp). Load fails loud on malformed JSON / unknown fields (no silent default), so a
corrupt config surfaces as an explicit-simplemem init failure (service
diagnostic), never a markdown fallback (docs/22 §9.1.10).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from .engines.simplemem.retrieval_config import RetrievalConfig

_CONFIG_NAME = "retrieval_config.json"


def store_root_for_engine(engine) -> str:
    """Resolved on-disk store root of a SimpleMem engine (where the config lives)."""
    return engine.stats()["root"]


def config_path(store_root: str) -> Path:
    return Path(store_root) / _CONFIG_NAME


def run_summary_path(store_root: str, run_id: str) -> Path:
    return Path(store_root) / "optimize" / "runs" / run_id / "summary.json"


def load_retrieval_config(store_root: str) -> RetrievalConfig:
    """Load the promoted config, or the default when none has been written.

    Malformed JSON / unknown fields raise (fail loud) — the caller surfaces this
    as an explicit backend failure, never a silent default (docs/22 §9.1.10)."""
    path = config_path(store_root)
    if not path.exists():
        return RetrievalConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"malformed retrieval_config.json at {path}: {e}") from e
    return RetrievalConfig.from_dict(data)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically via a UNIQUE same-dir temp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: dict) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False))


def _ts() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _write_run_report(store_root: str, run_id: str, report: dict) -> Path:
    """Persist a per-run report (summary.json + cases.jsonl) and append history."""
    run_dir = Path(store_root) / "optimize" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(run_dir / "summary.json", report.get("summary", {}))
    cases = report.get("cases", [])
    # Atomic (build in memory, single replace) — the module's atomicity invariant
    # covers report files too, so a crash never leaves a truncated cases.jsonl.
    _atomic_write_text(run_dir / "cases.jsonl",
                       "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases))
    hist = report.get("history")
    if hist is not None:
        _append_history(store_root, hist)
    return run_dir


def _append_history(store_root: str, entry: dict) -> None:
    hist_path = Path(store_root) / "optimize" / "history.jsonl"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    with hist_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def save_retrieval_config(store_root: str, config: "RetrievalConfig | None", *,
                          run_id: str, report: dict) -> str:
    """Persist a run report and, when `config` is not None, atomically promote it
    as the live `retrieval_config.json` (backing up the previous config).

    Returns the primary path written: the config path on promotion, else the run
    summary path. Promotion is the only path that writes the live config — the
    deterministic gate that calls this is the sole writer (docs/22 §4.2)."""
    run_dir = _write_run_report(store_root, run_id, report)
    if config is None:
        return str(run_dir / "summary.json")
    config.validate()
    path = config_path(store_root)
    if path.exists():
        # run_id-stamped backup: two promotions in the same wall-clock second no
        # longer collide (the second would otherwise clobber the first backup).
        shutil.copy2(str(path), str(Path(store_root) / f"retrieval_config.{_ts()}.{run_id}.bak"))
    _atomic_write_json(path, config.to_dict())
    return str(path)


def rollback_retrieval_config(store_root: str) -> bool:
    """Restore the most recent VALID backup over the live config.

    Validates each backup (newest first) and skips unparseable/invalid ones so a
    corrupt backup is never installed and reported as success. Backs up the current
    live config first, so a rollback is itself reversible. False if no valid backup
    exists."""
    root = Path(store_root)
    # Order by mtime (most recent first), not lexicographic filename: within the
    # same _ts() second the run_id suffix is random, so name-order could pick an
    # older backup. mtime reflects true write order.
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    baks = sorted(root.glob("retrieval_config.*.bak"), key=_mtime, reverse=True)
    for bak in baks:
        try:
            RetrievalConfig.from_dict(json.loads(bak.read_text(encoding="utf-8")))
        except Exception:
            continue  # skip corrupt/invalid backup, try the next-older one
        live = config_path(store_root)
        if live.exists():
            shutil.copy2(str(live), str(root / f"retrieval_config.{_ts()}.rollback.bak"))
        shutil.copy2(str(bak), str(live))
        return True
    return False
