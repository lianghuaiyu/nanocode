"""edit_file 工具：以精确字符串匹配替换文件内容（支持引号归一化）。"""

from __future__ import annotations

from .shared import _normalize_quotes, _find_actual_string, _generate_diff

SCHEMA = {
    "name": "edit_file",
    "description": "Edit a file by replacing an exact string match with new content. The old_string must match exactly (including whitespace and indentation).",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "The path to the file to edit"},
            "old_string": {"type": "string", "description": "The exact string to find and replace"},
            "new_string": {"type": "string", "description": "The string to replace it with"},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
}


def run(ctx, inp: dict) -> str:
    try:
        path = inp["file_path"]
        content = ctx.fs_write.read_text(path)

        actual = _find_actual_string(content, inp["old_string"])
        if not actual:
            return f"Error: old_string not found in {inp['file_path']}"

        count = content.count(actual)
        if count > 1:
            return f"Error: old_string found {count} times in {inp['file_path']}. Must be unique."

        new_content = content.replace(actual, inp["new_string"], 1)
        ctx.fs_write.write_text(path, new_content)

        diff = _generate_diff(content, actual, inp["new_string"])
        quote_note = " (matched via quote normalization)" if actual != inp["old_string"] else ""
        return f"Successfully edited {inp['file_path']}{quote_note}\n\n{diff}"
    except Exception as e:
        return f"Error editing file: {e}"
