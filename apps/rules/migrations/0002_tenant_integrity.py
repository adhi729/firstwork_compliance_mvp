from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("rules", "0001_initial")]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE rules_rule_version ADD CONSTRAINT rule_version_same_tenant_fk
                    FOREIGN KEY (rule_id, tenant_id) REFERENCES rules_rule (id, tenant_id)
                    DEFERRABLE INITIALLY DEFERRED;
                ALTER TABLE rules_rule ADD CONSTRAINT rule_current_version_fk
                    FOREIGN KEY (id, current_version, tenant_id)
                    REFERENCES rules_rule_version (rule_id, version, tenant_id)
                    DEFERRABLE INITIALLY DEFERRED;

                ALTER TABLE rules_rule ENABLE ROW LEVEL SECURITY;
                ALTER TABLE rules_rule FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation ON rules_rule
                    USING (tenant_id::text = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
                ALTER TABLE rules_rule_version ENABLE ROW LEVEL SECURITY;
                ALTER TABLE rules_rule_version FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation ON rules_rule_version
                    USING (tenant_id::text = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));

                CREATE FUNCTION rules_reject_version_mutation() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'Version records are append-only'
                        USING ERRCODE = 'integrity_constraint_violation';
                END;
                $$;
                CREATE TRIGGER rule_version_immutable
                    BEFORE UPDATE OR DELETE ON rules_rule_version
                    FOR EACH ROW EXECUTE FUNCTION rules_reject_version_mutation();
            """,
            reverse_sql="""
                DROP TRIGGER rule_version_immutable ON rules_rule_version;
                DROP FUNCTION rules_reject_version_mutation();
                DROP POLICY tenant_isolation ON rules_rule_version;
                ALTER TABLE rules_rule_version NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE rules_rule_version DISABLE ROW LEVEL SECURITY;
                DROP POLICY tenant_isolation ON rules_rule;
                ALTER TABLE rules_rule NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE rules_rule DISABLE ROW LEVEL SECURITY;
                ALTER TABLE rules_rule DROP CONSTRAINT rule_current_version_fk;
                ALTER TABLE rules_rule_version DROP CONSTRAINT rule_version_same_tenant_fk;
            """,
        ),
    ]
