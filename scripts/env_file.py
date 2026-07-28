"""Surgical .env editing.

Hand-editing .env in a text editor is how connection strings get lost: one
select-all and the file is gone, along with any generated password that exists
nowhere else. These helpers change exactly one key and leave every other line —
comments included — byte-identical.

Use as a library or from the shell:

    python scripts/env_file.py get COCKROACH_DATABASE_URL --mask
    python scripts/env_file.py set S3_ARTIFACT_BUCKET my-bucket
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
TEMPLATE_PATH = REPO_ROOT / ".env.example"


def ensure_env(path: Path = ENV_PATH) -> Path:
    """Create .env from the template if it does not exist. Never overwrites."""
    if not path.exists():
        path.write_text(TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def read_all(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def get_key(key: str, path: Path = ENV_PATH) -> str | None:
    return read_all(path).get(key)


def set_key(key: str, value: str, path: Path = ENV_PATH) -> None:
    """Replace the value of `key`, preserving all other lines exactly.

    Appends the key if absent. Only the first assignment of a key is rewritten,
    so a commented example line mentioning the same name is left alone.
    """
    ensure_env(path)
    lines = path.read_text(encoding="utf-8").splitlines()

    replaced = False
    out: list[str] = []
    for line in lines:
        if not replaced and re.match(rf"^\s*{re.escape(key)}\s*=", line):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def mask(value: str) -> str:
    """Mask secrets for display: passwords inside URLs, and bare secrets."""
    if "://" in value:
        return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", value)
    if len(value) <= 8:
        return "***" if value else ""
    return f"{value[:4]}...{value[-2:]} ({len(value)} chars)"


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("get", help="print one value")
    g.add_argument("key")
    g.add_argument("--mask", action="store_true", help="mask secrets in output")

    s = sub.add_parser("set", help="set one value, preserving the rest of the file")
    s.add_argument("key")
    s.add_argument("value")

    sub.add_parser("keys", help="list configured keys (values never printed)")

    args = parser.parse_args()

    if args.command == "get":
        value = get_key(args.key)
        if value is None:
            print(f"{args.key} is not set", file=sys.stderr)
            return 1
        print(mask(value) if args.mask else value)
    elif args.command == "set":
        set_key(args.key, args.value)
        print(f"{args.key} updated in {ENV_PATH}")
    elif args.command == "keys":
        values = read_all()
        for key in values:
            state = "set" if values[key] else "empty"
            print(f"  {key:32} {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
