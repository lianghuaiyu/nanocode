from nanocode.entrypoints.commands.builtin import handle_eval_command


def test_handle_eval_command_does_not_handle_generate():
    # generate 不属于纯函数职责 → Usage（路由由 REPL 主循环负责）
    out = handle_eval_command("generate")
    assert "Usage" in out


def test_handle_eval_command_does_not_handle_optimize():
    # optimize 是独立顶层命令 /memory optimize，不经 handle_eval_command → Usage
    out = handle_eval_command("optimize")
    assert "Usage" in out
