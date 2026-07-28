"""Create or rotate the CockroachDB SQL user and write the connection string.

The generated password is written straight into .env and never printed, never
passed on a visible command line, and never echoed in error output. If the
connection string is lost, re-run this: it rotates the password rather than
requiring you to recover the old one.

    python scripts/provision_db_user.py

Requires `ccloud auth login` first.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env_file import ENV_PATH, ensure_env, get_key, mask, set_key  # noqa: E402

DEFAULT_USER = "brasstacks_app"
DEFAULT_DATABASE = "defaultdb"
CERT_DIR = Path(__file__).resolve().parent.parent / "cockroach-certs"


def ccloud_path() -> Path:
    """Locate the ccloud binary, which is often not on a running shell's PATH."""
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "ccloud" / "ccloud.exe")
    candidates += [Path("/usr/local/bin/ccloud"), Path.home() / ".local/bin/ccloud"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path("ccloud")  # fall back to PATH resolution


def run_ccloud(args: list[str], secret: str | None = None) -> str:
    """Run ccloud, scrubbing `secret` from any output before it is surfaced."""
    result = subprocess.run(
        [str(ccloud_path()), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",  # ccloud's spinner emits bytes cp1252 cannot decode
    )
    if result.returncode != 0:
        err = result.stderr or result.stdout
        if secret:
            err = err.replace(secret, "***")
        raise SystemExit(f"ccloud {' '.join(args[:3])} failed:\n{err}")
    return result.stdout


def cluster_host(cluster: str) -> str:
    info = json.loads(run_ccloud(["cluster", "info", cluster, "--output", "json", "-q"]))
    host = info.get("sql_dns")
    if not host:
        raise SystemExit(f"cluster {cluster!r} has no sql_dns; is it still provisioning?")
    return host


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=None,
                        help="cluster name (default: CCLOUD_CLUSTER_NAME from .env)")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    args = parser.parse_args()

    ensure_env()
    cluster = args.cluster or get_key("CCLOUD_CLUSTER_NAME") or "brasstacks"

    ca_path = CERT_DIR / "ca.crt"
    if not ca_path.exists():
        raise SystemExit(
            f"Cluster CA not found at {ca_path}.\n"
            "Download it first (the cluster id is in `ccloud cluster list --output json`):\n"
            "  curl --create-dirs -o cockroach-certs/ca.crt \\\n"
            "    https://cockroachlabs.cloud/clusters/<cluster-id>/cert"
        )

    host = cluster_host(cluster)
    password = secrets.token_urlsafe(24)

    users = json.loads(run_ccloud(
        ["cluster", "user", "list", cluster, "--output", "json", "-q"]))
    already_exists = any(u.get("name") == args.user for u in users)

    # `create` 409s on an existing user; `password` rotates it in place.
    verb = "password" if already_exists else "create"
    run_ccloud(["cluster", "user", verb, cluster, args.user, "-p", password, "-q"],
               secret=password)

    url = (
        f"postgresql://{args.user}:{password}@{host}:26257/{args.database}"
        f"?sslmode=verify-full&sslrootcert={ca_path.as_posix()}"
    )
    set_key("COCKROACH_DATABASE_URL", url)
    set_key("CCLOUD_CLUSTER_NAME", cluster)

    action = "rotated password for" if already_exists else "created"
    print(f"{action} SQL user {args.user!r} on cluster {cluster!r}")
    print(f"COCKROACH_DATABASE_URL written to {ENV_PATH}")
    print(f"  {mask(url)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
