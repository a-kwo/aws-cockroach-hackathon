"""Deploy Brass Tacks from this machine. One command.

    python scripts/deploy.py                 # site, plus the backend if it changed
    python scripts/deploy.py site            # the board only  (~40s)
    python scripts/deploy.py backend         # the Lambdas only
    python scripts/deploy.py --schedule DISABLED    # and turn the autopilot off

Deploying used to be a sequence nobody could hold in their head — build the
images, deploy the stack with three parameters quoted a very particular way,
then build the site *again* with ten endpoint variables, sync it, invalidate
CloudFront — and every one of those steps had a way to fail quietly. This is
that sequence, written down once.

The three edges it exists to blunt:

**`--parameter-overrides` re-splits on whitespace.** SAM takes one argument
holding every Key=Value pair and splits it itself, so a shell's quotes — which
it strips before SAM ever sees them — protect nothing. On 2026-08-07 that sent
`ScheduleExpression=cron(0` to CloudFormation, EventBridge rejected it, and the
whole stack update rolled back, taking every Lambda with it. The quotes here are
literal characters inside the Python string and there is no shell to strip them.

**SAM lives behind a space.** `C:\\Program Files\\Amazon\\AWSSAMCLI\\bin\\sam.cmd`
cannot be launched through Git Bash, which hands `C:\\Program` to cmd.exe.
Everything here goes through `subprocess` with a list of arguments and no shell,
where a space in a path is not a special character at all.

**`sam deploy` does not touch the site.** The board is static files in S3 behind
a 24-hour cache; a deploy alone changes nothing a visitor sees, which reads
exactly like a deploy that did not work.

And the reason it is now fast: `sam build` rebuilds sixteen container images, so
this fingerprints what actually goes into them and skips the whole backend when
nothing did. Editing `site/app.html` costs forty seconds, not eight minutes.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

STACK = "brasstacks"
REGION = "us-east-1"

#: Everything `deploy/Dockerfile` copies into the image, plus the Dockerfile and
#: the template that describes the functions. Nothing else can change what runs
#: on Lambda, so nothing else justifies a rebuild.
IMAGE_INPUTS = (
    "backend/src",
    "deploy/Dockerfile",
    "deploy/template.yaml",
    "db/seed/observations.json",
)

#: Where the last successfully deployed fingerprint is remembered. Local and
#: disposable: losing it costs one unnecessary rebuild, which is the safe way
#: for this to fail.
STAMP = REPO_ROOT / ".aws-sam" / "deployed-backend.sha"

#: Windows installers do not always put these on the PATH of a shell that was
#: open before they ran.
KNOWN_LOCATIONS = {
    "sam": [Path(r"C:\Program Files\Amazon\AWSSAMCLI\bin\sam.cmd")],
    "aws": [Path(r"C:\Program Files\Amazon\AWSCLIV2\aws.exe")],
    "docker": [Path(os.path.expanduser(
        r"~\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe"))],
}


# ---------------------------------------------------------------------------
# The parts worth testing
# ---------------------------------------------------------------------------

def overrides_argument(*, bucket: str, schedule_state: str | None,
                       schedule_expression: str | None) -> str:
    """The single `--parameter-overrides` argument, quoted the way SAM reads it.

    Every value is quoted, not only the one that currently contains a space. A
    bucket name cannot contain one today, and quoting only what needs it now is
    how this breaks again the next time a parameter grows a space.
    """
    pairs = [f'ArtifactBucket="{bucket}"']
    # Omitted rather than guessed: a deploy that is not a decision about the
    # nightly spend leaves the stack's existing schedule exactly as it is.
    if schedule_state:
        pairs.append(f'ScheduleState="{schedule_state}"')
    if schedule_expression:
        pairs.append(f'ScheduleExpression="{schedule_expression}"')
    return " ".join(pairs)


def find_executable(name: str, *, extra: list[Path] | None = None) -> str:
    """The tool's path, unquoted, for a `subprocess` argument list."""
    found = shutil.which(name)
    if found:
        return found
    for candidate in (extra if extra is not None else KNOWN_LOCATIONS.get(name, [])):
        if Path(candidate).exists():
            return str(candidate)
    raise SystemExit(
        f"could not find {name}. Install it, or open a new terminal if it was "
        f"installed since this one started."
    )


