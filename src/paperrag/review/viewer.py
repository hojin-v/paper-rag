"""서버사이드에서 HTML+SVG+JS를 문자열로 직접 만드는 검수 뷰어.

이 화면은 관리자·운영자만 쓰는 내부 도구다(일반 검색 사용자는 이 화면을 보지 않는다). 그런
제한된 용도에 비해 React/Vue 같은 프론트엔드 프레임워크·빌드 파이프라인을 새로 들이는 비용이
크다고 판단해, FastAPI가 `HTMLResponse`로 바로 내려줄 수 있는 하나의 완성된 HTML 문자열을
Python에서 f-string으로 조립하는 방식을 택했다. 즉 별도 프론트엔드 의존성·빌드 단계 없이,
백엔드 배포만으로 뷰어까지 함께 배포된다는 것이 핵심 이유다.

렌더링 흐름: 페이지 이미지 위에 블록 좌표를 SVG `<rect>` 오버레이로 그리고, 블록 데이터
전체(JSON)를 `<script id="review-data" type="application/json">`에 심어 아래 인라인 JS가
그 데이터를 읽어 클릭 선택·박스 그리기·리사이즈 같은 상호작용을 처리한다. 서버는 초기 HTML만
만들고 그 이후 상호작용은 전부 브라우저의 fetch 호출(PUT/POST/DELETE)로 API를 직접 두드린다.
"""

import html
import json

from paperrag.ingest.models import BLOCK_TYPES
from paperrag.review.models import ReviewDocument

# 블록 유형(BlockType) 코드 → 한국어 표시 이름
BLOCK_LABELS = {
    "title": "제목",
    "author": "저자",
    "abstract": "초록",
    "section_header": "섹션 제목",
    "text": "본문",
    "table": "표",
    "table_caption": "표 캡션",
    "figure": "그림",
    "figure_caption": "그림 캡션",
    "formula": "수식",
    "reference": "참고문헌",
    "header_footer": "머리말/꼬리말",
}

# 블록 유형별 SVG 오버레이/범례 색상
BLOCK_COLORS = {
    "title": "#c62828",
    "author": "#7b1fa2",
    "abstract": "#00796b",
    "section_header": "#ef6c00",
    "text": "#1565c0",
    "table": "#2e7d32",
    "table_caption": "#558b2f",
    "figure": "#6a1b9a",
    "figure_caption": "#8e24aa",
    "formula": "#5d4037",
    "reference": "#455a64",
    "header_footer": "#616161",
}


