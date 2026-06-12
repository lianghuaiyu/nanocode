"""codeintel/graph：aider get_ranked_tags 复刻——边权 / 个性化 PageRank / rank 分发。"""

from nanocode.codeintel.graph import rank_tags
from nanocode.codeintel.index import RepoQuery
from nanocode.codeintel.symbols import SymbolTag


def _def(rel, name, line=1):
    return SymbolTag(rel_path=rel, abs_path=f"/{rel}", line=line, name=name,
                     kind="def", language="python", text=f"def {name}():")


def _ref(rel, name, line=1):
    return SymbolTag(rel_path=rel, abs_path=f"/{rel}", line=line, name=name,
                     kind="ref", language="python")


def _files(tags):
    return [(t[0] if isinstance(t, tuple) else t.rel_path) for t in tags]


def test_personal_files_seed_but_never_rendered():
    # aider 核心语义：chat/已读文件是 PageRank 种子（×50 referencer 边权），自身不渲染。
    tags_by_file = {
        "edited.py": [_def("edited.py", "main"), _ref("edited.py", "load_config")],
        "config.py": [_def("config.py", "load_config")],
        "unrelated.py": [_def("unrelated.py", "noop")],
    }
    out = rank_tags(tags_by_file, RepoQuery(files_read=["edited.py"]))
    files = _files(out)
    assert "edited.py" not in files                   # 种子不渲染（全文已在上下文）
    assert files.index("config.py") < files.index("unrelated.py")  # 被种子引用 → 排前


def test_mentioned_ident_x10_pulls_definition_file_first():
    # ×10 经出边权重比例起效：user.py 同时引用两个符号，提及的那个拿走绝大部分 rank 分发。
    # （两节点单出边环会被归一化抹平权重——aider 同构，故 fixture 用三节点。）
    tags_by_file = {
        "user.py": [_ref("user.py", "session_lease"), _ref("user.py", "misc_helper_fn")],
        "a.py": [_def("a.py", "session_lease")],
        "c.py": [_def("c.py", "misc_helper_fn")],
    }
    out = rank_tags(tags_by_file, RepoQuery(mentioned_identifiers=["session_lease"]))
    first = next(t for t in out if not isinstance(t, tuple))
    assert first.rel_path == "a.py" and first.name == "session_lease"


def test_private_idents_damped():
    # _ 开头 ×0.1：同等引用下私有符号的文件排私有名后面。
    tags_by_file = {
        "user.py": [_ref("user.py", "_internal"), _ref("user.py", "public_api_name")],
        "priv.py": [_def("priv.py", "_internal")],
        "pub.py": [_def("pub.py", "public_api_name")],
    }
    out = rank_tags(tags_by_file, RepoQuery(files_read=["user.py"]))
    files = _files(out)
    assert files.index("pub.py") < files.index("priv.py")


def test_widely_defined_idents_damped():
    # 定义出现在 >5 个文件的 ident ×0.1（如 main/test 之类的泛名不该主导排名）。
    tags_by_file = {"user.py": [_ref("user.py", "setup"), _ref("user.py", "rare_unique_fn")],
                    "target.py": [_def("target.py", "rare_unique_fn")]}
    for i in range(6):
        tags_by_file[f"g{i}.py"] = [_def(f"g{i}.py", "setup")]
    out = rank_tags(tags_by_file, RepoQuery(files_read=["user.py"]))
    files = _files(out)
    assert files.index("target.py") < min(files.index(f"g{i}.py") for i in range(6))


def test_defs_only_repo_does_not_collapse():
    # 全仓库无 ref → references=defines 自指兜底（aider :466-467），排名仍产出。
    tags_by_file = {"a.py": [_def("a.py", "alpha")], "b.py": [_def("b.py", "beta")]}
    out = rank_tags(tags_by_file, RepoQuery())
    assert {"a.py", "b.py"} <= set(_files(out))


def test_mentioned_ident_matches_path_components():
    # aider 技巧：提及的标识符命中文件路径成分 → personalization。
    tags_by_file = {
        "session.py": [_def("session.py", "x1")],
        "zother.py": [_def("zother.py", "x2")],
    }
    out = rank_tags(tags_by_file, RepoQuery(mentioned_identifiers=["session"]))
    files = _files(out)
    assert files.index("session.py") < files.index("zother.py")
