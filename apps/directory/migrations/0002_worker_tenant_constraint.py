from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="worker",
            constraint=models.UniqueConstraint(fields=("id", "tenant"), name="worker_id_tenant_uniq"),
        ),
        migrations.RunSQL(
            sql="""
                ALTER TABLE directory_contract
                ADD CONSTRAINT contract_worker_same_tenant_fk
                FOREIGN KEY (worker_id, tenant_id)
                REFERENCES directory_worker (id, tenant_id)
            """,
            reverse_sql=(
                "ALTER TABLE directory_contract "
                "DROP CONSTRAINT contract_worker_same_tenant_fk"
            ),
        ),
    ]
