import html
import json

from paperrag.ingest.models import BLOCK_TYPES
from paperrag.review.models import ReviewDocument

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
    options = "".join(
        f'<option value="{name}">{html.escape(BLOCK_LABELS.get(name, name))}</option>'
        for name in sorted(BLOCK_TYPES)
    )
    present_types = sorted({block.block_type for block in document.blocks})
    block_styles = "".join(
        f".overlay rect.block-{block_type}"
        f"{{fill:{BLOCK_COLORS.get(block_type, '#1769e0')}26;"
        f"stroke:{BLOCK_COLORS.get(block_type, '#1769e0')}}}"
        for block_type in present_types
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
      <label>영역 좌표 (x1, y1, x2, y2)</label><div><input id="x1" type="number" step="0.1"><input id="y1" type="number" step="0.1"><input id="x2" type="number" step="0.1"><input id="y2" type="number" step="0.1"></div></div>
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
const state=JSON.parse(document.getElementById('review-data').textContent); let current=null; let drawMode=false; let editMode=false; let drawStart=null; let preview=null; let editStart=null;
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
  document.getElementById('review-status').value=current.review_status;
  document.getElementById('message').textContent='';
  renderEditHandles();
}}
async function saveBlock(event){{
  event.preventDefault(); if(!current)return;
  const bbox=['x1','y1','x2','y2'].map(id=>Number(document.getElementById(id).value));
  const body={{block_type:document.getElementById('block-type').value,bbox,review_status:document.getElementById('review-status').value}};
  if(state.phase!=='layout_review')body.corrected_text=document.getElementById('corrected-text').value;
  const response=await fetch(`/documents/${{state.document_id}}/blocks/${{current.block_id}}`,{{method:'PUT',headers:{{'content-type':'application/json'}},body:JSON.stringify(body)}});
  if(!response.ok){{document.getElementById('message').textContent='저장 실패';return}}
  Object.assign(current,body); document.getElementById('message').textContent='저장했습니다.';
}}
async function deleteCurrentBlock(){{
  if(!current||state.phase!=='layout_review')return;
  const label=state.labels[current.block_type]||current.block_type;
  if(!confirm(`${{label}} · ${{current.block_id}} 영역을 삭제하시겠습니까? 이 영역은 OCR 입력에서도 제거됩니다.`))return;
  const response=await fetch(`/documents/${{state.document_id}}/blocks/${{current.block_id}}`,{{method:'DELETE'}});
  if(!response.ok){{document.getElementById('message').textContent='영역 삭제 실패';return}}
  location.reload();
}}
function toggleDrawMode(){{
  if(state.phase!=='layout_review')return;
  if(editMode)toggleEditMode();
  drawMode=!drawMode; drawStart=null;
  document.querySelectorAll('.overlay').forEach(svg=>svg.classList.toggle('drawing',drawMode));
  document.getElementById('draw-toggle').textContent=drawMode?'박스 그리기 취소':'페이지에서 박스 그리기 시작';
  document.getElementById('draw-message').textContent=drawMode?'페이지의 누락 영역을 드래그하세요.':'';
}}
function toggleEditMode(){{
  if(state.phase!=='layout_review')return;
  if(drawMode)toggleDrawMode();
  editMode=!editMode; editStart=null;
  document.querySelectorAll('.overlay').forEach(svg=>svg.classList.toggle('editing',editMode));
  document.getElementById('edit-toggle').textContent=editMode?'박스 수정 모드 종료':'선택 박스 이동·크기 조절';
  document.getElementById('draw-message').textContent=editMode?'박스를 드래그해 이동하거나 모서리 점을 드래그해 크기를 바꾼 뒤 저장하세요.':'';
  renderEditHandles();
}}
function clearEditHandles(){{document.querySelectorAll('.resize-handle').forEach(handle=>handle.remove())}}
function renderEditHandles(){{
  clearEditHandles(); if(!editMode||!current)return;
  const rect=document.querySelector(`rect[data-id="${{current.block_id}}"]`); if(!rect)return; const svg=rect.ownerSVGElement;
  const x=Number(rect.getAttribute('x')); const y=Number(rect.getAttribute('y')); const width=Number(rect.getAttribute('width')); const height=Number(rect.getAttribute('height'));
  for(const [corner,cx,cy] of [['nw',x,y],['ne',x+width,y],['sw',x,y+height],['se',x+width,y+height]]){{
    const handle=document.createElementNS('http://www.w3.org/2000/svg','circle'); handle.setAttribute('class','resize-handle'); handle.dataset.corner=corner; handle.dataset.id=current.block_id; handle.setAttribute('cx',cx); handle.setAttribute('cy',cy); handle.setAttribute('r','5'); svg.appendChild(handle);
  }}
}}
function svgPoint(svg,event){{
  const point=svg.createSVGPoint(); point.x=event.clientX; point.y=event.clientY;
  return point.matrixTransform(svg.getScreenCTM().inverse());
}}
document.querySelectorAll('.overlay').forEach(svg=>{{
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
const initialBlock=state.blocks.find(item=>state.phase!=='layout_review'&&item.ocr_text)||state.blocks.find(item=>item.bbox);
if(initialBlock)selectBlock(initialBlock.block_id);
</script>
</body></html>"""


def _page_markup(document: ReviewDocument, page_number: int) -> str:
    page = next(item for item in document.pages if item.page == page_number)
    rectangles: list[str] = []
    for block in document.blocks:
        if block.page != page_number or block.bbox is None:
            continue
        x1, y1, x2, y2 = block.bbox
        label = html.escape(BLOCK_LABELS.get(block.block_type, block.block_type))
        rectangles.append(
            f'<rect class="block-{block.block_type}" data-id="{block.block_id}" x="{x1}" y="{y1}" '
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
