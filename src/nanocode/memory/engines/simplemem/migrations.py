"""Schema versioning + storage-scope guards (docs/20 §6.6).

The vector store root must live under the nanocode data root; arbitrary
absolute paths, `..` traversal, and symlinked roots are rejected. A schema
version mismatch raises `MigrationRequired` — never a silent rebuild.
"""
from __future__ import annotations

from pathlib import Path

from .errors import SimpleMemError

SCHEMA_VERSION = 1
_SCHEMA_FILE = "schema.json"


class MigrationRequired(SimpleMemError):
    """The on-disk index schema does not match the engine's SCHEMA_VERSION."""


def resolve_scoped_root(root: str, *, data_root: str) -> Path:
    """Validate and resolve a SimpleMem store root.

    Rules (docs/20 §6.6 acceptance):
    - The resolved path must be inside `data_root`.
    - No `..` traversal escaping `data_root`.
    - No symlink among the path components *strictly under* `data_root` (blocks
      symlink-root traversal even when the target resolves back inside the root).
      `data_root`'s own ancestors are trusted (e.g. macOS /tmp -> /private/tmp)
      and are not inspected.
    """
    data_literal = Path(data_root)
    data = data_literal.resolve()
    raw = Path(root)
    # Reject `..` components outright before resolution.
    if any(part == ".." for part in raw.parts):
        raise SimpleMemError(f"memory root must not contain '..': {root!r}")
    candidate = raw if raw.is_absolute() else (data_literal / raw)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(data)
    except ValueError:
        raise SimpleMemError(f"memory root {resolved} escapes data root {data}")
    # Symlink check on the UNRESOLVED tail under data_root. is_symlink() on a
    # not-yet-created component is False, so only existing components matter.
    # Try the tail relative to BOTH the literal and resolved data root so a root
    # spelled through data_root's own resolved alias (e.g. macOS /tmp ->
    # /private/tmp) can't skip the walk.
    tail = None
    base = data_literal
    for candidate_base in (data_literal, data):
        try:
            tail = candidate.relative_to(candidate_base)
            base = candidate_base
            break
        except ValueError:
            continue
    if tail is not None:
        cur = base
        for part in tail.parts:
            cur = cur / part
            if cur.is_symlink():
                raise SimpleMemError(f"memory root path component is a symlink: {cur}")
    return resolved


def schema_path(root: Path) -> Path:
    return root / _SCHEMA_FILE


# Sentinels for the on-disk schema marker state.
_MISSING = "missing"
_CORRUPT = "corrupt"


def read_schema_state(root: Path):
    """Return ("version", int) | "missing" | "corrupt" for the schema marker."""
    p = schema_path(root)
    if not p.exists():
        return _MISSING
    import json
    try:
        v = int(json.loads(p.read_text()).get("schema_version"))
        return ("version", v)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return _CORRUPT


def read_schema_version(root: Path) -> "int | None":
    state = read_schema_state(root)
    return state[1] if isinstance(state, tuple) else None


def write_schema_version(root: Path) -> None:
    import json
    root.mkdir(parents=True, exist_ok=True)
    schema_path(root).write_text(json.dumps({"schema_version": SCHEMA_VERSION}))


def _root_has_store_data(root: Path) -> bool:
    """Whether the root already holds index data (anything but the schema marker)."""
    if not root.exists():
        return False
    return any(child.name != _SCHEMA_FILE for child in root.iterdir())


def ensure_schema(root: Path) -> None:
    """Create the schema marker on a fresh root; raise MigrationRequired otherwise.

    - missing marker + empty root  -> new store: write marker.
    - missing marker + existing data -> unversioned legacy store: MigrationRequired.
    - corrupt marker                 -> MigrationRequired.
    - version mismatch               -> MigrationRequired.
    No path silently rebuilds or adopts an unversioned/corrupt store (docs/20 §2.4 #4)."""
    state = read_schema_state(root)
    if state == _MISSING:
        if _root_has_store_data(root):
            raise MigrationRequired(
                f"SimpleMem store at {root} has no schema marker but contains data; "
                f"migration required (no automatic adoption)")
        write_schema_version(root)
        return
    if state == _CORRUPT:
        raise MigrationRequired(
            f"SimpleMem schema marker at {root} is unreadable/corrupt; migration required")
    found = state[1]
    if found != SCHEMA_VERSION:
        raise MigrationRequired(
            f"SimpleMem index at {root} has schema v{found}, engine expects "
            f"v{SCHEMA_VERSION}; migration required (no automatic rebuild)")
