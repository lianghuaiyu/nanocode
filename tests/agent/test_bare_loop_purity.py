"""docs/26 G1：②a 裸循环纯净性的**导入闭包**边界（把 four-layer 审计里 "arguably 正确" 锁成不变量）。

四层手机比喻里 ②a=裸模型循环（`agent/core.py` + `agent/loop.py`）。G1 单向依赖要求：②a 绝不
依赖 ②b(harness: session/、agent/state.py) / ③(host services: capabilities/runtime/tools/mcp/
runs/tasks) / ④(extensions)。历史存疑点是 `agent/state.py`(AgentState) import 了 session/——
但事实是**裸循环从不 import state.py**（`AgentState` 只被 ②b `session/agent.py` 消费），故
state.py 是 ②b 投影、不在 ②a 的导入闭包里。本测试在**干净解释器子进程**里证明这一点：
`import core+loop` 的**传递闭包**不含任一被禁前缀（含 state.py 自身），比 AST 直连扫描更强
（直连越界边会体现为闭包里的泄漏模块；TYPE_CHECKING 型 import 不进 sys.modules 不误报）。

subprocess 隔离：全量 pytest 早已把这些模块加载进 `sys.modules`，故必须新解释器测 import 闭包。
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

# ②a 裸循环绝不该在 import 闭包里出现的层（②b 投影 + ③ 宿主服务 + ④ 扩展）。
_FORBIDDEN_PREFIXES = (
    "nanocode.session",
    "nanocode.agent.state",
    "nanocode.capabilities",
    "nanocode.runtime",
    "nanocode.tools",
    "nanocode.mcp",
    "nanocode.runs",
    "nanocode.tasks",
    "nanocode.extensions",
)


def test_bare_loop_import_closure_is_pure():
    """`import nanocode.agent.core` + `nanocode.agent.loop` 的传递闭包不得含 ②b/③/④。"""
    src = Path(__file__).resolve().parents[2] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    code = textwrap.dedent(
        """
        import sys
        import nanocode.agent.core
        import nanocode.agent.loop
        forbidden = %r
        leaked = sorted(
            n for n in sys.modules
            if any(n == p or n.startswith(p + ".") for p in forbidden)
        )
        assert not leaked, "bare loop leaked layer-2b/3/4 imports: " + repr(leaked)
        """
        % (_FORBIDDEN_PREFIXES,)
    )
    proc = subprocess.run([sys.executable, "-c", code], env=env, text=True,
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr
