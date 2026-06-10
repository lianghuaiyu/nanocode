"""trajectory.export — 把一个 session 的 merged wire 导出为 trajectory bundle（docs/10 P5）。

DERIVED 只读导出层（硬边界，见 trajectory/__init__.py）：
- 本模块**只读** merged wire（经 ``events.reader.merge_session_events``），把 projection /
  metrics / eval 三路派生产物落盘为一个 bundle，**绝不**写回 wire、**绝不**驱动 runtime、
  **绝不**参与 resume / fork。
- 仅 import 同包的 ``project`` / ``metrics`` / ``eval`` 读侧 API、``schema``（PURE），与
  ``events.reader`` / ``session.v2`` 的读侧路径 helper；**绝不** import 任何 runtime 模块。
- reward / eval_result 是派生标签：只落 ``steps.jsonl`` / ``metrics.json`` / ``evals.jsonl``，
  绝不污染 wire。

健壮性铁律：导出绝不崩。空 / 缺失 session（无 wire）也产出合法 bundle——4 个文件齐全、
内容为空（空 steps.jsonl / evals.jsonl、零值 metrics / metadata）。所有解析在兜底内。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..events.reader import merge_session_events
from ..session.v2 import session_root
from . import eval as _eval
from . import metrics as _metrics
from . import project as _project
from .schema import TrajectoryMetadata

# bundle 内的标准文件名（集中此处的命名 grammar）。
_METADATA_FILE = "metadata.json"
_STEPS_FILE = "steps.jsonl"
_METRICS_FILE = "metrics.json"
_EVALS_FILE = "evals.jsonl"


def bundle_dir(session_id: str, out_dir: "str | Path | None" = None) -> Path:
    """trajectory bundle 的目标目录。

    默认 = ``session.v2.session_root(session_id) / "trajectory"``——与 wire 同 session 根、
    但**独立子目录**（绝不落在 ``agents/*/`` 下，保持 derived 投影与 execution-fact wire 物理分离）。
    传 ``out_dir`` 则覆盖为该目录（导出仍写入其下）。
    """
    if out_dir is not None:
        return Path(out_dir)
    return session_root(session_id) / "trajectory"


def export_bundle(session_id: str, out_dir: "str | Path | None" = None) -> Path:
    """把一个 session 导出为 trajectory bundle，返回 bundle 目录 Path。

    步骤（全只读 wire）：
      1. ``events = merge_session_events(session_id)``（跨 agent 读时 merge）。
      2. ``steps = project.build_steps(events)``；``metrics = metrics.compute_metrics(events, steps)``；
         ``evals = eval.online_evals(events, steps)``。
      3. 写入 bundle_dir：``metadata.json``（从 events 派生的 TrajectoryMetadata）、
         ``steps.jsonl``（每行一个 ``Step.to_record()``）、``metrics.json``、``evals.jsonl``。

    空 / 缺失 session 也产出合法的 4 文件 bundle（空内容），绝不崩。
    """
    out = bundle_dir(session_id, out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ── 只读 merge + 三路派生（各自内部已 defensive，绝不抛）──────────
    try:
        events = merge_session_events(session_id)
    except Exception:
        events = []
    try:
        steps = _project.build_steps(events)
    except Exception:
        steps = []
    try:
        metrics = _metrics.compute_metrics(events, steps)
    except Exception:
        metrics = {}
    try:
        evals = _eval.online_evals(events, steps)
    except Exception:
        evals = []
    # P4：把 online eval 的 provisional reward 回填进 steps，再落 steps.jsonl。否则 reward
    # 被算出来却从不进 dataset——导出的 steps.jsonl 的 reward 恒为 null，P4 形同虚设
    # （codex 复审发现）。attach_rewards 返回**新**列表（非就地改）、内部 defensive；
    # 失败则保留未回填的 steps。须在 metadata/落盘前完成（n_steps 不变，故 metadata 不受影响）。
    try:
        steps = _eval.attach_rewards(steps, evals)
    except Exception:
        pass

    metadata = _derive_metadata(session_id, events, steps, metrics)

    # ── 落盘（4 个文件；空 session 也写出空内容，不缺文件）────────────
    _write_json(out / _METADATA_FILE, _metadata_to_record(metadata))
    _write_jsonl(out / _STEPS_FILE, (_step_record(s) for s in steps))
    _write_json(out / _METRICS_FILE, metrics if isinstance(metrics, dict) else {})
    _write_jsonl(out / _EVALS_FILE, (e for e in evals if isinstance(e, dict)))

    return out


# ── metadata 派生（从 events / metrics，全 defensive）──────────────


def _derive_metadata(
    session_id: str, events: list, steps: list, metrics: dict,
) -> TrajectoryMetadata:
    """从 merged events + 已算的 metrics 派生 trajectory-level 元数据。

    - trajectory_id：取首个带 ``trajectory_id`` 的事件 data，否则 ``traj_<session_id>``。
    - model：首个带 ``model`` 的事件 payload。
    - start_time / end_time：events 首 / 末事件的 ts（events 已按展示序排好）。
    - final_status：末个终结事件（session_end / turn_end）的 status/final_status/reason；缺则 None。
    - token 总数：复用 metrics 的 total_input_tokens / total_output_tokens（同源、避免重复扫）。
    - n_steps：len(steps)。
    """
    traj_id = f"traj_{session_id}" if session_id else "traj_"
    start_time: "str | None" = None
    end_time: "str | None" = None

    # trajectory_id / model：各取首个带该键且值为真的事件 data；缺则保留默认。
    found_traj_id = _first_data_value(events, "trajectory_id")
    if found_traj_id is not None:
        traj_id = found_traj_id
    model = _first_data_value(events, "model")

    try:
        if events:
            start_time = _str(getattr(events[0], "ts", "")) or None
            end_time = _str(getattr(events[-1], "ts", "")) or None
    except Exception:
        pass

    final_status = _derive_final_status(events)

    m = metrics if isinstance(metrics, dict) else {}
    return TrajectoryMetadata(
        trajectory_id=traj_id,
        episode_id=session_id,
        model=model,
        start_time=start_time,
        end_time=end_time,
        final_status=final_status,
        total_input_tokens=_as_int(m.get("total_input_tokens")),
        total_output_tokens=_as_int(m.get("total_output_tokens")),
        total_cost=_as_float(m.get("est_cost_usd")),
        n_steps=len(steps) if isinstance(steps, list) else 0,
    )


def _derive_final_status(events: list) -> "str | None":
    """从末个终结事件（session_end / turn_end）尽力取终态标签。

    扫常见键 ``final_status`` / ``status`` / ``reason``；都缺则 None（不杜撰）。
    末个 budget_exceeded 也可作终态信号（reason）。
    """
    try:
        for ev in reversed(events):
            etype = _str(getattr(ev, "type", ""))
            if etype not in ("session_end", "turn_end", "budget_exceeded"):
                continue
            d = _ev_data(ev)
            for key in ("final_status", "status", "reason"):
                v = d.get(key)
                if isinstance(v, str) and v.strip():
                    return v
            # 终结事件存在但无显式状态：session_end 视作 completed 兜底，其余不杜撰。
            if etype == "session_end":
                return "completed"
            if etype == "budget_exceeded":
                return "budget_exceeded"
    except Exception:
        return None
    return None


def _metadata_to_record(m: TrajectoryMetadata) -> dict:
    """TrajectoryMetadata -> metadata.json 的 dict。"""
    return {
        "trajectory_id": m.trajectory_id,
        "episode_id": m.episode_id,
        "model": m.model,
        "start_time": m.start_time,
        "end_time": m.end_time,
        "final_status": m.final_status,
        "total_input_tokens": m.total_input_tokens,
        "total_output_tokens": m.total_output_tokens,
        "total_cost": m.total_cost,
        "n_steps": m.n_steps,
    }


def _step_record(step) -> dict:
    """Step -> steps.jsonl 行（容忍非 Step：返回原 dict 或空）。"""
    try:
        to_record = getattr(step, "to_record", None)
        if callable(to_record):
            return to_record()
        if isinstance(step, dict):
            return step
    except Exception:
        pass
    return {}


# ── 落盘 helper（绝不抛）────────────────────────────────────────────


def _write_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        return


def _write_jsonl(path: Path, records) -> None:
    """逐行写 JSONL；空迭代器写出空文件（仍创建文件）。绝不抛。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for rec in records:
                try:
                    fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                except Exception:
                    continue
    except Exception:
        return


# ── 字段安全读取 ────────────────────────────────────────────────────


def _ev_data(ev) -> dict:
    d = getattr(ev, "data", None)
    return d if isinstance(d, dict) else {}


def _first_data_value(events: list, key: str) -> "str | None":
    """取首个 ``data[key]`` 为真的事件的该值（``_str`` 后），缺则 None。绝不抛。"""
    try:
        for ev in events:
            v = _ev_data(ev).get(key)
            if v:
                return _str(v)
    except Exception:
        return None
    return None


def _str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    try:
        return str(val)
    except Exception:
        return ""


def _as_int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _as_float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0
