from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0004_alter_company_bank_deposit_alter_company_country_and_more"),
        ("evaluation", "0005_bulk_job_cursor"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conrelid = 'evaluation_job'::regclass
                          AND conname = 'job_check_version_same_tenant_fk'
                    ) THEN
                        ALTER TABLE evaluation_job
                            ADD CONSTRAINT job_check_version_same_tenant_fk
                            FOREIGN KEY (check_version_id, tenant_id)
                            REFERENCES evaluation_check_version (id, tenant_id)
                            DEFERRABLE INITIALLY DEFERRED;
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conrelid = 'evaluation_job_result'::regclass
                          AND conname = 'job_result_job_same_tenant_fk'
                    ) THEN
                        ALTER TABLE evaluation_job_result
                            ADD CONSTRAINT job_result_job_same_tenant_fk
                            FOREIGN KEY (job_id, tenant_id)
                            REFERENCES evaluation_job (id, tenant_id)
                            DEFERRABLE INITIALLY DEFERRED;
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conrelid = 'evaluation_job_result'::regclass
                          AND conname = 'job_result_contract_same_tenant_fk'
                    ) THEN
                        ALTER TABLE evaluation_job_result
                            ADD CONSTRAINT job_result_contract_same_tenant_fk
                            FOREIGN KEY (contract_id, tenant_id)
                            REFERENCES directory_contract (id, tenant_id)
                            DEFERRABLE INITIALLY DEFERRED;
                    END IF;
                END
                $$;

                ALTER TABLE evaluation_job ENABLE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_job FORCE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_job_result ENABLE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_job_result FORCE ROW LEVEL SECURITY;

                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE schemaname = current_schema()
                          AND tablename = 'evaluation_job'
                          AND policyname = 'tenant_isolation'
                    ) THEN
                        CREATE POLICY tenant_isolation ON evaluation_job
                            USING (tenant_id::text = current_setting('app.tenant_id', true))
                            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE schemaname = current_schema()
                          AND tablename = 'evaluation_job_result'
                          AND policyname = 'tenant_isolation'
                    ) THEN
                        CREATE POLICY tenant_isolation ON evaluation_job_result
                            USING (tenant_id::text = current_setting('app.tenant_id', true))
                            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
                    END IF;
                END
                $$;
            """,
            reverse_sql="""
                DROP POLICY IF EXISTS tenant_isolation ON evaluation_job_result;
                ALTER TABLE evaluation_job_result NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_job_result DISABLE ROW LEVEL SECURITY;
                DROP POLICY IF EXISTS tenant_isolation ON evaluation_job;
                ALTER TABLE evaluation_job NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_job DISABLE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_job_result
                    DROP CONSTRAINT IF EXISTS job_result_contract_same_tenant_fk;
                ALTER TABLE evaluation_job_result
                    DROP CONSTRAINT IF EXISTS job_result_job_same_tenant_fk;
                ALTER TABLE evaluation_job
                    DROP CONSTRAINT IF EXISTS job_check_version_same_tenant_fk;
            """,
        ),
    ]
