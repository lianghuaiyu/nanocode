"""SimpleMemEngine — nanocode-internal fork of SimpleMem (docs/20 §4/§6).

Public surface: `SimpleMemConfig`, `SimpleMemEngine`, `create_simplemem_engine`,
plus the typed models and errors. The upstream unified-factory / auto-routing
facade is gone, along with global settings, internal network/embedding clients,
and stdout — config and llm/embed callables are injected by the host.
"""
from .config import SimpleMemConfig
from .retrieval_config import RetrievalConfig, FUSION_MODES
from .engine import SimpleMemEngine, create_simplemem_engine
from .models import Dialogue, MemoryEntry, MemoryNote, MemoryPage
from .migrations import MigrationRequired, SCHEMA_VERSION
from .errors import EngineUnavailable

__all__ = [
    "SimpleMemConfig", "RetrievalConfig", "FUSION_MODES",
    "SimpleMemEngine", "create_simplemem_engine",
    "Dialogue", "MemoryEntry", "MemoryNote", "MemoryPage",
    "MigrationRequired", "SCHEMA_VERSION", "EngineUnavailable",
]
