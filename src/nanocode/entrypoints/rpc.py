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
import json
import sys

from ..runtime import RuntimeApprovalBroker
from .host import RuntimeHost


def _emit_line(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, default=str, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _response(command: str, *, success: bool = True, data=None, error: str | None = None,
              id_: str | None = None) -> dict:
    out = {"type": "response", "command": command, "success": success}
    if id_ is not None:
        out["id"] = id_
    if data is not None:
        out["data"] = data
    if error is not None:
        out["error"] = error
    return out


def _user_message_text(entry) -> str:
    msg = (entry.data or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


async def run_rpc_mode(host: RuntimeHost) -> None:
    """Run RPC against a runtime host."""
    if not isinstance(host, RuntimeHost):
        raise TypeError("run_rpc_mode requires a RuntimeHost")
    thread = host.current_thread
    loop = asyncio.get_running_loop()

    broker = RuntimeApprovalBroker(emit=_emit_line)
    thread.attach_approvals(confirm_fn=broker.confirm)
    # 订阅事件流 → 逐条 JSON line 到 stdout。RuntimeThread 已保证 event 是 JSON-able。
    unsubscribe = thread.subscribe(_emit_line)

    async def _rebind_current_thread() -> None:
        nonlocal thread, unsubscribe
        unsubscribe()
        thread = host.current_thread
        thread.attach_approvals(confirm_fn=broker.confirm)
        unsubscribe = thread.subscribe(_emit_line)

    async def _run_turn(text: str) -> None:
        # codex P2：detached task——必须自己兜住异常并 emit 结构化结果，否则 thread.run() 抛错时
        # 任务静默死亡、客户端永远等不到协议承诺的 turn_result。
        try:
            res = await host.current_thread.run(text)
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

        kind = cmd.get("cmd") or cmd.get("type")
        cid = cmd.get("id")
        if kind == "prompt":
            # codex P1：turn 串行——已有 turn 在跑时拒绝新 prompt（否则两 turn 共享同一 Agent 的
            # _current_task / token 计数 / _final_text_chunks / pending 审批，必然串台/丢状态）。
            if turn_task is not None and not turn_task.done():
                _emit_line(_response("prompt", success=False,
                                     error="a turn is already running; wait for turn_result", id_=cid))
            else:
                turn_task = asyncio.create_task(_run_turn(cmd.get("text", "")))
        elif kind == "cancel":
            host.current_thread.cancel()
        elif kind == "get_state":
            # Pi get_state 对位：完整会话快照（status + messages）。
            _emit_line(_response("get_state", data=host.current_thread.state(), id_=cid))
        elif kind == "get_messages":
            _emit_line(_response("get_messages", data={"messages": host.current_thread.messages()}, id_=cid))
        elif kind == "get_session_stats":
            _emit_line(_response("get_session_stats", data=host.current_thread.session_stats(), id_=cid))
        elif kind == "set_session_name":
            try:
                host.current_thread.set_session_name((cmd.get("name") or "").strip())
                _emit_line(_response("set_session_name", id_=cid))
            except Exception as e:
                _emit_line(_response("set_session_name", success=False, error=str(e), id_=cid))
        elif kind == "compact":
            try:
                await host.current_thread.compact()
                _emit_line(_response("compact", id_=cid))
            except Exception as e:
                _emit_line(_response("compact", success=False, error=str(e), id_=cid))
        elif kind == "new_session":
            ok, reason = host.can_switch()
            if not ok:
                _emit_line(_response("new_session", success=False,
                                     error=f"cannot switch sessions right now: {reason}", id_=cid))
            else:
                host.runtime.thread_new(host)
                await _rebind_current_thread()
                _emit_line(_response("new_session", id_=cid))
        elif kind == "resume":
            ok, reason = host.can_switch()
            if not ok:
                _emit_line(_response("resume", success=False,
                                     error=f"cannot switch sessions right now: {reason}", id_=cid))
            else:
                sid = cmd.get("session_id") or cmd.get("sessionId")
                if not sid or host.runtime.thread_resume(host, sid) is None:
                    _emit_line(_response("resume", success=False,
                                         error=f"cannot resume {sid!r}", id_=cid))
                else:
                    await _rebind_current_thread()
                    _emit_line(_response("resume", id_=cid))
        elif kind == "fork":
            ok, reason = host.can_switch()
            if not ok:
                _emit_line(_response("fork", success=False,
                                     error=f"cannot switch sessions right now: {reason}", id_=cid))
            else:
                entry_id = cmd.get("entry_id") or cmd.get("entryId")
                selected_text = ""
                view = host.current_thread.readonly_session()
                if view is not None:
                    entry = next((e for e in view.entries() if e.id == entry_id), None)
                    if entry is not None:
                        selected_text = _user_message_text(entry)
                if not entry_id or host.runtime.thread_fork(host, host.current_thread.session_id, entry_id) is None:
                    _emit_line(_response("fork", success=False, error="fork failed", id_=cid))
                else:
                    await _rebind_current_thread()
                    _emit_line(_response("fork", data={"text": selected_text}, id_=cid))
        elif kind == "clone":
            ok, reason = host.can_switch()
            if not ok:
                _emit_line(_response("clone", success=False,
                                     error=f"cannot switch sessions right now: {reason}", id_=cid))
            elif host.runtime.thread_clone(host, host.current_thread.session_id) is None:
                _emit_line(_response("clone", success=False, error="clone failed", id_=cid))
            else:
                await _rebind_current_thread()
                _emit_line(_response("clone", id_=cid))
        elif kind == "shell":
            result = await host.current_thread.execute_user_shell(cmd.get("command", ""))
            _emit_line(_response("shell", data={"output": result}, id_=cid))
        elif kind == "approval_response":
            # 按 request_id 精确应答；缺 id 则 FIFO 解决最早一个待审批（串行下唯一）。
            broker.resolve(cmd.get("request_id"), bool(cmd.get("approved", False)))
        elif kind == "exit":
            break
        else:
            _emit_line(_response(str(kind), success=False, error=f"unknown cmd: {kind!r}", id_=cid))

    if turn_task is not None and not turn_task.done():
        turn_task.cancel()
    unsubscribe()