def image_fingerprint(root: Path = REPO_ROOT) -> str:
    """A hash of everything that ends up inside the Lambda image.

    `site/` is deliberately absent: the board is not in the image, and letting a
    CSS edit trigger a sixteen-image rebuild is most of what made deploying feel
    slow. `__pycache__` is absent for the opposite reason — it changes on every
    test run and is not copied in, so counting it would make every deploy look
    like a change.
    """
    digest = hashlib.sha256()
    for entry in IMAGE_INPUTS:
        target = root / entry
        files = sorted(target.rglob("*")) if target.is_dir() else [target]
        for path in files:
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            digest.update(str(path.relative_to(root)).replace("\\", "/").encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class Plan:
    deploy_backend: bool
    publish_site: bool


def plan_for(target: str, *, backend_changed: bool, force: bool = False) -> Plan:
    return Plan(
        deploy_backend=(target in {"backend", "all", "auto"}
                        and (backend_changed or force)),
        publish_site=target in {"site", "all", "auto"},
    )


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

def run(command: list[str], *, label: str, dry: bool = False) -> None:
    print(f"  $ {' '.join(command)}")
    if dry:
        return
    started = time.monotonic()
    # No shell, ever. A path containing a space is only a problem for a shell.
    result = subprocess.run(command, cwd=REPO_ROOT)
    if result.returncode != 0:
        raise SystemExit(f"{label} failed (exit {result.returncode})")
    print(f"  ({time.monotonic() - started:.0f}s)")


def capture(command: list[str]) -> str:
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def artifact_bucket(aws: str) -> str:
    value = capture([
        aws, "cloudformation", "describe-stacks", "--stack-name", STACK,
        "--region", REGION, "--query",
        "Stacks[0].Parameters[?ParameterKey=='ArtifactBucket'].ParameterValue | [0]",
        "--output", "text"])
    if not value or value == "None":
        raise SystemExit(
            "the stack has no ArtifactBucket parameter — is it deployed?")
    return value


def require_docker(docker: str) -> None:
    """Docker Desktop's daemon is usually stopped. Say so before `sam build`
    spends a minute discovering it."""
    if subprocess.run([docker, "info"], capture_output=True).returncode != 0:
        raise SystemExit(
            "the Docker daemon is not running. Start Docker Desktop and try "
            "again — sam build needs it to build the function images.")


def verify(aws: str) -> None:
    """What the deploy actually left behind. Every one of these was checked by
    hand after every deploy until it lived here."""
    print("\nverifying")
    status = capture([aws, "cloudformation", "describe-stacks", "--stack-name",
                      STACK, "--region", REGION, "--query",
                      "Stacks[0].StackStatus", "--output", "text"])
    print(f"  stack     {status or 'unknown'}")

    schedule = capture([aws, "scheduler", "get-schedule", "--name",
                        "NightFunctionNightly", "--region", REGION, "--query",
                        "[State,ScheduleExpression,ScheduleExpressionTimezone]",
                        "--output", "text"]).replace("\t", "  ")
    print(f"  nightly   {schedule or 'unknown'}")

    site = capture([aws, "cloudformation", "describe-stacks", "--stack-name",
                    STACK, "--region", REGION, "--query",
                    "Stacks[0].Outputs[?OutputKey=='SiteUrl'].OutputValue | [0]",
                    "--output", "text"])
    if site and site != "None":
        try:
            with urllib.request.urlopen(f"{site}/app/", timeout=20) as response:
                print(f"  board     {response.status} "
                      f"{response.headers.get('Last-Modified', '')}")
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"  board     unreachable ({exc})")

    if status and not status.endswith(("_COMPLETE",)):
        raise SystemExit(f"stack is {status}, not a completed state")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default="auto",
                        choices=["auto", "site", "backend", "all"],
                        help="auto (default): the site, and the backend only if "
                             "something inside the image changed")
    parser.add_argument("--schedule", choices=["ENABLED", "DISABLED"],
                        help="turn the nightly run on or off; omitted leaves "
                             "the stack's current setting alone")
    parser.add_argument("--schedule-expression",
                        help="e.g. 'cron(0 18 * * ? *)'; omitted leaves it alone")
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the suite (it is the only gate here)")
    parser.add_argument("--force", action="store_true",
                        help="deploy the backend even if nothing changed")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    started = time.monotonic()
    aws = find_executable("aws")

    fingerprint = image_fingerprint()
    previous = STAMP.read_text(encoding="utf-8").strip() if STAMP.exists() else ""
    plan = plan_for(args.target, backend_changed=fingerprint != previous,
                    force=args.force)

    print(f"deploying: {args.target}")
    if args.target in {"auto", "backend"} and not plan.deploy_backend:
        print("  backend   unchanged since the last deploy — skipping the image "
              "build (--force to override)")

    if not args.skip_tests:
        print("\nrunning the suite (the gate CI used to be)")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        started_tests = time.monotonic()
        if not args.dry_run and subprocess.run(
                [sys.executable, "-m", "pytest", "backend/tests", "-q"],
                cwd=REPO_ROOT, env=env).returncode != 0:
            raise SystemExit("tests failed — nothing deployed")
        print(f"  ({time.monotonic() - started_tests:.0f}s)")

    if plan.deploy_backend:
        sam = find_executable("sam")
        require_docker(find_executable("docker"))

        print("\nbuilding the function images")
        run([sam, "build", "--template", "deploy/template.yaml",
             # Cached and parallel: the sixteen functions share one Dockerfile,
             # so after the first build this is mostly layer reuse.
             "--cached", "--parallel"], label="sam build", dry=args.dry_run)

        print("\ndeploying the stack")
        run([sam, "deploy",
             "--template-file", ".aws-sam/build/template.yaml",
             "--stack-name", STACK, "--region", REGION,
             "--capabilities", "CAPABILITY_IAM",
             "--resolve-image-repos", "--resolve-s3",
             "--no-confirm-changeset", "--no-fail-on-empty-changeset",
             "--parameter-overrides", overrides_argument(
                 bucket=artifact_bucket(aws),
                 schedule_state=args.schedule,
                 schedule_expression=args.schedule_expression)],
            label="sam deploy", dry=args.dry_run)

        if not args.dry_run:
            STAMP.parent.mkdir(parents=True, exist_ok=True)
            STAMP.write_text(fingerprint, encoding="utf-8")

    if plan.publish_site:
        print("\npublishing the board")
        # Reused rather than reimplemented: publish_site already reads the
        # endpoints out of the stack and knows the Google OAuth switch has no
        # fallback. Two copies of that would drift.
        run([sys.executable, str(REPO_ROOT / "scripts" / "publish_site.py"),
             "--no-export"] + (["--dry-run"] if args.dry_run else []),
            label="publish_site", dry=False)

    if not args.dry_run:
        verify(aws)
    print(f"\ndone in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