def build_viewer_html(document: ReviewDocument, *, editable: bool = True) -> str:
    """검수 문서 하나를 위한 완성된 HTML 페이지 문자열을 생성한다.

    `editable=False`는 운영자용 읽기 전용 자동 처리 품질 모니터, `editable=True`는 관리자가
    좌표·유형·텍스트를 직접 고치는 교정 화면이다. 두 모드가 같은 템플릿을 공유하며 JS 쪽에서
    `state.editable`/`state.phase` 값으로 어떤 입력을 활성화할지 결정한다(레이아웃 단계에서는
    좌표·유형·삭제만, OCR 이후 단계에서는 텍스트 교정·검수 상태만 허용).
    블록 데이터는 `<` 문자를 이스케이프한 JSON으로 `<script type="application/json">`에
    통째로 심어 브라우저 쪽 스크립트가 별도 API 호출 없이 초기 상태를 즉시 읽게 한다.
    """
    payload = json.dumps(
        {
            "document_id": document.document_id,
            "phase": document.phase,
            "editable": editable,
            "blocks": [block.model_dump(mode="json") for block in document.blocks],
            "labels": BLOCK_LABELS,
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    page_markup = "\n".join(_page_markup(document, page.page) for page in document.pages)
    show_run_ocr = editable and document.phase == "layout_review"
    show_ocr_review_actions = editable and document.phase == "ocr_review"
    options = "".join(
        f'<option value="{name}">{html.escape(BLOCK_LABELS.get(name, name))}</option>'
        for name in sorted(BLOCK_TYPES)
    )
    present_types = sorted({block.block_type for block in document.blocks})
    # present_types가 아니라 전체 12개 유형으로 CSS 규칙을 만든다 — 사용자가 블록 유형을
    # 교정해 이 문서에 아직 없던 유형으로 바뀌어도(예: 유일한 abstract를 author로 고침),
    # 브라우저 쪽에서 새로고침 없이 rect의 class만 바꿔 즉시 올바른 색을 보여줄 수 있어야
    # 하기 때문이다(아래 saveBlock() 참고). present_types로만 제한하면 그 유형의 CSS 규칙
    # 자체가 없어서 색이 기본값으로 보이는 문제가 생긴다.
    block_styles = "".join(
        f".overlay rect.block-{block_type}"
        f"{{fill:{BLOCK_COLORS.get(block_type, '#1769e0')}26;"
        f"stroke:{BLOCK_COLORS.get(block_type, '#1769e0')}}}"
        for block_type in sorted(BLOCK_TYPES)
    )
    legend = "".join(
        f'<span><i style="background:{BLOCK_COLORS.get(block_type, "#1769e0")}"></i>'
        f"{html.escape(BLOCK_LABELS.get(block_type, block_type))}</span>"
        for block_type in present_types
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(document.filename)} 분석 검수</title>
<style>
:root{{--panel:#fff;--ink:#172033;--muted:#697386;--line:#d8dee9;--accent:#1769e0}}
*{{box-sizing:border-box}} body{{margin:0;background:#eef1f5;color:var(--ink);font:14px/1.5 system-ui,sans-serif}}
header{{position:sticky;top:0;z-index:20;padding:12px 18px;background:#172033;color:#fff}}
header strong{{font-size:16px}} header span{{margin-left:12px;color:#b9c4d6}}
header a{{float:right;color:#fff;background:#1769e0;padding:5px 10px;border-radius:6px;text-decoration:none}}
.layout{{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,360px);gap:16px;max-width:1500px;margin:16px auto;padding:0 16px}}
.pages{{min-width:0}}
.page{{position:relative;margin:0 auto 20px;background:#fff;box-shadow:0 2px 12px #0002}}
.page img{{display:block;width:100%;height:auto}}
.overlay{{position:absolute;inset:0;width:100%;height:100%}}
.overlay rect{{fill:#1769e022;stroke:#1769e0;stroke-width:1.5;vector-effect:non-scaling-stroke;cursor:pointer}}
{block_styles}
.overlay rect:hover,.overlay rect.selected{{fill:#ffb00044;stroke:#e46f00;stroke-width:3}}
.overlay.drawing{{cursor:crosshair}} .overlay rect.draw-preview{{fill:#12a15033;stroke:#087f3f;pointer-events:none}}
.overlay.editing rect.selected{{cursor:move}} .resize-handle{{fill:#fff;stroke:#087f3f;stroke-width:2;vector-effect:non-scaling-stroke;cursor:nwse-resize}}
.page-number{{position:absolute;top:6px;left:6px;padding:2px 7px;background:#172033cc;color:#fff;border-radius:4px}}
aside{{position:sticky;top:66px;align-self:start;max-height:calc(100vh - 82px);overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px}}
.legend{{display:flex;flex-wrap:wrap;gap:6px 10px;margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid var(--line)}}
.legend span{{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--muted)}}
.legend i{{width:10px;height:10px;border-radius:2px}}
.empty{{color:var(--muted)}} label{{display:block;margin:12px 0 4px;font-weight:700}}
textarea,select{{width:100%;padding:9px;border:1px solid var(--line);border-radius:6px;background:#fff}}
input[type=number]{{width:23%;padding:7px;border:1px solid var(--line);border-radius:6px}}
textarea{{min-height:170px;resize:vertical}} .meta{{display:grid;grid-template-columns:90px 1fr;gap:5px;margin:10px 0}}
.meta dt{{color:var(--muted)}} .meta dd{{margin:0;word-break:break-all}}
button{{margin-top:12px;width:100%;padding:10px;border:0;border-radius:7px;background:var(--accent);color:#fff;font-weight:700;cursor:pointer}}
button.danger{{background:#b42318}}
#message{{min-height:22px;margin-top:8px;color:#087f3f}}
.status-summary{{display:flex;flex-wrap:wrap;gap:8px 14px;margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid var(--line);font-size:13px}}
.status-summary b{{color:var(--ink)}} .status-summary .status-unreviewed-count{{color:#b42318;font-weight:700}}
.overlay rect.status-unreviewed{{stroke-dasharray:5,4}}
.read-only .edit-only{{display:none}} .edit-help{{margin:8px 0;padding:8px;background:#fff4d6;border-radius:6px;color:#725000}}
@media(max-width:720px){{.layout{{display:flex;flex-direction:column}} aside{{order:-1;position:relative;top:auto;max-height:55vh;margin-bottom:16px}}}}
</style>
</head>
<body class="{"editable" if editable else "read-only"}">
<header><strong>{html.escape(document.filename)}</strong><span>{"관리자 교정 모드" if editable else "읽기 전용 모니터"} · {html.escape(document.phase)} · {html.escape(document.backend)} · {len(document.blocks)}개 영역</span><a href="?editable={"false" if editable else "true"}">{"읽기 전용 모니터로 전환" if editable else "관리자 교정 모드 열기"}</a></header>
<main class="layout">
  <section class="pages">{page_markup}</section>
  <aside>
    <div class="legend">{legend}</div>
    <div id="status-summary" class="status-summary"></div>
    <button type="button" id="jump-unreviewed" onclick="jumpToNextUnreviewed()">다음 미검수 영역으로 이동</button>
    <section id="phase-actions">
      <button type="button" id="run-ocr-btn" onclick="runOcr()" {"hidden" if not show_run_ocr else ""}>영역별 OCR 실행</button>
      <button type="button" id="confirm-ocr-btn" onclick="confirmOcr()" {"hidden" if not show_ocr_review_actions else ""}>OCR 검수 완료</button>
      <button type="button" id="return-layout-btn" class="danger" onclick="returnToLayout()" {"hidden" if not show_ocr_review_actions else ""}>레이아웃부터 다시 수정</button>
      <div id="phase-message"></div>
    </section>
    <section id="layout-tools" {"hidden" if document.phase != "layout_review" or not editable else ""}>
      <strong>누락 영역 추가</strong>
      <label for="new-block-type">새 영역 유형</label><select id="new-block-type">{options}</select>
      <button type="button" id="draw-toggle" onclick="toggleDrawMode()">페이지에서 박스 그리기 시작</button>
      <button type="button" id="edit-toggle" onclick="toggleEditMode()">선택 박스 이동·크기 조절</button>
      <p class="empty">버튼을 누른 뒤 페이지에서 누락 영역을 드래그하세요.</p>
      <div id="draw-message"></div>
    </section>
    <div id="empty" class="empty">페이지의 색상 영역을 클릭하면 자동 레이아웃 좌표와 해당 영역의 OCR 결과를 확인할 수 있습니다.</div>
    <form id="editor" hidden onsubmit="saveBlock(event)">
      <h2 id="block-title">영역</h2>
      <dl class="meta"><dt>페이지</dt><dd id="page"></dd><dt>자동 유형</dt><dd id="detected-type"></dd><dt>자동 좌표</dt><dd id="detected-bbox"></dd><dt>현재 좌표</dt><dd id="bbox"></dd><dt>신뢰도</dt><dd id="confidence"></dd><dt>OCR 엔진</dt><dd id="engine"></dd></dl>
      <div class="edit-only"><div id="phase-help" class="edit-help"></div>
      <label for="block-type">영역 유형</label><select id="block-type">{options}</select>
      <label>영역 좌표 (x1, y1, x2, y2)</label><div><input id="x1" type="number" step="any"><input id="y1" type="number" step="any"><input id="x2" type="number" step="any"><input id="y2" type="number" step="any"></div>
      <button type="button" id="revert-detected" onclick="revertToDetected()">자동 검출값으로 되돌리기</button>
      </div>
      <label for="ocr-text">모델 OCR 원문</label><textarea id="ocr-text" readonly></textarea>
      <div class="edit-only">
      <label for="corrected-text">검수·교정 텍스트</label><textarea id="corrected-text"></textarea>
      <label for="review-status">검수 상태</label>
      <select id="review-status"><option value="unreviewed">미검수</option><option value="approved">승인</option><option value="corrected">교정</option><option value="rejected">제외</option></select>
      <button type="submit">검수 결과 저장</button>
      <button type="button" id="delete-block" class="danger" onclick="deleteCurrentBlock()">선택 영역 삭제</button>
      <div id="message"></div>
      </div>
    </form>
  </aside>
</main>
<script id="review-data" type="application/json">{payload}</script>
<script>
// review-data 스크립트 태그에 서버가 심어둔 JSON(state)을 파싱해 프런트엔드 상태로 사용한다.
// current: 현재 선택된 블록, drawMode/editMode: 박스 그리기·이동/리사이즈 모드 on/off,
// drawStart/preview: 그리기 중인 미리보기 사각형, editStart: 드래그로 이동/리사이즈 중인 상태.
const state=JSON.parse(document.getElementById('review-data').textContent); let current=null; let drawMode=false; let editMode=false; let drawStart=null; let preview=null; let editStart=null;
// 블록 하나를 선택해 오른쪽 편집 패널에 상세 정보를 채운다.
// 현재 phase(layoutOnly)와 editable(canEdit) 조합에 따라 어떤 입력을 활성화할지 여기서 결정한다.
function selectBlock(id){{
  current=state.blocks.find(item=>item.block_id===id); if(!current)return;
  document.querySelectorAll('rect').forEach(el=>el.classList.toggle('selected',el.dataset.id===id));
  document.getElementById('empty').hidden=true; document.getElementById('editor').hidden=false;
  document.getElementById('block-title').textContent=`${{state.labels[current.block_type]||current.block_type}} · ${{current.block_id}}`;
  document.getElementById('page').textContent=current.page;
  const detectedType=current.detected_block_type||current.block_type;
  const detectedBbox=current.detected_bbox||current.bbox;
  document.getElementById('detected-type').textContent=state.labels[detectedType]||detectedType;
  document.getElementById('detected-bbox').textContent=detectedBbox?detectedBbox.map(v=>Number(v).toFixed(1)).join(', '):'좌표 없음';
  document.getElementById('bbox').textContent=current.bbox?current.bbox.map(v=>Number(v).toFixed(1)).join(', '):'좌표 없음';
  document.getElementById('confidence').textContent=current.confidence==null?'-':Number(current.confidence).toFixed(3);
  document.getElementById('engine').textContent=current.ocr_engine||'-';
  document.getElementById('block-type').value=current.block_type;
  ['x1','y1','x2','y2'].forEach((id,index)=>document.getElementById(id).value=current.bbox?current.bbox[index]:'');
  const layoutOnly=state.phase==='layout_review'; const canEdit=state.editable;
  document.getElementById('phase-help').textContent=layoutOnly
    ?'레이아웃 단계: 영역 유형·좌표·삭제를 수정할 수 있고 OCR 텍스트 교정은 아직 사용할 수 없습니다.'
    :'OCR 이후 단계: OCR 결과와 검수 상태만 수정할 수 있습니다. 유형·좌표 변경은 레이아웃 단계로 되돌아가야 합니다.';
  document.getElementById('block-type').disabled=!layoutOnly||!canEdit;
  ['x1','y1','x2','y2'].forEach(id=>document.getElementById(id).disabled=!layoutOnly||!canEdit);
  document.getElementById('ocr-text').value=layoutOnly?'레이아웃 확정 후 이 박스만 OCR합니다.':(current.ocr_text||'');
  document.getElementById('corrected-text').value=layoutOnly?'':(current.corrected_text==null?current.ocr_text:current.corrected_text);
  document.getElementById('corrected-text').disabled=layoutOnly||!canEdit;
  document.getElementById('review-status').disabled=!canEdit;
  document.querySelector('#editor button[type="submit"]').hidden=!canEdit;
  document.getElementById('delete-block').hidden=!layoutOnly||!canEdit;
  document.getElementById('revert-detected').hidden=!layoutOnly||!canEdit;
  document.getElementById('review-status').value=current.review_status;
  document.getElementById('message').textContent='';
  renderEditHandles();
}}
// 현재 선택된 블록의 유형·좌표 입력을 모델이 처음 검출한 값(detected_block_type/detected_bbox)으로
// 되돌린다. 화면(폼)만 되돌릴 뿐 서버에는 아직 반영하지 않으므로, 이 상태를 실제로 저장하려면
// "검수 결과 저장"을 눌러야 한다 — 사람이 교정을 시작하기 전 원래 자동 검출값으로 다시 맞춰보고
// 싶을 때 쓴다(예: 잘못 고친 뒤 원래 상태부터 다시 보고 싶을 때).
function revertToDetected(){{
  if(!current)return;
  const type=current.detected_block_type||current.block_type;
  const bbox=current.detected_bbox||current.bbox;
  document.getElementById('block-type').value=type;
  ['x1','y1','x2','y2'].forEach((id,index)=>document.getElementById(id).value=bbox?bbox[index]:'');
  document.getElementById('message').textContent='자동 검출값으로 되돌렸습니다. 검수 결과 저장을 눌러야 반영됩니다.';
}}
// 편집 폼 제출 시 PUT /documents/<id>/blocks/<id>로 유형·좌표·검수 상태(및 OCR 이후 단계면
// 교정 텍스트)를 저장한다. 서버(update_block)는 phase가 layout_review가 아닐 때 body에
// block_type/bbox 키가 "존재하기만 해도"(값이 실제로 바뀌었는지와 무관하게) 좌표·유형을
// 바꾸려는 시도로 보고 거부한다 — 그래서 layout_review가 아닌 단계에서는 이 두 키를 아예
// body에 넣지 않아야 한다(넣으면 OCR 텍스트 교정 저장 자체가 매번 실패한다).
async function saveBlock(event){{
  event.preventDefault(); if(!current)return;
  const body={{review_status:document.getElementById('review-status').value}};
  if(state.phase==='layout_review'){{
    body.block_type=document.getElementById('block-type').value;
    body.bbox=['x1','y1','x2','y2'].map(id=>Number(document.getElementById(id).value));
  }}else{{
    body.corrected_text=document.getElementById('corrected-text').value;
  }}
  const response=await fetch(`/documents/${{state.document_id}}/blocks/${{current.block_id}}`,{{method:'PUT',headers:{{'content-type':'application/json'}},body:JSON.stringify(body)}});
  if(!response.ok){{document.getElementById('message').textContent='저장 실패';return}}
  Object.assign(current,body);
  {{
    // 드래그로 이동·리사이즈할 때는 포인터 이벤트가 rect의 x/y/width/height를 그 자리에서
    // 바로 갱신하지만, 좌표 입력창에 직접 값을 넣거나(예: revertToDetected) 타이핑해서
    // 저장한 경우에는 그 갱신이 한 번도 일어나지 않는다 — 그래서 여기서 저장 성공 후
    // rect를 body의 최종값으로 다시 그려야, 새로고침 없이도 화면 박스가 실제 저장된
    // 좌표·유형과 일치한다. class는 block_type/bbox를 안 바꿔도(예: OCR 단계에서
    // 검수 상태만 바꾼 경우) status- 부분이 최신 review_status를 반영하도록 항상 다시 쓴다.
    const rectEl=document.querySelector(`rect[data-id="${{current.block_id}}"]`);
    if(rectEl){{
      rectEl.setAttribute('class',`block-${{current.block_type}} status-${{current.review_status}}`);
      if(body.bbox){{
        const [x1,y1,x2,y2]=body.bbox;
        rectEl.setAttribute('x',x1); rectEl.setAttribute('y',y1);
        rectEl.setAttribute('width',x2-x1); rectEl.setAttribute('height',y2-y1);
      }}
    }}
    renderEditHandles();
  }}
  document.getElementById('message').textContent='저장했습니다.';
  renderStatusSummary();
}}
// 검수 상태(미검수/승인/교정/제외) 개수를 집계해 사이드바에 표시한다. 저장할 때마다
// 다시 계산해서, 화면을 새로고침하지 않아도 "몇 개나 남았는지"를 바로 확인할 수 있게 한다.
function renderStatusSummary(){{
  const counts={{unreviewed:0,approved:0,corrected:0,rejected:0}};
  for(const block of state.blocks)counts[block.review_status]=(counts[block.review_status]||0)+1;
  const total=state.blocks.length;
  document.getElementById('status-summary').innerHTML=
    `<span class="status-unreviewed-count">미검수 ${{counts.unreviewed}}</span>`+
    `<span>승인 ${{counts.approved}}</span><span>교정 ${{counts.corrected}}</span>`+
    `<span>제외 ${{counts.rejected}}</span><span>(총 ${{total}}개)</span>`;
}}
// "다음 미검수 영역으로 이동" 버튼: state.blocks에서 미검수 블록을 하나 찾아 선택하고
// 그 박스가 있는 페이지로 스크롤한다. 여러 페이지짜리 문서에서 미검수 블록을 눈으로
// 일일이 찾아야 하는 문제(라벨링 중 실제로 겪은 문제)를 해결하기 위한 기능이다.
function jumpToNextUnreviewed(){{
  const next=state.blocks.find(block=>block.review_status==='unreviewed');
  if(!next){{document.getElementById('message').textContent='미검수 영역이 없습니다.';return}}
  selectBlock(next.block_id);
  const rectEl=document.querySelector(`rect[data-id="${{next.block_id}}"]`);
  if(rectEl)rectEl.scrollIntoView({{behavior:'smooth',block:'center'}});
}}
// task_id를 2초 간격으로 폴링해 Celery 작업이 끝날 때까지 기다린다(review/api.py의
// GET /jobs/{{task_id}}와 동일한 상태 값: pending/started/success/failure).
async function pollJob(taskId){{
  while(true){{
    const response=await fetch(`/jobs/${{taskId}}`); const body=await response.json();
    if(body.status==='success')return {{ok:true}};
    if(body.status==='failure')return {{ok:false,error:body.error}};
    await new Promise(resolve=>setTimeout(resolve,2000));
  }}
}}
// "영역별 OCR 실행": 페이지 1장짜리라도 몇 분 걸릴 수 있어(docs/guide/10-production-readiness.md
// 실측) 동기 호출 대신 비동기 큐(run-ocr/async)에 제출하고 폴링한다 — 동기로 하면 리버스
// 프록시·브라우저 타임아웃에 걸릴 위험이 있다(review/api.py의 submit_reviewed_ocr와 동일한 이유).
async function runOcr(){{
  const button=document.getElementById('run-ocr-btn'); button.disabled=true;
  document.getElementById('phase-message').textContent='영역별 OCR 실행 중입니다... (몇 분 걸릴 수 있습니다)';
  const submitted=await fetch(`/documents/${{state.document_id}}/run-ocr/async`,{{method:'POST'}});
  if(!submitted.ok){{document.getElementById('phase-message').textContent='OCR 제출 실패';button.disabled=false;return}}
  const {{task_id}}=await submitted.json();
  const result=await pollJob(task_id);
  if(!result.ok){{document.getElementById('phase-message').textContent=`OCR 실패: ${{result.error}}`;button.disabled=false;return}}
  location.reload();
}}
// "OCR 검수 완료": 미검수 블록이 남아 있으면 서버가 400으로 거부하므로 그 메시지를 그대로 보여준다.
async function confirmOcr(){{
  const response=await fetch(`/documents/${{state.document_id}}/confirm-ocr`,{{method:'POST'}});
  if(!response.ok){{
    const body=await response.json().catch(()=>({{}}));
    document.getElementById('phase-message').textContent=body.detail||'OCR 검수 완료 실패';
    return;
  }}
  location.reload();
}}
// "레이아웃부터 다시 수정": 현재 OCR 결과(텍스트)를 전부 폐기하고 레이아웃 검수로 되돌리는
// 되돌릴 수 없는 작업이라 확인창을 거친다.
async function returnToLayout(){{
  if(!confirm('OCR 결과를 폐기하고 레이아웃 검수로 되돌리시겠습니까?'))return;
  const response=await fetch(`/documents/${{state.document_id}}/return-to-layout`,{{method:'POST'}});
  if(!response.ok){{
    const body=await response.json().catch(()=>({{}}));
    document.getElementById('phase-message').textContent=body.detail||'되돌리기 실패';
    return;
  }}
  location.reload();
}}
// 선택된 블록을 삭제 확인 후 DELETE API로 지운다. 성공하면 화면을 새로고침해 최신 상태를 반영한다.
async function deleteCurrentBlock(){{
  if(!current||state.phase!=='layout_review')return;
  const label=state.labels[current.block_type]||current.block_type;
  if(!confirm(`${{label}} · ${{current.block_id}} 영역을 삭제하시겠습니까? 이 영역은 OCR 입력에서도 제거됩니다.`))return;
  const response=await fetch(`/documents/${{state.document_id}}/blocks/${{current.block_id}}`,{{method:'DELETE'}});
  if(!response.ok){{document.getElementById('message').textContent='영역 삭제 실패';return}}
  location.reload();
}}
// 누락 영역을 드래그로 새로 그리는 모드를 켜고 끈다. 편집(이동/리사이즈) 모드와는 동시에 켤 수 없다.
function toggleDrawMode(){{
  if(state.phase!=='layout_review')return;
  if(editMode)toggleEditMode();
  drawMode=!drawMode; drawStart=null;
  document.querySelectorAll('.overlay').forEach(svg=>svg.classList.toggle('drawing',drawMode));
  document.getElementById('draw-toggle').textContent=drawMode?'박스 그리기 취소':'페이지에서 박스 그리기 시작';
  document.getElementById('draw-message').textContent=drawMode?'페이지의 누락 영역을 드래그하세요.':'';
}}
// 기존 박스를 드래그로 이동하거나 모서리 점으로 크기를 바꾸는 편집 모드를 켜고 끈다.
function toggleEditMode(){{
  if(state.phase!=='layout_review')return;
  if(drawMode)toggleDrawMode();
  editMode=!editMode; editStart=null;
  document.querySelectorAll('.overlay').forEach(svg=>svg.classList.toggle('editing',editMode));
  document.getElementById('edit-toggle').textContent=editMode?'박스 수정 모드 종료':'선택 박스 이동·크기 조절';
  document.getElementById('draw-message').textContent=editMode?'박스를 드래그해 이동하거나 모서리 점을 드래그해 크기를 바꾼 뒤 저장하세요.':'';
  renderEditHandles();
}}
// 편집 모드에서 표시하던 네 모서리 리사이즈 핸들(원)을 전부 제거한다.
function clearEditHandles(){{document.querySelectorAll('.resize-handle').forEach(handle=>handle.remove())}}
// 현재 선택된 블록의 사각형 네 모서리에 리사이즈 핸들을 다시 그린다(좌표가 바뀔 때마다 다시 호출됨).
function renderEditHandles(){{
  clearEditHandles(); if(!editMode||!current)return;
  const rect=document.querySelector(`rect[data-id="${{current.block_id}}"]`); if(!rect)return; const svg=rect.ownerSVGElement;
  const x=Number(rect.getAttribute('x')); const y=Number(rect.getAttribute('y')); const width=Number(rect.getAttribute('width')); const height=Number(rect.getAttribute('height'));
  for(const [corner,cx,cy] of [['nw',x,y],['ne',x+width,y],['sw',x,y+height],['se',x+width,y+height]]){{
    const handle=document.createElementNS('http://www.w3.org/2000/svg','circle'); handle.setAttribute('class','resize-handle'); handle.dataset.corner=corner; handle.dataset.id=current.block_id; handle.setAttribute('cx',cx); handle.setAttribute('cy',cy); handle.setAttribute('r','5'); svg.appendChild(handle);
  }}
}}
// 브라우저 포인터 이벤트의 화면 좌표를 SVG viewBox 좌표계(=PDF 좌표계)로 변환한다.
function svgPoint(svg,event){{
  const point=svg.createSVGPoint(); point.x=event.clientX; point.y=event.clientY;
  return point.matrixTransform(svg.getScreenCTM().inverse());
}}
// 각 페이지의 SVG 오버레이에 포인터(마우스/터치) 이벤트를 붙여 두 가지 상호작용을 처리한다.
// 1) editMode: 선택된 박스를 드래그로 이동하거나 모서리 핸들로 리사이즈
// 2) drawMode: 빈 영역을 드래그해 새 박스(누락 영역)를 미리 그리기
document.querySelectorAll('.overlay').forEach(svg=>{{
  // 드래그 시작: 편집 모드면 이동/리사이즈 시작점을 기록하고, 그리기 모드면 미리보기 사각형을 만든다.
  svg.addEventListener('pointerdown',event=>{{
    const target=event.target; const tag=target.tagName.toLowerCase();
    if(editMode&&(tag==='rect'||target.classList.contains('resize-handle'))){{
      const id=target.dataset.id; if(!id)return; selectBlock(id); const rect=svg.querySelector(`rect[data-id="${{id}}"]`); if(!rect)return;
      event.preventDefault(); const point=svgPoint(svg,event); editStart={{point,rect,corner:target.dataset.corner||'move',x:Number(rect.getAttribute('x')),y:Number(rect.getAttribute('y')),width:Number(rect.getAttribute('width')),height:Number(rect.getAttribute('height'))}}; svg.setPointerCapture(event.pointerId); return;
    }}
    if(!drawMode||tag==='rect')return;
    event.preventDefault(); drawStart=svgPoint(svg,event); preview=document.createElementNS('http://www.w3.org/2000/svg','rect');
    preview.setAttribute('class','draw-preview'); preview.setAttribute('x',drawStart.x); preview.setAttribute('y',drawStart.y); preview.setAttribute('width','0'); preview.setAttribute('height','0'); svg.appendChild(preview); svg.setPointerCapture(event.pointerId);
  }});
  // 드래그 중: 편집 모드면 사각형 좌표를 실시간으로 갱신(코너별 리사이즈 규칙 포함, 페이지 밖으로 못 나가게 clamp),
  // 그리기 모드면 미리보기 사각형 크기만 갱신한다.
  svg.addEventListener('pointermove',event=>{{
    if(editStart){{
      const point=svgPoint(svg,event); const dx=point.x-editStart.point.x; const dy=point.y-editStart.point.y; let x1=editStart.x; let y1=editStart.y; let x2=editStart.x+editStart.width; let y2=editStart.y+editStart.height;
      if(editStart.corner==='move'){{x1+=dx;x2+=dx;y1+=dy;y2+=dy}}else{{if(editStart.corner.includes('w'))x1=Math.min(x2-3,x1+dx);if(editStart.corner.includes('e'))x2=Math.max(x1+3,x2+dx);if(editStart.corner.includes('n'))y1=Math.min(y2-3,y1+dy);if(editStart.corner.includes('s'))y2=Math.max(y1+3,y2+dy)}}
      const view=svg.viewBox.baseVal; const width=x2-x1; const height=y2-y1; x1=Math.max(0,Math.min(view.width-width,x1)); y1=Math.max(0,Math.min(view.height-height,y1)); x2=x1+width; y2=y1+height;
      editStart.rect.setAttribute('x',x1); editStart.rect.setAttribute('y',y1); editStart.rect.setAttribute('width',x2-x1); editStart.rect.setAttribute('height',y2-y1); renderEditHandles(); return;
    }}
    if(!drawStart||!preview)return; const point=svgPoint(svg,event);
    preview.setAttribute('x',Math.min(drawStart.x,point.x)); preview.setAttribute('y',Math.min(drawStart.y,point.y));
    preview.setAttribute('width',Math.abs(point.x-drawStart.x)); preview.setAttribute('height',Math.abs(point.y-drawStart.y));
  }});
  // 드래그 종료: 편집 모드면 최종 좌표를 폼과 current.bbox에 반영(서버 저장은 아직 안 함, 사용자가
  // "검수 결과 저장"을 눌러야 반영됨), 그리기 모드면 새 블록 생성 API를 호출한다.
  svg.addEventListener('pointerup',async event=>{{
    if(editStart){{
      const rect=editStart.rect; const bbox=[Number(rect.getAttribute('x')),Number(rect.getAttribute('y')),Number(rect.getAttribute('x'))+Number(rect.getAttribute('width')),Number(rect.getAttribute('y'))+Number(rect.getAttribute('height'))].map(value=>Math.round(value*10)/10); editStart=null; current.bbox=bbox;
      ['x1','y1','x2','y2'].forEach((id,index)=>document.getElementById(id).value=bbox[index]); document.getElementById('bbox').textContent=bbox.map(v=>v.toFixed(1)).join(', '); document.getElementById('message').textContent='박스 좌표를 바꿨습니다. 검수 결과 저장을 누르세요.'; renderEditHandles(); return;
    }}
    if(!drawStart||!preview)return; const point=svgPoint(svg,event); const x1=Math.min(drawStart.x,point.x); const y1=Math.min(drawStart.y,point.y); const x2=Math.max(drawStart.x,point.x); const y2=Math.max(drawStart.y,point.y);
    preview.remove(); preview=null; drawStart=null;
    if(x2-x1<3||y2-y1<3){{document.getElementById('draw-message').textContent='너무 작은 영역은 추가하지 않았습니다.';return}}
    const page=Number(svg.dataset.page); const block_type=document.getElementById('new-block-type').value;
    const response=await fetch(`/documents/${{state.document_id}}/blocks`,{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{page,block_type,bbox:[x1,y1,x2,y2]}})}});
    if(!response.ok){{document.getElementById('draw-message').textContent='영역 추가 실패';return}}
    location.reload();
  }});
}});
// 화면을 열자마자 오른쪽 패널이 비어 보이지 않도록, OCR 이후 단계면 텍스트가 있는 블록을,
// 레이아웃 단계면 좌표가 있는 아무 블록이나 하나 골라 초기 선택 상태로 보여준다.
const initialBlock=state.blocks.find(item=>state.phase!=='layout_review'&&item.ocr_text)||state.blocks.find(item=>item.bbox);
if(initialBlock)selectBlock(initialBlock.block_id);
renderStatusSummary();
</script>
</body></html>"""


def _page_markup(document: ReviewDocument, page_number: int) -> str:
    """한 페이지의 이미지 위에 그 페이지에 속한 블록들을 SVG rect 오버레이로 얹은 HTML 조각을 만든다."""
    page = next(item for item in document.pages if item.page == page_number)
    rectangles: list[str] = []
    for block in document.blocks:
        if block.page != page_number or block.bbox is None:
            continue
        x1, y1, x2, y2 = block.bbox
        label = html.escape(BLOCK_LABELS.get(block.block_type, block.block_type))
        rectangles.append(
            f'<rect class="block-{block.block_type} status-{block.review_status}" '
            f'data-id="{block.block_id}" x="{x1}" y="{y1}" '
            f'width="{x2 - x1}" height="{y2 - y1}" onclick="selectBlock(\'{block.block_id}\')">'
            f"<title>{label}</title></rect>"
        )
    image_url = f"/documents/{document.document_id}/pages/{page_number}/image"
    return (
        f'<article class="page" style="max-width:{page.width}px">'
        f'<img src="{image_url}" alt="{page_number}페이지">'
        f'<span class="page-number">{page_number}</span>'
        f'<svg class="overlay" data-page="{page_number}" viewBox="0 0 {page.width} {page.height}" preserveAspectRatio="none">'
        f"{''.join(rectangles)}</svg></article>"
    )
