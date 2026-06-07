from nanocode.agent import Agent
from nanocode.memory.backend import OffMemoryBackend


def _agent(**kw):
    return Agent(api_key="x", model="claude-opus-4-6", **kw)


def test_agent_holds_memory_backend():
    b = OffMemoryBackend()
    a = _agent(memory_backend=b)
    assert a._memory_backend is b


def test_agent_default_memory_backend_none():
    a = _agent()
    assert a._memory_backend is None
