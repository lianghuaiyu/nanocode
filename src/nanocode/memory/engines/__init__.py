"""nanocode-owned memory engines.

Currently the only engine is `simplemem` — a hard fork of the upstream
SimpleMem text pipeline, owned by nanocode (docs/20). It is a pure
memory algorithm/index implementation: it does not import runtime, session,
or capability code, never constructs network clients, and never reads env.
"""
