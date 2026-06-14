import sys
import pytest
from nanocode.entrypoints import cli


def test_parse_memory_backend_default(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["nanocode", "hi"])
    args = cli.parse_args()
    assert args.memory_backend is None


def test_parse_memory_backend_explicit(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["nanocode", "--memory-backend", "off", "hi"])
    args = cli.parse_args()
    assert args.memory_backend == "off"


def test_parse_memory_backend_rejects_invalid(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["nanocode", "--memory-backend", "bogus"])
    with pytest.raises(SystemExit):
        cli.parse_args()


def test_parse_repo_map_flags(monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "nanocode",
        "--map-tokens", "0",
        "--map-refresh", "files",
        "--map-multiplier-no-files", "4",
        "hi",
    ])
    args = cli.parse_args()
    assert args.map_tokens == 0
    assert args.map_refresh == "files"
    assert args.map_multiplier_no_files == 4
