"""Strip the real tenant's identity out of the built fixture.

    python scripts/anonymise_fixture.py            # rewrite db/fixtures/demo.json
    python scripts/anonymise_fixture.py --check     # exit 1 if anything survived

The demo board is built from a real restaurant's data: its reviews, its
competitors, its listings, and an agent's revenue advice about it. The finds,
the retrieval scores and the evidence are all genuine and stay that way — what
goes is the identity.

**The map lives outside the repository, and that is the point.** An earlier
version of this file held the business name, street address and phone number as
literals, which published in a public repo exactly what it existed to remove.
The patterns now come from `.anonymise-map.json`, which is gitignored like
`.env`; `.anonymise-map.example.json` documents the shape with dummy values.

Replacement order is load-bearing: the longest form of each identifier has to go
first, or the alias inherits the original's shape ("Foo Japanese Cuisine"
becoming "Alias Japanese Cuisine" still tells you the original had two extra
words).

**This is not anonymity, and the README must not claim it is.** Verbatim review
sentences are still searchable, and a determined reader who pastes one into a
search engine will find the restaurant. What this buys is that the repository,
the fixture and the rendered page do not themselves name it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "db" / "fixtures" / "demo.json"
MAP_PATH = REPO_ROOT / ".anonymise-map.json"
EXAMPLE_PATH = REPO_ROOT / ".anonymise-map.example.json"


class MapMissing(SystemExit):
    """Raised, with instructions, when the local map is absent."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"no anonymisation map at {path.name}.\n"
            f"Copy {EXAMPLE_PATH.name} to {path.name} and fill in the real "
            "identifiers. It is gitignored — the real values must never be "
            "committed, which is the whole reason this file is separate."
        )


def load_map(path: Path | None = None) -> dict:
    """The replacement map, from the local file.

    Kept as a plain dict rather than module constants so the tests can pass a
    synthetic map and never need the real one.
    """
    path = path or MAP_PATH
    if not path.exists():
        raise MapMissing(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("replacements", "forbidden", "alias"):
        if key not in data:
            raise SystemExit(f"{path.name} is missing its {key!r} section")
    return data


def redact(text: str, mapping: dict) -> str:
    """Every replacement, in the order the map lists them. Safe to run twice."""
    for pattern, replacement in mapping["replacements"]:
        text = re.sub(pattern, replacement, text)
    return text


def scrub(value, mapping: dict):
    """Walk the model, redacting every string it holds.

    Keys are redacted too: a find id is a UUID, but an observation's source name
    is free text and has carried the host name.
    """
    if isinstance(value, str):
        return redact(value, mapping)
    if isinstance(value, list):
        return [scrub(v, mapping) for v in value]
    if isinstance(value, dict):
        return {redact(k, mapping) if isinstance(k, str) else k: scrub(v, mapping)
                for k, v in value.items()}
    return value


def anonymise(model: dict, mapping: dict) -> dict:
    """The scrubbed model, with the identity fields set rather than patched.

    `business.city` held the full street address rather than a city, so a regex
    alone would leave a plausible-looking address behind. These are assigned.
    """
    out = scrub(model, mapping)
    alias = mapping["alias"]
    business = out.get("business")
    if isinstance(business, dict):
        business["name"] = alias["name"]
        business["city"] = alias["city"]
    owner = out.get("owner")
    if isinstance(owner, dict):
        owner["username"] = alias.get("username", "owner")
        owner["display_name"] = alias["name"]
        if owner.get("email"):
            owner["email"] = alias.get("email", "owner@example.com")
    return out


def survivors(raw: str, mapping: dict) -> list[str]:
    """Which forbidden identifiers are still in the text."""
    return [f"{label} ({len(re.findall(pattern, raw))}x)"
            for label, pattern in mapping["forbidden"]
            if re.search(pattern, raw)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify the fixture is clean; change nothing")
    args = parser.parse_args()

    mapping = load_map()
    raw = FIXTURE.read_text(encoding="utf-8")

    if args.check:
        left = survivors(raw, mapping)
        if left:
            print("fixture still names the real business: " + ", ".join(left))
            return 1
        print("fixture is clean")
        return 0

    model = anonymise(json.loads(raw), mapping)
    out = json.dumps(model, indent=2, ensure_ascii=False) + "\n"
    FIXTURE.write_text(out, encoding="utf-8")

    left = survivors(out, mapping)
    if left:
        print("FAILED — identifiers survived: " + ", ".join(left))
        return 1
    alias = mapping["alias"]
    print(f"anonymised {FIXTURE.relative_to(REPO_ROOT)}")
    print(f"  business  {alias['name']} · {alias['city']}")
    print(f"  {len(model.get('finds') or [])} finds, "
          f"{(model.get('corpus') or {}).get('observations')} observations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
