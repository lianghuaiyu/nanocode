"""docs/15 Phase 0：ContextPack 结构化封装契约（§8.1）。"""

from nanocode.context.packs import ContextPack, estimate_tokens


def test_estimate_tokens_str_and_blocks():
    assert estimate_tokens("a" * 40) == 10           # ~4 chars/token
    assert estimate_tokens([{"type": "text", "text": "x" * 20},
                            {"type": "image", "data": "..."}]) == 5  # 只计 text
    assert estimate_tokens("") == 1                   # 下限 1
    assert estimate_tokens([]) == 1


def test_pack_autofills_token_estimate():
    p = ContextPack(id="g1", kind="git", content="branch: main")
    assert p.token_estimate == estimate_tokens("branch: main")


def test_pack_as_text_from_blocks():
    p = ContextPack(id="m1", kind="memory",
                    content=[{"type": "text", "text": "remember "}, {"type": "text", "text": "this"}])
    assert p.as_text() == "remember this"


def test_pack_to_custom_message_shape():
    p = ContextPack(id="s1", kind="skill_listing", content="- skill A", persist_policy="custom_message")
    cm = p.to_custom_message()
    assert cm == {"customType": "skill_listing", "content": "- skill A", "display": False}
    assert p.to_custom_message(display=True)["display"] is True


def test_pack_defaults():
    p = ContextPack(id="x", kind="k", content="c")
    assert p.lifecycle == "turn"
    assert p.cache_policy == "volatile_tail"
    assert p.persist_policy == "custom_message"
    assert p.priority == 0
    assert p.provenance == {}
