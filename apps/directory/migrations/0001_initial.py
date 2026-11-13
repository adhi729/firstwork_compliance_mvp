from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Company",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="tenants.tenant")),
            ],
            options={"db_table": "directory_company"},
        ),
        migrations.CreateModel(
            name="Worker",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("display_name", models.CharField(max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="tenants.tenant")),
            ],
            options={"db_table": "directory_worker"},
        ),
        migrations.CreateModel(
            name="Contract",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="directory.company")),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="tenants.tenant")),
                ("worker", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="directory.worker")),
            ],
            options={"db_table": "directory_contract"},
        ),
        migrations.AddConstraint(
            model_name="company",
            constraint=models.UniqueConstraint(fields=("id", "tenant"), name="company_id_tenant_uniq"),
        ),
        migrations.RunSQL(
            sql=[
                """
                ALTER TABLE directory_contract
                ADD CONSTRAINT contract_company_same_tenant_fk
                FOREIGN KEY (company_id, tenant_id)
                REFERENCES directory_company (id, tenant_id)
                DEFERRABLE INITIALLY DEFERRED
                """,
                "ALTER TABLE directory_worker ENABLE ROW LEVEL SECURITY",
                "ALTER TABLE directory_worker FORCE ROW LEVEL SECURITY",
                """
                CREATE POLICY tenant_isolation ON directory_worker
                USING (tenant_id::text = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
                """,
                "ALTER TABLE directory_company ENABLE ROW LEVEL SECURITY",
                "ALTER TABLE directory_company FORCE ROW LEVEL SECURITY",
                """
                CREATE POLICY tenant_isolation ON directory_company
                USING (tenant_id::text = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
                """,
                "ALTER TABLE directory_contract ENABLE ROW LEVEL SECURITY",
                "ALTER TABLE directory_contract FORCE ROW LEVEL SECURITY",
                """
                CREATE POLICY tenant_isolation ON directory_contract
                USING (tenant_id::text = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
                """,
            ],
            reverse_sql=[
                "ALTER TABLE directory_contract DROP CONSTRAINT contract_company_same_tenant_fk",
                "DROP POLICY tenant_isolation ON directory_contract",
                "ALTER TABLE directory_contract NO FORCE ROW LEVEL SECURITY",
                "ALTER TABLE directory_contract DISABLE ROW LEVEL SECURITY",
                "DROP POLICY tenant_isolation ON directory_company",
                "ALTER TABLE directory_company NO FORCE ROW LEVEL SECURITY",
                "ALTER TABLE directory_company DISABLE ROW LEVEL SECURITY",
                "DROP POLICY tenant_isolation ON directory_worker",
                "ALTER TABLE directory_worker NO FORCE ROW LEVEL SECURITY",
                "ALTER TABLE directory_worker DISABLE ROW LEVEL SECURITY",
            ],
        ),
    ]
