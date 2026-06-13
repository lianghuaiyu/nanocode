"""RPC / headless 模式（docs/17 Phase 5b）——TUI 客户端化的「验收试金石」。

证明 agent core 已与表现层彻底解耦：用 JSON-lines over stdio 驱动**同一个** RuntimeThread/
AgentSession，core 一行不改。这是 pi `modes/rpc` 的对位实现。

协议（行分隔 JSON）：
  stdin  命令：
    {"cmd": "prompt", "text": "..."}                 → 跑一个 turn（异步,不阻塞 stdin 读取）
    {"cmd": "cancel"}                                → thread.cancel()（abort 当前 turn）
    {"cmd": "get_state"}                             → 回 {"type":"state","state":{...messages...}}（Pi get_state）
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
    # 待审批 future（按 request_id 关联）。审批在 agent 循环内**串行**（turn 挂起等 confirm_fn），
    # 同一时刻至多一个待审批；用 dict 仍便于按 id 精确应答 + FIFO 兜底。
    pending: "dict[str, asyncio.Future]" = {}
    approval_seq = [0]

    async def confirm_fn(message: str) -> bool:
        # codex P1：confirm_fn **自带**审批请求往返——不依赖 ApprovalRequested 事件经订阅流到达
        # 客户端。这点对前台子 agent 至关重要：子 agent 是独立 Agent，其事件发往子的 _event_subscribers
        # （不连父 RuntimeThread 流），但它**继承父 confirm_fn**，故经此处自带的 approval_request 行
        # 仍能让客户端看到并应答（否则危险子 agent 动作会让 turn 永久挂起）。
        approval_seq[0] += 1
        rid = f"appr-{approval_seq[0]}"
        fut = loop.create_future()
        pending[rid] = fut
        _emit_line({"type": "approval_request", "request_id": rid, "message": message})
        try:
            return await fut
        finally:
            pending.pop(rid, None)

    rt = AgentRuntime()
    thread = rt.adopt(agent, approvals=ApprovalManager(confirm_fn=confirm_fn), lease=lease)
    # 订阅事件流 → 逐条 JSON line 到 stdout（这是 core→client 的全部表现通道）。
    thread.subscribe(lambda env: _emit_line({**env, "event": _serialize_event(env["event"])}))

    async def _run_turn(text: str) -> None:
        # codex P2：detached task——必须自己兜住异常并 emit 结构化结果，否则 thread.run() 抛错时
        # 任务静默死亡、客户端永远等不到协议承诺的 turn_result。
        try:
            res = await thread.run(text)
            _emit_line({"type": "turn_result", "status": res.status,
                        "final_response": res.final_response,
                        "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                        "error": res.error})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _emit_line({"type": "turn_result", "status": "error", "final_response": "",
                        "input_tokens": 0, "output_tokens": 0, "error": str(e)})

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
            # codex P1：turn 串行——已有 turn 在跑时拒绝新 prompt（否则两 turn 共享同一 Agent 的
            # _current_task / token 计数 / _final_text_chunks / pending 审批，必然串台/丢状态）。
            if turn_task is not None and not turn_task.done():
                _emit_line({"type": "error", "message": "a turn is already running; wait for turn_result"})
            else:
                turn_task = asyncio.create_task(_run_turn(cmd.get("text", "")))
        elif kind == "cancel":
            thread.cancel()
        elif kind == "get_state":
            # Pi get_state 对位：完整会话快照（status + messages）。
            _emit_line({"type": "state", "state": thread.state()})
        elif kind == "approval_response":
            # 按 request_id 精确应答；缺 id 则 FIFO 解决最早一个待审批（串行下唯一）。
            rid = cmd.get("request_id")
            fut = pending.get(rid) if rid is not None else next(iter(pending.values()), None)
            if fut is not None and not fut.done():
                fut.set_result(bool(cmd.get("approved", False)))
        elif kind == "exit":
            break
        else:
            _emit_line({"type": "error", "message": f"unknown cmd: {kind!r}"})

    if turn_task is not None and not turn_task.done():
        turn_task.cancel()
