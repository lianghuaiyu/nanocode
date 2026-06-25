"""Memory boundary invariants (docs/20 §2.1 / §9.1).

These are source-level guards: the embedded-agent boundary must hold structurally,
not just at runtime.
"""
import re
from pathlib import Path

import nanocode

PKG = Path(nanocode.__file__).parent
ENGINE_DIR = PKG / "memory" / "engines" / "simplemem"


def _read(p: Path) -> str:
    return p.read_text()


def test_agent_core_does_not_import_memory():
    src = _read(PKG / "agent" / "core.py")
    assert "nanocode.memory" not in src
    assert not re.search(r"from \.\.memory|import .*\bmemory\b", src)


def test_memory_tool_is_schema_only():
    src = _read(PKG / "tools" / "memory_tool.py")
    assert "def run(" not in src
    # no imports of the store/backend/engine — schema + prose only
    assert not re.search(r"^\s*(from|import)\b.*\b(store|backend|simplemem|engines)\b",
                         src, re.M | re.I)


def test_simplemem_engine_does_not_import_host_layers():
    for f in ENGINE_DIR.glob("*.py"):
        src = _read(f)
        assert "nanocode.runtime" not in src, f
        assert "nanocode.session" not in src, f
        assert "nanocode.capabilities" not in src, f
        assert "nanocode.agent" not in src, f


def test_simplemem_engine_has_no_env_or_network_or_stdout():
    for f in ENGINE_DIR.glob("*.py"):
        src = _read(f)
        assert "import openai" not in src and "from openai" not in src, f
        assert "sentence_transformers" not in src, f
        assert "os.getenv" not in src and "os.environ" not in src, f
        assert re.search(r"^import config|^\s*import config\b", src, re.M) is None, f
        # the engine logs via logging, never prints
        assert not re.search(r"(?<!\.)\bprint\(", src), f


def test_runtime_services_constructs_memory_service():
    src = _read(PKG / "runtime" / "facade.py")
    assert "MemoryService(" in src and "memory_service" in src


def test_no_vendored_simplemem_imports():
    forbidden = "_" + "vendor.simplemem"
    for f in PKG.rglob("*.py"):
        assert forbidden not in _read(f), f


# ── import-graph boundaries (subprocess: in-process sys.modules is polluted) ──
def _fresh_import_check(stmt: str) -> str:
    import subprocess
    import sys
    return subprocess.run([sys.executable, "-c", stmt], capture_output=True,
                          text=True).stdout.strip()


def test_agentcore_import_does_not_load_memory():
    out = _fresh_import_check(
        "import sys; import nanocode.agent.core as _; "
        "print('nanocode.memory' in sys.modules)")
    assert out == "False"


def test_simplemem_engine_import_does_not_load_session():
    out = _fresh_import_check(
        "import sys; import nanocode.memory.engines.simplemem as _; "
        "print(any(m.startswith(('nanocode.session','nanocode.runtime','nanocode.capabilities')) "
        "for m in sys.modules))")
    assert out == "False"
