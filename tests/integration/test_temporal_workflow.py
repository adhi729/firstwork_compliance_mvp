import asyncio

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from workflows.bulk_evaluation import (
    TASK_QUEUE,
    BulkBatchOutcome,
    BulkEvaluationInput,
    BulkEvaluationWorkflow,
)


def test_bulk_workflow_retries_a_failed_batch_and_continues() -> None:
    async def run_workflow_test() -> None:
        running_calls = 0
        batch_attempts = 0
        completed_batches: list[int] = []

        @activity.defn(name="mark_bulk_job_running")
        async def mark_running(args: BulkEvaluationInput) -> None:
            nonlocal running_calls
            assert args == BulkEvaluationInput(job_id=17, tenant_id="tenant-17")
            running_calls += 1

        @activity.defn(name="process_bulk_evaluation_batch")
        async def process_batch(args: BulkEvaluationInput) -> BulkBatchOutcome:
            nonlocal batch_attempts
            assert args == BulkEvaluationInput(job_id=17, tenant_id="tenant-17")
            batch_attempts += 1
            if batch_attempts == 1:
                raise ApplicationError("Transient batch failure")
            completed_batches.append(batch_attempts)
            return BulkBatchOutcome(
                completed=batch_attempts == 3,
                processed_contracts=2,
            )

        @activity.defn(name="mark_bulk_job_failed")
        async def mark_failed(args: BulkEvaluationInput, failure_type: str) -> None:
            pytest.fail(f"Successful retry should not fail job: {failure_type}")

        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with Worker(
                environment.client,
                task_queue=TASK_QUEUE,
                workflows=[BulkEvaluationWorkflow],
                activities=[mark_running, process_batch, mark_failed],
            ):
                await environment.client.execute_workflow(
                    BulkEvaluationWorkflow.run,
                    BulkEvaluationInput(job_id=17, tenant_id="tenant-17"),
                    id="bulk-evaluation-retry-test",
                    task_queue=TASK_QUEUE,
                )

        assert running_calls == 1
        assert batch_attempts == 3
        assert completed_batches == [2, 3]

    asyncio.run(run_workflow_test())
