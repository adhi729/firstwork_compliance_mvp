from django.conf import settings
from temporalio.client import Client

from workflows.bulk_evaluation import TASK_QUEUE, BulkEvaluationInput, BulkEvaluationWorkflow


async def start_bulk_evaluation(job_id: int, tenant_id: str) -> None:
    address = settings.TEMPORAL_ADDRESS
    if not address:
        raise RuntimeError("TEMPORAL_ADDRESS must be set")
    client = await Client.connect(
        address,
        namespace=settings.TEMPORAL_NAMESPACE,
    )
    await client.start_workflow(
        BulkEvaluationWorkflow.run,
        BulkEvaluationInput(job_id=job_id, tenant_id=tenant_id),
        id=f"bulk-evaluation-{job_id}",
        task_queue=TASK_QUEUE,
    )
