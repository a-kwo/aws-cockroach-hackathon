"""Publish the board: build, upload, invalidate.

    python scripts/publish_site.py            # export fixture, build, sync, invalidate
    python scripts/publish_site.py --no-export  # keep the committed fixture as-is
    python scripts/publish_site.py --dry-run    # print what would happen

`sam deploy` updates the Lambdas and nothing else. The board is static files in
S3 behind a CloudFront cache, so a deploy alone changes nothing a visitor sees —
which reads exactly like "the deploy did not work". Three steps are needed and
all three are easy to forget:

1. **Build** with the live endpoint URLs spliced in. Without them the page ships
   with no API wired up and silently falls back to its static snapshot.
2. **Sync** to the site bucket.
3. **Invalidate** CloudFront. The cache policy has a 24-hour TTL, so without
   this the old page keeps being served for the rest of the day.

Endpoint URLs and bucket names are read from the deployed CloudFormation stack
rather than hardcoded, so this keeps working after the stack is recreated.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STACK = "brasstacks"
REGION = "us-east-1"

#: Windows installs the CLI here and does not always put it on PATH for a shell
#: that was open before the installer ran.
AWS_CANDIDATES = (
    "aws",
    r"C:\Program Files\Amazon\AWSCLIV2\aws.exe",
)


def aws_binary() -> str:
    for candidate in AWS_CANDIDATES:
        try:
            subprocess.run([candidate, "--version"], capture_output=True, check=True)
            return candidate
        except (OSError, subprocess.CalledProcessError):
            continue
    raise SystemExit(
        "could not find the AWS CLI. Install it, or open a new terminal if you "
        "installed it since this one started."
    )


def stack_outputs(aws: str) -> dict[str, str]:
    result = subprocess.run(
        [aws, "cloudformation", "describe-stacks", "--stack-name", STACK,
         "--region", REGION,
         "--query", "Stacks[0].Outputs[].[OutputKey,OutputValue]",
         "--output", "text"],
        capture_output=True, text=True, check=True,
    )
    outputs = {}
    for line in result.stdout.strip().splitlines():
        if "\t" in line:
            key, value = line.split("\t", 1)
            outputs[key.strip()] = value.strip()
    return outputs


def distribution_id(aws: str) -> str:
    result = subprocess.run(
        [aws, "cloudformation", "describe-stack-resources", "--stack-name", STACK,
         "--region", REGION,
         "--query",
         "StackResources[?ResourceType=='AWS::CloudFront::Distribution']"
         ".PhysicalResourceId",
         "--output", "text"],
        capture_output=True, text=True, check=True,
    )
    value = result.stdout.strip()
    if not value:
        raise SystemExit("no CloudFront distribution in the stack")
    return value


def run(command: list[str], *, env: dict | None = None, dry: bool) -> None:
    printable = " ".join(c if " " not in c else f'"{c}"' for c in command)
    print(f"  $ {printable}")
    if dry:
        return
    subprocess.run(command, check=True, env=env)


def oauth_client_exists(aws: str) -> bool:
    """Is there a Google OAuth client for the button to point at?

    The three routes are always deployed; without a client they answer 404, and
    a Sign in with Google button that 404s is worse than no button. Nothing at
    build time can tell, so this asks Parameter Store the same question the CI
    job asks.
    """
    result = subprocess.run(
        [aws, "ssm", "get-parameter", "--name",
         "/brasstacks/GOOGLE_OAUTH_CLIENT_ID", "--region", REGION],
        capture_output=True,
    )
    return result.returncode == 0


def frontend_env(outputs: dict[str, str], *, oauth_enabled: bool) -> dict[str, str]:
    """What `build_web.py` needs, from the deployed stack's outputs.

    Kept as one pure function so it can be checked against
    `.github/workflows/deploy-frontend.yml` in a test. The two build the same
    site from the same commit, and the ways they can silently disagree are all
    quiet: `build_web` derives most endpoints from the decision endpoint, so a
    variable missing here produces a working site minus one feature rather than
    a failure anybody notices.

    Missing outputs become "" rather than absent, because "" is what `build_web`
    reads as "derive it" — and the AWS CLI prints the string "None" for an
    output that does not exist, which would otherwise be spliced into the page
    as a URL.
    """
    def value(key: str) -> str:
        raw = (outputs.get(key) or "").strip()
        return "" if raw == "None" else raw

    return {
        "PYTHONIOENCODING": "utf-8",   # every find carries an emoji
        "DECISION_API_ENDPOINT": value("DecisionEndpoint"),
        "WORKFLOW_API_ENDPOINT": value("WorkflowEndpoint"),
        "ONBOARDING_API_ENDPOINT": value("OnboardingEndpoint"),
        "LOGIN_API_ENDPOINT": value("LoginEndpoint"),
        "REGISTER_API_ENDPOINT": value("RegisterEndpoint"),
        "RUN_API_ENDPOINT": value("RunEndpoint"),
        "PROFILE_API_ENDPOINT": value("ProfileEndpoint"),
        "ADMIN_API_ENDPOINT": value("AdminWorkspacesEndpoint"),
        "GOOGLE_START_API_ENDPOINT": value("GoogleStartEndpoint"),
        "GOOGLE_COMPLETE_API_ENDPOINT": value("GoogleCompleteEndpoint"),
        # The one switch with no fallback. See oauth_client_exists.
        "GOOGLE_OAUTH_ENABLED": "1" if oauth_enabled else "",
    }


def export_commands() -> list[list[str]]:
    """The export path, in publish order: dump, scrub, prove the scrub took.

    `export_fixture.py` writes whatever tenant BRASSTACKS_BUSINESS_ID points at,
    verbatim — reviews, revenue figures, the owner's name. The committed fixture
    is only safe because `anonymise_fixture.py` rewrote it, so a fresh export
    must go through the same rewrite and then `--check` before anything reaches
    S3. If the gitignored map is absent, anonymise_fixture exits with
    instructions and the publish aborts — the safe failure. `--no-export` skips
    all three and publishes the already-scrubbed committed fixture.
    """
    anonymise = str(REPO_ROOT / "scripts" / "anonymise_fixture.py")
    return [
        [sys.executable, str(REPO_ROOT / "scripts" / "export_fixture.py")],
        [sys.executable, anonymise],
        [sys.executable, anonymise, "--check"],
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-export", action="store_true",
                        help="skip export_fixture; publish the committed fixture")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the commands without running them")
    args = parser.parse_args()

    aws = aws_binary()
    outputs = stack_outputs(aws)

    bucket = outputs.get("SiteBucketName")
    if not bucket:
        raise SystemExit("stack has no SiteBucketName output — is it deployed?")

    env = dict(os.environ)
    env.update(frontend_env(outputs, oauth_enabled=oauth_client_exists(aws)))

    if not args.no_export:
        print("\nexporting the fixture from the live cluster (then anonymising it)")
        for command in export_commands():
            run(command, env=env, dry=args.dry_run)

    print("\nbuilding the site")
    run([sys.executable, str(REPO_ROOT / "scripts" / "build_web.py")],
        env=env, dry=args.dry_run)

    print("\nuploading")
    # no-cache on the objects themselves, matching the CI job. The
    # invalidation below clears what CloudFront already holds; this stops it
    # holding the next one for 24 hours.
    run([aws, "s3", "sync", str(REPO_ROOT / "web") + "/", f"s3://{bucket}/",
         "--delete", "--cache-control", "no-cache", "--region", REGION],
        dry=args.dry_run)

    print("\ninvalidating the cache (without this the old page serves for 24h)")
    dist = distribution_id(aws)
    run([aws, "cloudfront", "create-invalidation", "--distribution-id", dist,
         "--paths", "/*"], dry=args.dry_run)

    site = outputs.get("SiteUrl", "")
    print(f"\npublished. The invalidation takes a minute or two to finish.\n  {site}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
