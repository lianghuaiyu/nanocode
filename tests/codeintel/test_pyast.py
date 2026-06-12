"""codeintel/pyast：Python stdlib-AST defs/refs 抽取（aider tree-sitter-python tags 的等价物）。"""

from nanocode.codeintel import extract_symbols


def _tags(text, rel="m.py"):
    return extract_symbols(rel, f"/abs/{rel}", text)


def _names(tags, kind):
    return {t.name for t in tags if t.kind == kind}


def test_defs_functions_classes_methods_nested():
    code = (
        "class Server:\n"
        "    def serve(self, port):\n"
        "        def inner():\n"
        "            pass\n"
        "async def main():\n"
        "    pass\n"
    )
    defs = _names(_tags(code), "def")
    assert {"Server", "serve", "inner", "main"} <= defs
    assert "Server.serve" in defs                     # 方法另发限定名（点查/展示）


def test_def_carries_signature_line():
    tags = _tags("def handler(req, *, timeout=30):\n    pass\n")
    d = next(t for t in tags if t.kind == "def" and t.name == "handler")
    assert d.text == "def handler(req, *, timeout=30):"


def test_refs_calls_attributes_imports():
    code = (
        "from pkg.mod import helper\n"
        "import os\n"
        "def go(x):\n"
        "    helper(x)\n"
        "    return os.path.join(x)\n"
    )
    refs = _names(_tags(code), "ref")
    assert {"helper", "os", "join", "path"} <= refs


def test_ref_noise_filtered():
    refs = _names(_tags("def f(self):\n    print(len(str(True)))\n"), "ref")
    assert not ({"print", "len", "str", "True", "self"} & refs)


def test_syntax_error_falls_back_to_lexical():
    code = "def good():\n    pass\ndef broken(:\n"            # 语法错
    tags = _tags(code)
    assert "good" in _names(tags, "def")                       # 词法回退仍抽到 defs
    assert all(t.kind == "def" for t in tags) or _names(tags, "ref")  # 回退路径合法产出
