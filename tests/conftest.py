from pathlib import Path
import sys

TESTS_ROOT = Path(__file__).resolve().parent
SRC_ROOT = TESTS_ROOT.parent / "src"
for path in (SRC_ROOT, TESTS_ROOT):
    # conftest.py는 한 번만 임포트되고 이 sys.path 변경은 프로세스 전역에 남으므로,
    # tests/integration/ 등 하위 디렉터리의 테스트에서도 pdf_fixtures 같은
    # tests/ 바로 아래의 헬퍼 모듈을 별도 조치 없이 그대로 임포트할 수 있다.
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
