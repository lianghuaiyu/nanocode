"""Security tests for `_load_dotenv`: a repo-local .env must not set nanocode's
own security-sensitive env vars (sandbox mode / microVM launcher path)."""
import os

import pytest

from nanocode.entrypoints import cli


def _clear_env(monkeypatch, *names):
    for n in names:
        monkeypatch.delenv(n, raising=False)


def test_is_blocked_dotenv_key_pure():
    # Blocked: NANOCODE_* prefix (any case) and MSB_BIN exact name.
    assert cli._is_blocked_dotenv_key("NANOCODE_FOO")
    assert cli._is_blocked_dotenv_key("NANOCODE_SHELL_SANDBOX")
    assert cli._is_blocked_dotenv_key("MSB_BIN")
    assert cli._is_blocked_dotenv_key("nanocode_x")  # lowercase
    assert cli._is_blocked_dotenv_key("msb_bin")
    # Not blocked: API config and unrelated vars.
    assert not cli._is_blocked_dotenv_key("OPENAI_API_KEY")
    assert not cli._is_blocked_dotenv_key("MSB")  # prefix of MSB_BIN, not exact
    assert not cli._is_blocked_dotenv_key("FOO")


def test_blocked_security_keys_not_loaded(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "NANOCODE_SHELL_SANDBOX=off\n"
        "NANOCODE_MSB_BIN=/bin/sh\n"
        "MSB_BIN=/bin/sh\n"
        "export NANOCODE_SHELL_SANDBOX=auto\n"
        "  nanocode_shell_sandbox = off \n"
    )
    _clear_env(
        monkeypatch,
        "NANOCODE_SHELL_SANDBOX",
        "NANOCODE_MSB_BIN",
        "MSB_BIN",
    )
    monkeypatch.chdir(tmp_path)
    cli._load_dotenv()
    assert "NANOCODE_SHELL_SANDBOX" not in os.environ
    assert "NANOCODE_MSB_BIN" not in os.environ
    assert "MSB_BIN" not in os.environ


def test_nonsecurity_keys_still_loaded(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "OPENAI_API_KEY=sk-x\n"
        "ANTHROPIC_API_KEY=y\n"
        "MY_VAR=z\n"
    )
    _clear_env(monkeypatch, "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MY_VAR")
    monkeypatch.chdir(tmp_path)
    cli._load_dotenv()
    assert os.environ.get("OPENAI_API_KEY") == "sk-x"
    assert os.environ.get("ANTHROPIC_API_KEY") == "y"
    assert os.environ.get("MY_VAR") == "z"


def test_existing_env_not_overwritten(tmp_path, monkeypatch):
    # The `key not in os.environ` guard runs before the block filter, so a
    # pre-existing (operator-set) value always wins.
    env = tmp_path / ".env"
    env.write_text("MY_VAR=from-dotenv\n")
    monkeypatch.setenv("MY_VAR", "preset")
    monkeypatch.chdir(tmp_path)
    cli._load_dotenv()
    assert os.environ.get("MY_VAR") == "preset"


# --- expanded blocklist (PATH / LD_* / DYLD_* / interpreter-injection) --------------------

_EXPANDED_BLOCKED = [
    "PATH",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "PYTHONPATH",
    "NODE_OPTIONS",
    "BASH_ENV",
    "IFS",
    "ENV",
]


def test_expanded_blocklist_is_blocked_pure():
    for name in _EXPANDED_BLOCKED:
        assert cli._is_blocked_dotenv_key(name), name
        assert cli._is_blocked_dotenv_key(name.lower()), name


