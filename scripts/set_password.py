"""Set (or reset) an owner's password directly in the cluster.

    python scripts/set_password.py <username> --password <new-password>

There is deliberately no password-reset flow in the product — the login page
says so — but the operator holds the database, and a forgotten password must
not orphan a workspace. This writes a fresh scrypt hash over the old one;
the old password stops working immediately, nothing else about the account
changes, and the password itself is never stored or printed back.

Needs COCKROACH_DATABASE_URL (falls back to .env, like every other script).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

from brasstacks.auth import hash_password, normalise_username  # noqa: E402
from brasstacks.config import Settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="the owner's username")
    parser.add_argument("--password", required=True,
                        help="the new password (8+ characters)")
    args = parser.parse_args()

    username = normalise_username(args.username)
    if len(args.password) < 8:
        raise SystemExit("Password must be at least 8 characters.")

    settings = Settings.load()
    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE owner_account SET password_hash = %s WHERE username = %s",
                (hash_password(args.password), username),
            )
            if cur.rowcount == 0:
                raise SystemExit(f"No account with username {username!r}.")
    print(f"Password updated for {username!r}. The old password no longer works.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
