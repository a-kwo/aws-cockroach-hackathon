# Container image, not a zip.
#
# `psycopg[binary]` ships platform-specific wheels and this repo is developed on
# Windows. A zip built here would install Windows wheels and fail on Lambda's
# manylinux runtime with an import error that points nowhere useful. Building in
# the Lambda base image gets the right wheels by construction.
#
# Build context is the repository root, not this directory:
#   docker build -f deploy/Dockerfile .

FROM public.ecr.aws/lambda/python:3.12

# Dependencies first so the layer caches across source edits. Pinned to the
# same floors as backend/pyproject.toml.
RUN pip install --no-cache-dir \
        "psycopg[binary]>=3.2" \
        "anthropic[bedrock]>=0.40" \
        "boto3>=1.35" \
        "httpx>=0.27" \
        "certifi"

# The source tree keeps its repository shape rather than being pip-installed
# into site-packages. Installing it would move `brasstacks` somewhere that
# `REPO_ROOT = parents[3]` no longer resolves correctly, which is exactly the
# kind of silent path bug that only shows up in the deployed function.
COPY backend/src ${LAMBDA_TASK_ROOT}/backend/src

# Radar reads the committed corpus. Live web search stays opt-in: the demo
# tenant is fictional, so searching for it returns trade-show videos and years-
# old market reports that pollute the memory layer and cost embedding spend.
COPY db/seed/observations.json ${LAMBDA_TASK_ROOT}/db/seed/observations.json

ENV PYTHONPATH=${LAMBDA_TASK_ROOT}/backend/src \
    BRASSTACKS_CORPUS_PATH=${LAMBDA_TASK_ROOT}/db/seed/observations.json \
    PYTHONUNBUFFERED=1

# Overridden per function by ImageConfig.Command in deploy/template.yaml.
CMD ["brasstacks.handlers.night.handler"]
