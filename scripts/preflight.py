"""운영 배포 전 구성 점검(preflight) 스크립트.

`paperrag.readiness.build_readiness_report`가 정의한 순서대로 다음을 확인한다.
1) 전체 OCR 정책(paddle backend 고정), 임베딩 정책(hash 차단)과 차원, 대체 결과 차단 정책
2) pypdfium2·paddle·paddleocr 모듈이 실제로 import 되는지
3) 레이아웃·텍스트 검출·인식(및 표 인식 사용 시 분류/유선/무선) 로컬 모델 디렉터리 존재 여부
4) (옵션) 실제 PostgreSQL 연결, BGE-M3 임베딩 서비스 health, Ollama 모델 존재 여부

docs/guide/10-production-readiness.md의 "2단계: 운영 정책과 준비 상태 확인"에서 이 스크립트를
`with_paddle_runtime.sh`로 감싸 실행하도록 안내한다. `/ready` API 엔드포인트도 동일한 리포트를 사용한다.
"""

import json

from paperrag.config import get_settings
from paperrag.readiness import build_readiness_report


def main() -> int:
    """구성 점검 리포트를 JSON으로 출력하고, 하나라도 error 상태면 실패(exit code 1)로 종료한다.

    warning은 실패로 취급하지 않는다(예: 모델 경로 미지정이지만 Paddle 캐시에 사전학습 모델이
    있을 수 있는 경우). exit code 0은 "논문 처리에 필요한 구성이 전부 준비됨"만을 의미하며,
    실제 OCR 정확도나 처리량 합격을 보장하지 않는다.
    """
    report = build_readiness_report(get_settings())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
