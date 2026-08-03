# v27 file manifest

This drop-in update intentionally does **not** contain `.github/workflows/*` and will not replace the repository's existing deployment workflows or credentials.

## Application and task platform

- `.env.example`
- `README.md`
- `backend/src/brasstacks/agents/maker.py`
- `backend/src/brasstacks/handlers/ask.py`
- `backend/src/brasstacks/handlers/decision.py`
- `backend/src/brasstacks/handlers/maker.py`
- `backend/src/brasstacks/handlers/maker_email.py`
- `backend/src/brasstacks/handlers/task_reconciler.py`
- `backend/src/brasstacks/handlers/task_starter.py`
- `backend/src/brasstacks/maker_dispatch.py`
- `backend/src/brasstacks/repository.py`
- `backend/src/brasstacks/repository_pg.py`
- `backend/src/brasstacks/task_schema.py`
- `backend/src/brasstacks/tasks.py`
- `backend/src/brasstacks/tools/__init__.py`
- `backend/src/brasstacks/tools/base.py`
- `backend/src/brasstacks/tools/email.py`
- `backend/src/brasstacks/workflow_snapshot.py`
- `db/schema.sql`
- `deploy/README.md`
- `deploy/maker_workflow.asl.json`
- `deploy/template.yaml`
- `scripts/build_web.py`
- `scripts/export_fixture.py`
- `site/app.html`

## Documentation

- `docs/BACKEND_DESIGN.md`
- `docs/MULTI_TENANT_AGENT_PLATFORM.md`
- `docs/V27_IMPLEMENTATION_NOTES.md`
- `docs/V27_FILE_MANIFEST.md`

## Tests

- `backend/tests/test_ask_handler_memory.py`
- `backend/tests/test_decision_handler.py`
- `backend/tests/test_maker_email.py`
- `backend/tests/test_maker_handler.py`
- `backend/tests/test_site_build.py`
- `backend/tests/test_task_reconciler.py`
- `backend/tests/test_task_starter.py`
- `backend/tests/test_tasks.py`
- `backend/tests/test_workflow_snapshot.py`

Validated before packaging:

```text
656 passed, 65 deselected
web build passed
Python compilation passed
SAM YAML parsed
Step Functions ASL parsed
browser JavaScript syntax passed
```
