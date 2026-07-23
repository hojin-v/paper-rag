"""LLM 호출 관찰 목록을 보여주는 읽기 전용 뷰어.

`review/viewer.py`와 같은 이유로(관리자 전용 내부 도구, 프론트엔드 빌드 파이프라인
도입 비용 대비 효용 낮음) 완성된 HTML 문자열 하나를 서버에서 조립해 그대로 내려준다.
편집 상호작용이 없는 단순 목록이라 review 뷰어처럼 JS를 쓰지 않고, 프롬프트/응답
펼쳐보기는 네이티브 `<details>`/`<summary>`만으로 처리한다.
"""

import html
from typing import Any


def build_llm_calls_viewer_html(
    calls: list[dict[str, Any]],
    *,
    operation_filter: str | None,
    success_filter: bool | None,
    limit: int,
) -> str:
    """최근 LLM 호출 목록 페이지 HTML을 만든다."""
    filter_form = _filter_form(operation_filter, success_filter, limit)
    rows = "".join(_row_markup(call) for call in calls) or (
        '<tr><td colspan="7" class="empty">기록된 호출이 없습니다.</td></tr>'
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>LLM 호출 관찰</title>
<style>
body{{font-family:-apple-system,sans-serif;margin:24px;color:#1a1a1a;background:#fafafa}}
h1{{font-size:20px}}
form{{margin:16px 0;display:flex;gap:8px;align-items:center}}
select,button{{padding:6px 10px;font-size:14px}}
table{{width:100%;border-collapse:collapse;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
th,td{{padding:8px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top;font-size:13px}}
th{{background:#f0f0f0;position:sticky;top:0}}
tr.failed{{background:#fff4f4}}
.badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px}}
.badge.ok{{background:#e6f4ea;color:#1e7e34}}
.badge.fail{{background:#fce8e8;color:#c62828}}
.badge.cache{{background:#e3f2fd;color:#1565c0;margin-left:4px}}
.empty{{text-align:center;color:#888;padding:24px}}
details summary{{cursor:pointer;color:#1565c0}}
pre{{white-space:pre-wrap;word-break:break-all;background:#f7f7f7;padding:8px;border-radius:4px;max-height:300px;overflow:auto}}
.error{{color:#c62828}}
</style>
</head>
<body>
<h1>LLM 호출 관찰 (최근 {len(calls)}건)</h1>
{filter_form}
<table>
<thead><tr>
<th>시각</th><th>operation</th><th>model</th><th>지연시간</th><th>토큰</th><th>상태</th><th>프롬프트/응답</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>"""


def _filter_form(operation_filter: str | None, success_filter: bool | None, limit: int) -> str:
    success_value = "" if success_filter is None else ("true" if success_filter else "false")
    return f"""<form method="get">
<label>operation:
<input type="text" name="operation" value="{html.escape(operation_filter or "")}" placeholder="예: paragraph_enrich">
</label>
<label>상태:
<select name="success">
<option value="" {"selected" if success_value == "" else ""}>전체</option>
<option value="true" {"selected" if success_value == "true" else ""}>성공</option>
<option value="false" {"selected" if success_value == "false" else ""}>실패</option>
</select>
</label>
<label>개수:
<input type="number" name="limit" value="{limit}" min="1" max="1000" style="width:70px">
</label>
<button type="submit">조회</button>
</form>"""


def _row_markup(call: dict[str, Any]) -> str:
    success = call["success"]
    status_badge = (
        '<span class="badge ok">성공</span>' if success else '<span class="badge fail">실패</span>'
    )
    if call.get("cache_hit"):
        status_badge += '<span class="badge cache">캐시</span>'
    error_html = (
        f'<div class="error">{html.escape(str(call["error"]))}</div>' if call.get("error") else ""
    )
    latency = call.get("latency_ms")
    latency_text = f"{latency:.0f}ms" if latency is not None else "-"
    tokens = call.get("prompt_tokens"), call.get("completion_tokens")
    tokens_text = f"{tokens[0] or 0} / {tokens[1] or 0}"
    row_class = "" if success else ' class="failed"'
    prompt_text = html.escape(str(call["prompt"]))
    response_text = html.escape(str(call.get("response") or ""))
    return f"""<tr{row_class}>
<td>{html.escape(str(call["created_at"]))}</td>
<td>{html.escape(str(call["operation"]))}</td>
<td>{html.escape(str(call["model"]))}</td>
<td>{latency_text}</td>
<td>{tokens_text}</td>
<td>{status_badge}{error_html}</td>
<td>
<details><summary>프롬프트</summary><pre>{prompt_text}</pre></details>
<details><summary>응답</summary><pre>{response_text}</pre></details>
</td>
</tr>"""
