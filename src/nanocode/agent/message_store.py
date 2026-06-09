"""MessageStore：provider 消息列表的单一 owner（P-1 子目标2）。

集中 load / replace / append / dump 四个生命周期入口。Agent 经 `_openai_messages` /
`_anthropic_messages` 属性访问活动列表——getter 返回 live list（供读/索引/切片/in-place
裁剪/append，热路径零改动），整列**赋值**（resume / compaction 摘要替换 / clear）经 setter
路由到 owner。父 agent **不得**直接赋值子 agent 的列表——经子的 `_load_messages`（见 engine）。

设计意图：把原先散落在 engine + 两 backend + plan_mode 的直接列表读写收敛到单一 owner，
为后续 AgentSession 抽层铺路；本步**不改变任何行为**（store 持有的就是原来的那个 list 对象，
load/replace 接管外部引用、保持旧 by-ref 语义）。
"""

from __future__ import annotations


class MessageStore:
    """单一 owner：持有一个 provider 消息列表，集中 load/replace/append/dump。"""

    def __init__(self) -> None:
        self._items: list = []

    @property
    def items(self) -> list:
        """live 列表引用——读/索引/切片/in-place 裁剪/append 用（owner 暴露其所有物）。"""
        return self._items

    def load(self, messages: list) -> None:
        """load：用快照 / history 接管列表（resume / restore；保持旧 by-ref 语义）。"""
        self._items = messages

    def replace(self, messages: list) -> None:
        """replace：整列重置（compaction 摘要替换 / clear）。机制同 load，名字区分意图。"""
        self._items = messages

    def append(self, message) -> None:
        self._items.append(message)

    def dump(self) -> list:
        """dump：导出列表（持久化只读）。"""
        return self._items
