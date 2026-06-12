"""tree-sitter 抽取/渲染（可选 extra codeintel）——装了才跑（skipif），语义对照 aider get_tags_raw。"""

import pytest

from nanocode.codeintel.ts import extract_ts_symbols, tree_context, ts_available

pytestmark = pytest.mark.skipif(not ts_available(), reason="codeintel extra not installed")

_JS = """class OrderService {
  constructor(repo) { this.repo = repo; }

  submitOrder(
    order,
    options,
  ) {
    return helper(order);
  }
}
function makeService() {
  return new OrderService(null);
}
"""


def test_js_defs_methods_and_refs_with_lines():
    tags = extract_ts_symbols("svc.js", "/x/svc.js", _JS, "javascript")
    by = {(t.kind, t.name): t for t in tags}
    assert ("def", "OrderService") in by and ("def", "submitOrder") in by
    assert ("def", "makeService") in by
    assert by[("def", "submitOrder")].line == 4               # 真实行号（词法路径做不到 method）
    assert ("ref", "helper") in by and by[("ref", "helper")].line == 8     # call ref
    assert ("ref", "OrderService") in by                       # new_expression ref
    assert ("def", "constructor") not in by                    # #not-eq? 谓词生效（aider 同款 scm）
    assert by[("def", "submitOrder")].text.startswith("submitOrder(")      # 签名行


def test_go_and_rust_queries_load():
    go = "package main\n\nfunc Handle(x int) int {\n\treturn helper(x)\n}\n"
    names = {(t.kind, t.name) for t in extract_ts_symbols("m.go", "/x/m.go", go, "go")}
    assert ("def", "Handle") in names and ("ref", "helper") in names
    rs = "pub fn run() -> i32 {\n    helper()\n}\n"
    names = {(t.kind, t.name) for t in extract_ts_symbols("m.rs", "/x/m.rs", rs, "rust")}
    assert ("def", "run") in names


def test_tsx_aliases_to_typescript_query():
    tsx = "export function App() {\n  return render();\n}\n"
    tags = extract_ts_symbols("app.tsx", "/x/app.tsx", tsx, "tsx")
    assert tags is not None
    assert ("def", "App") in {(t.kind, t.name) for t in tags}


def test_unknown_query_falls_back_none():
    assert extract_ts_symbols("m.zig", "/x/m.zig", "fn f() {}", "zig") is None


def test_tree_context_renders_skeleton():
    ctx = tree_context("svc.js", _JS)
    assert ctx is not None
    out = ctx.render([3])                                      # submitOrder( 的 0-indexed 行
    assert "│  submitOrder(" in out
    assert "⋮" in out


def test_repo_map_integration_js(tmp_path):
    from nanocode.codeintel import RepoQuery, get_service, reset_services
    reset_services()
    (tmp_path / "service.js").write_text(
        "export class OrderService {\n  submitOrder(order) {\n    return helper(order);\n  }\n}\n")
    (tmp_path / "app.js").write_text(
        "import { OrderService } from './service.js';\nnew OrderService().submitOrder({});\n")
    svc = get_service(str(tmp_path))
    r = svc.repo_map(RepoQuery(files_read=[str(tmp_path / "app.js")]), budget_tokens=600)
    assert "service.js:" in r.text and "│" in r.text           # TreeContext 骨架
    assert "submitOrder" in r.text
    assert "app.js:" not in r.text                             # personal 不渲染
    reset_services()
