"""`python -m paperrag.ingest`로 실행할 때의 진입점.

실제 인자 파싱과 파이프라인 실행 로직은 전부 `cli.py`의 `main()`에 있으며,
이 파일은 표준 `__main__` 규약에 따라 그것을 호출하고 종료 코드를 반환하기만 한다.
"""

from paperrag.ingest.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
