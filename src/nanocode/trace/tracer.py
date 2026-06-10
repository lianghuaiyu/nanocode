"""Tracer：在明确节点 emit 事件给各 sink；关闭态为零开销 no-op。

事件 spine（docs/09「现有事件源对账」）：Tracer 在原有事件 dict 上 **flat-additive**
地补 envelope 树链接字段——`id`/`agent_id`/`branch_id`/`parent_id`/`turn_id`——使
per-agent `wire.jsonl` 成为 Pi 风格的盘上 entry tree。所有 enrich 逻辑都在 emit 的
try 内，保住「instrumentation 绝不影响 agent」。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from ..events.models import event_id
from .redaction import apply_summary_shaping
from .sinks import Sink

SCHEMA_VERSION = 1


class Tracer:
    def __init__(
        self,
        session_id: str,
        sinks: "list[Sink]",
        parent_session_id: "str | None" = None,
        *,
        agent_id: str = "main",
        branch_id: str = "main",
        start_seq: int = 0,
        trajectory_enabled: bool = False,
        trajectory_level: str = "summary",
        trajectory_id: "str | None" = None,
    ) -> None:
        self.session_id = session_id
        self.parent_session_id = parent_session_id
        self.sinks = sinks
        self.agent_id = agent_id
        self.branch_id = branch_id
        self._seq = start_seq
        # trajectory 采集（docs/10）：execution-fact wire 的附加投影开关；flat-additive 信封键，
        # 关闭/FULL 时 payload byte-identical，SUMMARY 时由 apply_summary_shaping 整形（见 emit）。
        self.trajectory_enabled = trajectory_enabled
        self.trajectory_level = trajectory_level
        self.trajectory_id = trajectory_id or (
            f"traj_{session_id}" if trajectory_enabled else None
        )
        # resume-safe 链接：若从 wire tail 续号（start_seq>0），首个新事件的 parent
        # 即上一轮 tail 的（可确定性反推的）id evt_{agent_id}_{start_seq-1}。
        self._last_event_id: "str | None" = (
            event_id(agent_id, start_seq - 1) if start_seq > 0 else None
        )
        self._turn_id: "str | None" = None
        # fork：下一条 emit 要带的一次性 parent_event_id（分支首事件指向 fork 点）。
        self._pending_parent_event_id: "str | None" = None

    def begin_branch(self, branch_id: str, *, from_event_id: "str | None" = None) -> None:
        """切到一个新分支（fork）：后续事件携带新 branch_id，且分支的第一条事件带
        parent_event_id=from_event_id（指向 fork 点），其 parent_id 也接到 fork 点而非上一条。"""
        self.branch_id = branch_id
        if from_event_id is not None:
            self._pending_parent_event_id = from_event_id
            self._last_event_id = from_event_id  # 分支首事件的 parent_id 接到 fork 点

    def begin_turn(self, turn_id: "str | None" = None) -> str:
        """标记一个 turn（一次用户输入）的开始；后续事件携带该 turn_id。

        缺省 turn_id 由 **resume-safe 的 seq** 派生（``turn_{agent_id}_{seq}``，seq 为本
        turn 首个事件的序号）——而非可重置计数器，否则 resume 后新 turn 会与上一轮的
        ``turn_1`` 碰撞（与 event id 同一类 resume 安全问题）。调用方亦可显式传 turn_id。
        """
        self._turn_id = turn_id or f"turn_{self.agent_id}_{self._seq}"
        return self._turn_id

    def emit(self, type: str, **fields: Any) -> None:
        try:
            # payload 先铺底，envelope 字段随后**全部**覆盖写入——确保 envelope 对所有键
            # authoritative（含 v/ts/session_id/parent_session_id/seq/type）。否则同名 payload
            # kwarg（如误传 seq=）会篡改 envelope，造成 id↔seq 错位、resume 续号被污染。
            event = dict(fields)
            # 硬边界（用户强制）：reward / eval_result 是**派生标签**，绝不进 wire 事实源。
            # 即便调用方误传（tracer.emit(..., reward=...)）也在此无条件剥除——靠守卫而非约定，
            # 使「metrics/evals never contaminate wire」成为结构保证。derived 标签只活在
            # trajectory 的 metrics.json / evals.jsonl（export 层落盘，绝不回写 wire）。
            event.pop("reward", None)
            event.pop("eval_result", None)
            event["v"] = SCHEMA_VERSION
            event["ts"] = datetime.now(timezone.utc).isoformat()
            event["session_id"] = self.session_id
            event["parent_session_id"] = self.parent_session_id
            event["seq"] = self._seq
            event["type"] = type
            ev_id = event_id(self.agent_id, self._seq)
            event["id"] = ev_id
            event["agent_id"] = self.agent_id
            event["branch_id"] = self.branch_id
            event["parent_id"] = self._last_event_id
            event["turn_id"] = self._turn_id
            # fork：分支首事件带一次性 parent_event_id（指向 fork 点），随后清掉。
            if self._pending_parent_event_id is not None:
                event["parent_event_id"] = self._pending_parent_event_id
                self._pending_parent_event_id = None
            self._last_event_id = ev_id
            self._seq += 1
            # trajectory 投影信封（flat-additive，落到读侧 SessionEvent.data）：
            # 关闭/FULL 时 payload 不变；SUMMARY 时丢重型 payload、补摘要+hash。
            if self.trajectory_enabled:
                event["trajectory"] = True
                event["trajectory_id"] = self.trajectory_id
                event["trajectory_level"] = self.trajectory_level
                if self.trajectory_level == "summary":
                    apply_summary_shaping(event)
        except Exception:
            return
        for sink in self.sinks:
            try:
                sink.write(event)
            except Exception:
                pass  # instrumentation 绝不影响 agent

    def child(self, session_id: str, agent_id: "str | None" = None) -> "Tracer":
        """[test/legacy 用途] 派生一个共享 sinks 的子 Tracer。

        注意：生产中的子 agent **不**走这里——它们经 ``Agent(...) -> _build_tracer`` 构造，
        那里用 ``next_seq_from_wire`` 算出 ``start_seq``（resume-safe）并取 ``agent_id=artifact_id``。
        本方法不接 ``start_seq``（恒从 seq 0 起，非 resume-safe），仅供测试/历史调用；新代码
        请走 ``_build_tracer`` 路径，勿用 ``child()`` 挂生产 wire，否则会重开 id 碰撞。
        """
        return Tracer(
            session_id,
            self.sinks,
            parent_session_id=self.session_id,
            agent_id=agent_id or session_id,
            branch_id=self.branch_id,
            trajectory_enabled=self.trajectory_enabled,
            trajectory_level=self.trajectory_level,
            trajectory_id=self.trajectory_id,
        )

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception:
                pass


class NullTracer:
    """关闭态：全 no-op，零分配、零 I/O，不创建任何文件。"""

    session_id = ""
    parent_session_id = None
    agent_id = "main"
    branch_id = "main"
    trajectory_enabled = False
    trajectory_level = "summary"
    trajectory_id = None

    def begin_turn(self, *args: Any, **kwargs: Any) -> str:
        return ""

    def emit(self, *args: Any, **kwargs: Any) -> None:
        pass

    def child(self, *args: Any, **kwargs: Any) -> "NullTracer":
        return self

    def close(self) -> None:
        pass


def make_tracer(session_id: str, *, enabled: bool, sinks: "list[Sink] | None" = None):
    if not enabled:
        return NullTracer()
    if sinks is None:
        from .config import build_default_sinks
        sinks = build_default_sinks(session_id)
    parent = os.environ.get("NANOCODE_TRACE_PARENT", "").strip() or None
    return Tracer(session_id, sinks, parent_session_id=parent)
