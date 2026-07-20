"""`python -m paperrag.collect`로 실행할 때 사용되는 모듈 진입점. 실제 로직은 `cli.py`의 `main`에 있다."""

from paperrag.collect.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
