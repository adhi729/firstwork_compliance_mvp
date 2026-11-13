from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0004_alter_company_bank_deposit_alter_company_country_and_more"),
        ("evaluation", "0001_initial"),
        ("rules", "0002_tenant_integrity"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE evaluation_check_version ADD CONSTRAINT check_version_same_tenant_fk
                    FOREIGN KEY (compliance_check_id, tenant_id) REFERENCES evaluation_check (id, tenant_id)
                    DEFERRABLE INITIALLY DEFERRED;
                ALTER TABLE evaluation_check ADD CONSTRAINT check_current_version_fk
                    FOREIGN KEY (id, current_version, tenant_id)
                    REFERENCES evaluation_check_version (compliance_check_id, version, tenant_id)
                    DEFERRABLE INITIALLY DEFERRED;
                ALTER TABLE evaluation_check_version_rule ADD CONSTRAINT pin_check_same_tenant_fk
                    FOREIGN KEY (check_version_id, tenant_id)
                    REFERENCES evaluation_check_version (id, tenant_id)
                    DEFERRABLE INITIALLY DEFERRED;
                ALTER TABLE evaluation_check_version_rule ADD CONSTRAINT pin_rule_same_tenant_fk
                    FOREIGN KEY (rule_version_id, tenant_id)
                    REFERENCES rules_rule_version (id, tenant_id)
                    DEFERRABLE INITIALLY DEFERRED;
                ALTER TABLE evaluation_result ADD CONSTRAINT result_check_same_tenant_fk
                    FOREIGN KEY (check_version_id, tenant_id)
                    REFERENCES evaluation_check_version (id, tenant_id)
                    DEFERRABLE INITIALLY DEFERRED;
                ALTER TABLE evaluation_result ADD CONSTRAINT result_contract_same_tenant_fk
                    FOREIGN KEY (contract_id, tenant_id)
                    REFERENCES directory_contract (id, tenant_id)
                    DEFERRABLE INITIALLY DEFERRED;

                ALTER TABLE evaluation_check ENABLE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_check FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation ON evaluation_check
                    USING (tenant_id::text = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
                ALTER TABLE evaluation_check_version ENABLE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_check_version FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation ON evaluation_check_version
                    USING (tenant_id::text = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
                ALTER TABLE evaluation_check_version_rule ENABLE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_check_version_rule FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation ON evaluation_check_version_rule
                    USING (tenant_id::text = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
                ALTER TABLE evaluation_result ENABLE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_result FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation ON evaluation_result
                    USING (tenant_id::text = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));

                CREATE FUNCTION evaluation_reject_version_mutation() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'Version records are append-only'
                        USING ERRCODE = 'integrity_constraint_violation';
                END;
                $$;
                CREATE TRIGGER check_version_immutable BEFORE UPDATE OR DELETE
                    ON evaluation_check_version FOR EACH ROW
                    EXECUTE FUNCTION evaluation_reject_version_mutation();
                CREATE TRIGGER check_pin_immutable BEFORE UPDATE OR DELETE
                    ON evaluation_check_version_rule FOR EACH ROW
                    EXECUTE FUNCTION evaluation_reject_version_mutation();

                -- Validate the complete, ordered pin set at commit, including later inserts.
                -- Temporarily restore the row tenant for FORCE RLS if a transaction switched
                -- tenant context between writing the rows and checking deferred constraints.
                CREATE FUNCTION evaluation_validate_pins() RETURNS trigger
                LANGUAGE plpgsql AS $$
                DECLARE
                    previous_tenant text := current_setting('app.tenant_id', true);
                    version_id bigint;
                    expected jsonb;
                    actual jsonb;
                BEGIN
                    IF TG_TABLE_NAME = 'evaluation_check_version' THEN
                        version_id := NEW.id;
                    ELSE
                        version_id := NEW.check_version_id;
                    END IF;
                    PERFORM set_config('app.tenant_id', NEW.tenant_id::text, true);
                    SELECT rule_versions INTO expected
                        FROM evaluation_check_version WHERE id = version_id;
                    SELECT COALESCE(jsonb_agg(rule_version_id ORDER BY position), '[]'::jsonb)
                        INTO actual FROM evaluation_check_version_rule
                        WHERE check_version_id = version_id;
                    PERFORM set_config('app.tenant_id', COALESCE(previous_tenant, ''), true);
                    IF expected IS NULL OR expected <> actual THEN
                        RAISE EXCEPTION 'Check version pins must match rule_versions'
                            USING ERRCODE = 'integrity_constraint_violation';
                    END IF;
                    RETURN NULL;
                END;
                $$;
                CREATE CONSTRAINT TRIGGER check_version_pins_match
                    AFTER INSERT ON evaluation_check_version DEFERRABLE INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION evaluation_validate_pins();
                CREATE CONSTRAINT TRIGGER check_pin_list_match
                    AFTER INSERT ON evaluation_check_version_rule DEFERRABLE INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION evaluation_validate_pins();
            """,
            reverse_sql="""
                DROP TRIGGER check_pin_list_match ON evaluation_check_version_rule;
                DROP TRIGGER check_version_pins_match ON evaluation_check_version;
                DROP FUNCTION evaluation_validate_pins();
                DROP TRIGGER check_pin_immutable ON evaluation_check_version_rule;
                DROP TRIGGER check_version_immutable ON evaluation_check_version;
                DROP FUNCTION evaluation_reject_version_mutation();
                DROP POLICY tenant_isolation ON evaluation_result;
                ALTER TABLE evaluation_result NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_result DISABLE ROW LEVEL SECURITY;
                DROP POLICY tenant_isolation ON evaluation_check_version_rule;
                ALTER TABLE evaluation_check_version_rule NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_check_version_rule DISABLE ROW LEVEL SECURITY;
                DROP POLICY tenant_isolation ON evaluation_check_version;
                ALTER TABLE evaluation_check_version NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_check_version DISABLE ROW LEVEL SECURITY;
                DROP POLICY tenant_isolation ON evaluation_check;
                ALTER TABLE evaluation_check NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_check DISABLE ROW LEVEL SECURITY;
                ALTER TABLE evaluation_result DROP CONSTRAINT result_contract_same_tenant_fk;
                ALTER TABLE evaluation_result DROP CONSTRAINT result_check_same_tenant_fk;
                ALTER TABLE evaluation_check_version_rule DROP CONSTRAINT pin_rule_same_tenant_fk;
                ALTER TABLE evaluation_check_version_rule DROP CONSTRAINT pin_check_same_tenant_fk;
                ALTER TABLE evaluation_check DROP CONSTRAINT check_current_version_fk;
                ALTER TABLE evaluation_check_version DROP CONSTRAINT check_version_same_tenant_fk;
            """,
        ),
    ]
