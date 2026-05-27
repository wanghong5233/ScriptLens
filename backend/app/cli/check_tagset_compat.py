from __future__ import annotations

import argparse
import json
import sys

from service.tag_registry.compat_check import check_tagset_compatibility


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check tag-set compatibility across versions.")
    parser.add_argument("--baseline", required=True, help="baseline tag_set_ver, e.g. v0.1.0")
    parser.add_argument("--candidate", required=True, help="candidate tag_set_ver, e.g. v1.0.0")
    parser.add_argument("--mode", default="BACKWARD", help="compat mode: BACKWARD|FORWARD|FULL|NONE")
    parser.add_argument("--breaking", action="store_true", help="allow breaking changes explicitly")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = check_tagset_compatibility(
        args.baseline,
        args.candidate,
        mode=args.mode,
        breaking=args.breaking,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if not result.compatible:
        sys.exit(1)


if __name__ == "__main__":
    main()

