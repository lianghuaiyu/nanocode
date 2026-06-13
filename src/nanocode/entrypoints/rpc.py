"""RPC / headless 模式（docs/17 Phase 5b）——TUI 客户端化的「验收试金石」。

证明 agent core 已与表现层彻底解耦：用 JSON-lines over stdio 驱动**同一个** RuntimeThread/
AgentSession，core 一行不改。这是 pi `modes/rpc` 的对位实现。

协议（行分隔 JSON）：
  stdin  命令：
    {"cmd": "prompt", "text": "..."}                 → 跑一个 turn（异步,不阻塞 stdin 读取）
    {"cmd": "cancel"}                                → thread.cancel()（abort 当前 turn）
    {"cmd": "approval_response", "approved": bool}   → 应答待审批（FIFO；turn 内审批串行）
    {"cmd": "exit"}                                  → 退出
  stdout 输出：
    每条 AgentEvent 信封 {thread_id, session_id, seq, type, event}（event 为 dataclass→dict）
    {"type": "turn_result", "status": ..., "final_response": ..., "input_tokens", "output_tokens"}

审批往返：core 的 confirm_fn 是 async——turn 自然挂起等 confirm_fn。本模式的 confirm_fn 设一个
pending future，stdin 的 approval_response 解决它（FIFO 即可：agent 循环串行，同一时刻至多一个
待审批）。ApprovalRequested 事件已随订阅流出 stdout，携带 request_id 供外部客户端回显。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys

from ..agent import AgentRuntime, ApprovalManager


def _serialize_event(event) -> object:
    """typed AgentEvent（frozen dataclass）→ JSON-able dict；边界事件已是 plain dict。"""
    if dataclasses.is_dataclass(event) and not isinstance(event, type):
        return dataclasses.asdict(event)
    return event


def _emit_line(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, default=str, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def run_rpc_mode(agent, lease=None) -> None:
    """在 agent 上跑 RPC 事件循环（stdin 命令 ↔ stdout 事件流）。"""
    loop = asyncio.get_event_loop()
    pending_approval: dict = {"fut": None}

    async def confirm_fn(message: str) -> bool:
        # turn 在此自然挂起；stdin 的 approval_response 解决 future（FIFO，串行审批）。
        fut = loop.create_future()
        pending_approval["fut"] = fut
        return await fut

    rt = AgentRuntime()
    thread = rt.adopt(agent, approvals=ApprovalManager(confirm_fn=confirm_fn), lease=lease)
    # 订阅事件流 → 逐条 JSON line 到 stdout（这是 core→client 的全部表现通道）。
    thread.subscribe(lambda env: _emit_line({**env, "event": _serialize_event(env["event"])}))

    async def _run_turn(text: str) -> None:
        res = await thread.run(text)
        _emit_line({"type": "turn_result", "status": res.status,
                    "final_response": res.final_response,
                    "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                    "error": res.error})

    turn_task: "asyncio.Task | None" = None
    while True:
        # 阻塞读 stdin 放到线程池，避免阻塞事件循环（confirm future / turn task 仍可推进）。
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break                                   # EOF
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except Exception as e:
            _emit_line({"type": "error", "message": f"bad json: {e}"})
            continue

        kind = cmd.get("cmd")
        if kind == "prompt":
            # 不 await——turn 作为独立 task 跑,stdin 继续读（以便接收 approval_response / cancel）。
            turn_task = asyncio.create_task(_run_turn(cmd.get("text", "")))
        elif kind == "cancel":
            thread.cancel()
        elif kind == "approval_response":
            fut = pending_approval["fut"]
            if fut is not None and not fut.done():
                fut.set_result(bool(cmd.get("approved", False)))
        elif kind == "exit":
            break
        else:
            _emit_line({"type": "error", "message": f"unknown cmd: {kind!r}"})

    if turn_task is not None and not turn_task.done():
        turn_task.cancel()
