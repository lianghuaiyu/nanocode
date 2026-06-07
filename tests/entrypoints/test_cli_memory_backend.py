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
