from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("directory", "0002_worker_tenant_constraint")]

    # Backfill these nullable staging columns before applying the next migration.
    operations = [
        migrations.RenameField(model_name="worker", old_name="display_name", new_name="name"),
        migrations.AddField(model_name="worker", name="date_of_birth", field=models.DateField(null=True)),
        migrations.AddField(
            model_name="worker", name="nationality",
            field=models.CharField(max_length=2, null=True),
        ),
        migrations.AddField(model_name="company", name="industry", field=models.CharField(max_length=200, null=True)),
        migrations.AddField(
            model_name="company", name="country",
            field=models.CharField(max_length=2, null=True),
        ),
        migrations.AddField(
            model_name="company", name="bank_deposit",
            field=models.DecimalField(decimal_places=2, max_digits=18, null=True),
        ),
        migrations.AddField(model_name="contract", name="start_date", field=models.DateTimeField(null=True)),
        migrations.AddField(model_name="contract", name="end_date", field=models.DateTimeField(null=True)),
        migrations.AddField(
            model_name="contract", name="country",
            field=models.CharField(max_length=2, null=True),
        ),
    ]
