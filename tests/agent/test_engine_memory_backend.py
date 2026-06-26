from nanocode.agent import Agent
from nanocode.memory.service import MemoryService, MemoryServiceConfig


def _agent(**kw):
    return Agent(api_key="x", model="claude-opus-4-6", **kw)


def _off_service():
    return MemoryService(config=MemoryServiceConfig(backend="off"), cwd=".", agent_dir=".")


def test_agent_holds_memory_service():
    svc = _off_service()
    a = _agent(memory_service=svc)
    assert a._memory_service is svc


def test_agent_default_memory_service_none():
    a = _agent()
    assert a._memory_service is None
