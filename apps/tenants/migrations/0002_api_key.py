from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="APIKey",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("key_hash", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=100)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="tenants.tenant")),
            ],
            options={"db_table": "tenants_api_key"},
        ),
        migrations.AddIndex(
            model_name="apikey",
            index=models.Index(fields=["tenant", "active"], name="api_key_tenant_active_idx"),
        ),
        migrations.RunSQL(
            sql=[
                "ALTER TABLE tenants_api_key ENABLE ROW LEVEL SECURITY",
                "ALTER TABLE tenants_api_key FORCE ROW LEVEL SECURITY",
                """
                CREATE POLICY tenant_isolation ON tenants_api_key
                USING (tenant_id::text = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
                """,
            ],
            reverse_sql=[
                "DROP POLICY tenant_isolation ON tenants_api_key",
                "ALTER TABLE tenants_api_key NO FORCE ROW LEVEL SECURITY",
                "ALTER TABLE tenants_api_key DISABLE ROW LEVEL SECURITY",
            ],
        ),
    ]
