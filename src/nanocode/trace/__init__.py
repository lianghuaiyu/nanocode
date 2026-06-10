"""nanocode trace：低侵入的 agent 轨迹记录（默认关闭）。"""
from .sinks import Sink, JsonlSink
from .tracer import Tracer, NullTracer, make_tracer, SCHEMA_VERSION
from .config import (
    is_enabled, trace_dir, trace_file, build_default_sinks,
    trajectory_enabled, trajectory_level,
)

__all__ = [
    "Sink", "JsonlSink", "Tracer", "NullTracer", "make_tracer", "SCHEMA_VERSION",
    "is_enabled", "trace_dir", "trace_file", "build_default_sinks",
    "trajectory_enabled", "trajectory_level",
]
