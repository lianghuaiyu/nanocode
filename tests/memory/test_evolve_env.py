from nanocode.memory.maintenance import evolve_min_confirmed, evolve_max_rounds


def test_defaults(monkeypatch):
    # 人工最终决策覆盖 #1：confirmed 阈值默认 = 5（非 10）。
    monkeypatch.delenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", raising=False)
    monkeypatch.delenv("NANOCODE_MEMORY_EVOLVE_MAX_ROUNDS", raising=False)
    assert evolve_min_confirmed() == 5
    assert evolve_max_rounds() == 7


def test_env_override(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "3")
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MAX_ROUNDS", "2")
    assert evolve_min_confirmed() == 3
    assert evolve_max_rounds() == 2


def test_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "0")
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MAX_ROUNDS", "garbage")
    assert evolve_min_confirmed() == 5    # <=0 回退默认
    assert evolve_max_rounds() == 7       # 非数字回退默认


def test_exported():
    from nanocode.memory import evolve_min_confirmed as a, evolve_max_rounds as b
    assert a is evolve_min_confirmed and b is evolve_max_rounds
