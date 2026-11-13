from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

TASK_QUEUE = "firstwork-compliance"
BULK_EVALUATION_BATCH_SIZE = 250

_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)


@dataclass(frozen=True)
class BulkEvaluationInput:
    job_id: int
    tenant_id: str


@dataclass(frozen=True)
class BulkBatchOutcome:
    completed: bool
    processed_contracts: int


@workflow.defn
class BulkEvaluationWorkflow:
    @workflow.run
    async def run(self, args: BulkEvaluationInput) -> None:
        try:
            await workflow.execute_activity(
                "mark_bulk_job_running",
                args,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=_RETRY_POLICY,
            )
            while True:
                batch = await workflow.execute_activity(
                    "process_bulk_evaluation_batch",
                    args,
                    result_type=BulkBatchOutcome,
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=_RETRY_POLICY,
                )
                if batch.completed:
                    return
        except Exception as exc:
            await workflow.execute_activity(
                "mark_bulk_job_failed",
                args=[args, type(exc).__name__],
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=_RETRY_POLICY,
            )
            raise
