"""SQS redelivery converges on one Step Functions execution."""

import json

from brasstacks.handlers.task_starter import start_records


class FakeStates:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.calls = []

    def start_execution(self, **kwargs):
        self.calls.append(kwargs)
        error = self.failures.get(kwargs["name"])
        if error:
            raise error
        return {"executionArn": f"arn:execution:{kwargs['name']}"}


class AwsError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def record(message_id, *, name="task-1-d1"):
    return {
        "messageId": message_id,
        "body": json.dumps({
            "task_id": "task-1",
            "business_id": "business-1",
            "execution_name": name,
            "dispatch_count": 1,
        }),
    }


def test_valid_fifo_message_starts_the_named_standard_workflow():
    client = FakeStates()
    result = start_records(
        {"Records": [record("m1")]}, client=client,
        state_machine_arn="arn:state-machine:maker",
    )

    assert result == {"batchItemFailures": []}
    assert client.calls == [{
        "stateMachineArn": "arn:state-machine:maker",
        "name": "task-1-d1",
        "input": json.dumps({
            "business_id": "business-1",
            "dispatch_count": 1,
            "execution_name": "task-1-d1",
            "task_id": "task-1",
        }, separators=(",", ":"), sort_keys=True),
    }]


def test_execution_already_exists_is_an_acknowledged_duplicate():
    client = FakeStates({"task-1-d1": AwsError("ExecutionAlreadyExists")})
    result = start_records(
        {"Records": [record("m1")]}, client=client,
        state_machine_arn="arn:state-machine:maker",
    )

    assert result == {"batchItemFailures": []}


def test_only_failed_messages_are_returned_for_partial_batch_retry():
    client = FakeStates({"task-2-d1": AwsError("AccessDeniedException")})
    result = start_records(
        {"Records": [record("m1"), record("m2", name="task-2-d1")]},
        client=client, state_machine_arn="arn:state-machine:maker",
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "m2"}]}


def test_invalid_message_is_retried_instead_of_silently_dropped():
    result = start_records(
        {"Records": [{"messageId": "bad", "body": "{}"}]},
        client=FakeStates(), state_machine_arn="arn:state-machine:maker",
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
