"""verbosity 开关：默认静默 print_cost，verbose 才打印。"""
from nanocode import tui as ui


def test_default_quiet_no_cost(capsys):
    ui.set_verbose(False)
    ui.print_cost(12991, 117)
    out = capsys.readouterr().out
    assert "Tokens" not in out
    assert ui.is_verbose() is False


def test_verbose_prints_cost(capsys):
    try:
        ui.set_verbose(True)
        ui.print_cost(12991, 117)
        out = capsys.readouterr().out
        assert "Tokens" in out and "12991" in out
    finally:
        ui.set_verbose(False)
