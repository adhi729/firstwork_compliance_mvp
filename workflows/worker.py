"""Temporal worker for durable compliance workflows."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import django
from temporalio.client import Client
from temporalio.worker import Worker

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from workflows.activities import (  # noqa: E402
    mark_bulk_job_failed,
    mark_bulk_job_running,
    process_bulk_evaluation_batch,
)
from workflows.bulk_evaluation import TASK_QUEUE, BulkEvaluationWorkflow  # noqa: E402


async def main() -> None:
    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"],
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    activity_executor = ThreadPoolExecutor(max_workers=10)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[BulkEvaluationWorkflow],
        activities=[
            mark_bulk_job_running,
            process_bulk_evaluation_batch,
            mark_bulk_job_failed,
        ],
        activity_executor=activity_executor,
    )
    try:
        await worker.run()
    finally:
        activity_executor.shutdown(wait=True)


if __name__ == "__main__":
    asyncio.run(main())
