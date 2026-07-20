-- 키워드별 "기본 뷰"(섹션 필터 없음, 연관 논문·표 포함) 검색 결과 캐시.
-- 목적: 같은 키워드로 검색이 반복될 때마다 대표/연관 논문 점수를 다시 계산하고
-- 엑셀을 처음부터 다시 만드는 대신, 이미 만들어둔 결과를 즉시 재사용하기 위함.
-- primary_paper/related_paper는 PaperSummary(점수·선정 사유 포함)를 그대로 JSON으로
-- 저장한다 — 점수 재계산(대표 논문 선정 로직)이 캐시로 건너뛰고 싶은 비용이므로,
-- paper_id만 저장하고 다시 조회하는 방식으로는 목적을 달성할 수 없기 때문이다.
-- keyword_id가 삭제되면(이론상 발생하지 않지만) 캐시 행도 함께 정리한다.
CREATE TABLE keyword_result_cache (
    keyword_id BIGINT PRIMARY KEY REFERENCES keywords(keyword_id) ON DELETE CASCADE,
    result_id TEXT NOT NULL,
    excel_path TEXT NOT NULL,
    primary_paper JSONB NOT NULL,
    related_paper JSONB,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