def test_expanded_blocklist_not_loaded(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("".join(f"{n}=/evil/value\n" for n in _EXPANDED_BLOCKED))
    _clear_env(monkeypatch, *_EXPANDED_BLOCKED)
    monkeypatch.chdir(tmp_path)
    cli._load_dotenv()
    for n in _EXPANDED_BLOCKED:
        assert n not in os.environ, n


def test_lookalike_keys_not_overblocked():
    # Prefix/exact semantics, not substring — these must NOT be blocked.
    for name in ("MSB", "MSB_BINX", "XNANOCODE_FOO", "MY_PATH", "MYAPP_LD",
                 "MY_LD_FLAGS", "PATHEXT", "OLD_PATH"):
        assert not cli._is_blocked_dotenv_key(name), name


def test_lookalike_keys_still_loaded(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "DATABASE_URL=postgres://x\n"
        "OPENAI_API_KEY=sk-x\n"
        "MY_VAR=v\n"
        "MSB=ok\n"
        "MSB_BINX=ok\n"
        "XNANOCODE_FOO=ok\n"
        "MY_PATH=ok\n"
        "MYAPP_LD=ok\n"
    )
    keys = ["DATABASE_URL", "OPENAI_API_KEY", "MY_VAR", "MSB", "MSB_BINX",
            "XNANOCODE_FOO", "MY_PATH", "MYAPP_LD"]
    _clear_env(monkeypatch, *keys)
    monkeypatch.chdir(tmp_path)
    cli._load_dotenv()
    expected = {
        "DATABASE_URL": "postgres://x",
        "OPENAI_API_KEY": "sk-x",
        "MY_VAR": "v",
    }
    for k in keys:
        assert os.environ.get(k) == expected.get(k, "ok"), k


# --- trust-gating for repo ./.env (Codex shape) -------------------------------------------

def _write_user_env(tmp_path, body):
    user_env = tmp_path / "user.env"
    user_env.write_text(body)
    return str(user_env)


def test_repo_env_loaded_when_trusted(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("REPO_TRUSTED_KEY=from-repo\n")
    user_env = _write_user_env(tmp_path, "")
    _clear_env(monkeypatch, "REPO_TRUSTED_KEY")
    monkeypatch.setattr(cli, "_user_env_path", lambda: user_env)
    monkeypatch.setattr(cli, "is_trusted", lambda cwd: True, raising=False)
    monkeypatch.chdir(tmp_path)
    cli._load_env_files()
    assert os.environ.get("REPO_TRUSTED_KEY") == "from-repo"


def test_repo_env_not_loaded_when_untrusted(tmp_path, monkeypatch):
    # An untrusted repo's .env is never read — not even an innocuous OPENAI_API_KEY.
    (tmp_path / ".env").write_text(
        "REPO_UNTRUSTED_KEY=from-repo\n"
        "OPENAI_API_KEY=sk-from-untrusted-repo\n"
    )
    user_env = _write_user_env(tmp_path, "USER_LEVEL_KEY=from-user\n")
    _clear_env(monkeypatch, "REPO_UNTRUSTED_KEY", "OPENAI_API_KEY", "USER_LEVEL_KEY")
    monkeypatch.setattr(cli, "_user_env_path", lambda: user_env)
    monkeypatch.setattr(cli, "is_trusted", lambda cwd: False, raising=False)
    monkeypatch.chdir(tmp_path)
    cli._load_env_files()
    # Repo .env entirely ignored.
    assert "REPO_UNTRUSTED_KEY" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
    # User-level .env still loaded (trusted source).
    assert os.environ.get("USER_LEVEL_KEY") == "from-user"


def test_user_env_always_loaded_even_when_untrusted(tmp_path, monkeypatch):
    user_env = _write_user_env(tmp_path, "USER_ONLY_KEY=u\n")
    _clear_env(monkeypatch, "USER_ONLY_KEY")
    monkeypatch.setattr(cli, "_user_env_path", lambda: user_env)
    monkeypatch.setattr(cli, "is_trusted", lambda cwd: False, raising=False)
    monkeypatch.chdir(tmp_path)
    cli._load_env_files()
    assert os.environ.get("USER_ONLY_KEY") == "u"


def test_user_env_blocklist_enforced(tmp_path, monkeypatch):
    # Even the trusted user-level .env may not set security/injection vars.
    user_env = _write_user_env(
        tmp_path,
        "NANOCODE_SHELL_SANDBOX=off\n"
        "PATH=/evil\n"
        "USER_SAFE_KEY=ok\n"
    )
    _clear_env(monkeypatch, "NANOCODE_SHELL_SANDBOX", "USER_SAFE_KEY")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    orig_path = os.environ.get("PATH")
    monkeypatch.setattr(cli, "_user_env_path", lambda: user_env)
    monkeypatch.setattr(cli, "is_trusted", lambda cwd: False, raising=False)
    monkeypatch.chdir(tmp_path)
    cli._load_env_files()
    assert "NANOCODE_SHELL_SANDBOX" not in os.environ
    assert os.environ.get("PATH") == orig_path  # not clobbered to /evil
    assert os.environ.get("USER_SAFE_KEY") == "ok"
