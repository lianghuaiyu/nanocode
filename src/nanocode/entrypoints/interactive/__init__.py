"""entrypoints/interactive — Pi 式交互呈现层（Layer 7 client）。

纯逻辑（footer / treemodel / sessionmodel）与 prompt_toolkit 渲染外壳（selector）分离:
纯逻辑可脱离 Application 单测;外壳只在 TTY 接管终端,非 TTY 由调用方走文本回退。
本层只消费 runtime（经 Control）+ SessionManager 只读 + 既有 append,不碰 agent/session core。
"""
