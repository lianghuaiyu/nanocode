"""web_fetch 工具：抓取 URL 并返回文本（HTML 去标签，其余原样）。"""

from __future__ import annotations

import re

SCHEMA = {
    "name": "web_fetch",
    "description": "Fetch a URL and return its content as text. For HTML pages, tags are stripped to return readable text. For JSON/text responses, content is returned directly.",
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"},
            "max_length": {"type": "number", "description": "Maximum content length in characters (default 50000)"},
        },
        "required": ["url"],
    },
}


def run(ctx, inp: dict) -> str:
    # ctx 接收以保持 dispatch arity 一致；web_fetch 是网络操作，不用 fs 把手。
    import urllib.request
    import urllib.error

    url = inp.get("url", "")
    max_length = inp.get("max_length", 50000)
    req = urllib.request.Request(url, headers={"User-Agent": "nanocode/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return f"HTTP error: {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return f"Error fetching {url}: {e.reason}"
    except Exception as e:
        return f"Error fetching {url}: {e}"

    if "html" in content_type:
        text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]*>", " ", text)
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")
        text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
        text = re.sub(r"\s{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()

    if len(text) > max_length:
        text = text[:max_length] + f"\n\n[... truncated at {max_length} characters]"

    return text or "(empty response)"
