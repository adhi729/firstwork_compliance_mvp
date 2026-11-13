from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("evaluation", "0004_evaluationjob_evaluationjobresult_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="evaluationjob",
            name="last_contract_id",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name="evaluationjob",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["PENDING", "RUNNING", "COMPLETED", "FAILED"])
                ),
                name="job_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="evaluationjob",
            constraint=models.CheckConstraint(
                condition=models.Q(("last_contract_id__lte", models.F("max_contract_id"))),
                name="job_cursor_lte_max",
            ),
        ),
    ]
