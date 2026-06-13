"""REPL SIGINT 决策(cli._sigint_decision):turn 中 Ctrl-C 打断,空闲按两次退出。"""

from __future__ import annotations

from nanocode.entrypoints.cli import _sigint_decision


def test_sigint_during_turn_aborts_and_resets_count():
    # 正在 thinking/streaming → 打断当前 turn,计数清零(不向退出累积)
    action, n = _sigint_decision(is_processing=True, sigint_count=1)
    assert action == "abort" and n == 0
    # 即便此前已按过一次,turn 中再按仍是 abort、不退出
    action, n = _sigint_decision(is_processing=True, sigint_count=5)
    assert action == "abort" and n == 0


def test_sigint_idle_first_press_warns():
    action, n = _sigint_decision(is_processing=False, sigint_count=0)
    assert action == "warn" and n == 1


def test_sigint_idle_second_press_exits():
    action, n = _sigint_decision(is_processing=False, sigint_count=1)
    assert action == "exit" and n == 2


def test_abort_after_warn_does_not_exit():
    # 空闲按一次(warn,count=1),随后 turn 开始再按 → abort,不会因 count=2 误退出
    _, n = _sigint_decision(False, 0)
    assert n == 1
    action, n = _sigint_decision(True, n)
    assert action == "abort" and n == 0
