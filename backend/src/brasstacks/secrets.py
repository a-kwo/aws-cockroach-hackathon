"""Getting secrets into the environment on Lambda.

`config.py` resolves settings from `os.environ`, falling back to a repo-root
`.env`. A deployed Lambda has no `.env`, so something has to put the four real
secrets into the environment before `Settings.load()` runs. That is this module,
and it is the whole of it.

Two deliberate properties:

* **The real environment wins.** Values already set are never overwritten, which
  matches `Settings.load()` exactly. A stale parameter must not be able to
  silently override an explicitly exported one.
* **No prefix configured means no-op.** Local development, the test suite, and
  anyone running the harness from a laptop never touch AWS.

The parameters are SecureString rather than baked into the image or into Lambda
environment variables, where they would be readable from the console.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from typing import Any

#: Set on the Lambda to point at the parameter path, e.g. `/brasstacks`.
SSM_PREFIX_VAR = "BRASSTACKS_SSM_PREFIX"


def hydrate_environment(
    *,
    env: MutableMapping[str, str] | None = None,
    client: Any = None,
    prefix: str | None = None,
) -> int:
    """Copy SSM parameters under `prefix` into `env`. Returns how many landed.

    `/brasstacks/ANTHROPIC_API_KEY` becomes `ANTHROPIC_API_KEY`.
    """
    target = os.environ if env is None else env
    path = prefix if prefix is not None else target.get(SSM_PREFIX_VAR)

    if not path:
        return 0

    if client is None:  # pragma: no cover - requires AWS
        import boto3

        client = boto3.client("ssm")

    try:
        response = client.get_parameters_by_path(
            Path=path,
            Recursive=True,
            WithDecryption=True,
        )
    except Exception as e:
        raise RuntimeError(
            f"could not read SSM parameters under {path!r} "
            f"({type(e).__name__}: {e}). The function's role needs "
            "ssm:GetParametersByPath and kms:Decrypt."
        ) from e

    loaded = 0
    for parameter in response.get("Parameters", []):
        name = parameter["Name"].rsplit("/", 1)[-1]
        # setdefault, not assignment: an explicitly set variable outranks
        # whatever is in Parameter Store, same rule as Settings.load().
        if name not in target:
            target[name] = parameter["Value"]
            loaded += 1

    return loaded
