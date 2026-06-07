# src/nanocode/tools/sandbox_defaults.py
"""会话级 sandbox 默认值：模块级状态 + getter/setter。
由 cli.py 的 /sandbox 命令读写；sandbox_shell.run 调用时若未显式传参则取这里的默认。
仿 registry._activated_tools 的模块级状态风格。重启进程即回到内置默认。"""

from __future__ import annotations

# 内置默认 = 最严
_BUILTIN = {"persist": False, "network": "none", "mount_workspace": False, "deps": "reuse", "trace": False}
_NETWORK_VALUES = {"none", "public"}
_DEPS_VALUES = {"none", "reuse", "install"}
_BOOL_KEYS = {"persist", "mount_workspace", "trace"}

_state: dict = dict(_BUILTIN)


def reset_defaults() -> None:
    global _state
    _state = dict(_BUILTIN)


def get_defaults() -> dict:
    return dict(_state)


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"on", "true", "yes", "1"}:
        return True
    if s in {"off", "false", "no", "0"}:
        return False
    raise ValueError("value must be on/off")


def set_default(key: str, value) -> object:
    if key not in _BUILTIN:
        raise ValueError(f"unknown sandbox setting: {key} (valid: {', '.join(sorted(_BUILTIN))})")
    if key in _BOOL_KEYS:
        parsed = _parse_bool(value)
    elif key == "network":
        parsed = str(value).strip().lower()
        if parsed not in _NETWORK_VALUES:
            raise ValueError(f"network must be one of: {', '.join(sorted(_NETWORK_VALUES))}")
    elif key == "deps":
        parsed = str(value).strip().lower()
        if parsed not in _DEPS_VALUES:
            raise ValueError(f"deps must be one of: {', '.join(sorted(_DEPS_VALUES))}")
    else:
        parsed = value
    _state[key] = parsed
    return parsed
