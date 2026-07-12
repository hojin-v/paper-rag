import json

from paperrag.config import get_settings
from paperrag.readiness import build_readiness_report


def main() -> int:
    report = build_readiness_report(get_settings())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
