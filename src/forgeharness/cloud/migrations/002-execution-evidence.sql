-- Apply only while workers are drained, with the migration identity. No runtime
-- connection performs upgrades. Existing first-slice facts remain unchanged.
LOCK TABLE fh_cloud.schema_version IN ACCESS EXCLUSIVE MODE;
ALTER TABLE fh_cloud.schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE fh_cloud.schema_version ADD CONSTRAINT schema_version_version_check CHECK(version IN (1,2));
ALTER TABLE fh_cloud.runs DROP CONSTRAINT runs_status_check;
ALTER TABLE fh_cloud.runs ADD CONSTRAINT runs_status_check CHECK(status IN
    ('queued','running','awaiting_approval','reconciling','succeeded','failed','cancelled','exhausted'));
-- Only trusted execution services admit policy-allowed calls. Existing callers
-- keep the independent-approval default and cannot obtain it by omitting a grant.
ALTER TABLE fh_cloud.invocations ALTER COLUMN approval_id DROP NOT NULL;
ALTER TABLE fh_cloud.invocations ADD COLUMN authorization_mode TEXT NOT NULL DEFAULT 'approval'
    CHECK (authorization_mode IN ('approval','policy'));
ALTER TABLE fh_cloud.invocations ADD CONSTRAINT invocation_authorization_shape CHECK (
    (authorization_mode='approval' AND approval_id IS NOT NULL)
    OR (authorization_mode='policy' AND approval_id IS NULL)
);
CREATE TABLE fh_cloud.execution_checkpoints (
    org_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    budget JSONB NOT NULL,
    owner_context JSONB NOT NULL,
    initial_input JSONB NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    checkpoint JSONB NOT NULL,
    used_usage JSONB NOT NULL DEFAULT '{"steps":0,"tool_calls":0,"input_tokens":0,"output_tokens":0}',
    reserved_usage JSONB NOT NULL DEFAULT '{"steps":0,"tool_calls":0,"input_tokens":0,"output_tokens":0}',
    trace_sequence BIGINT NOT NULL DEFAULT 0 CHECK (trace_sequence >= 0),
    trace_hash TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (org_id,run_id),
    FOREIGN KEY (org_id,run_id) REFERENCES fh_cloud.runs(org_id,run_id)
);
CREATE TABLE fh_cloud.execution_evidence (
    org_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    event_hash TEXT NOT NULL,
    envelope JSONB NOT NULL,
    PRIMARY KEY (org_id,run_id,sequence),
    UNIQUE (org_id,run_id,event_hash),
    FOREIGN KEY (org_id,run_id) REFERENCES fh_cloud.execution_checkpoints(org_id,run_id)
);
CREATE TABLE fh_cloud.execution_calls (
    org_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('model','tool')),
    logical_id TEXT NOT NULL,
    physical_attempt INTEGER NOT NULL DEFAULT 1 CHECK (physical_attempt=1),
    idempotency_key TEXT NOT NULL,
    canonical_input JSONB NOT NULL,
    reserved_usage JSONB NOT NULL,
    canonical_output JSONB,
    actual_usage JSONB,
    PRIMARY KEY (org_id,run_id,invocation_id),
    UNIQUE (org_id,run_id,kind,logical_id),
    UNIQUE (org_id,idempotency_key),
    FOREIGN KEY (org_id,run_id,invocation_id)
        REFERENCES fh_cloud.invocations(org_id,run_id,invocation_id)
);
-- A rejected logical request consumes the core tool-call budget without a
-- physical dispatch, provider reservation or provider receipt. Bind the fact to
-- its committed denial evidence so replay cannot count it twice.
CREATE TABLE fh_cloud.execution_logical_usage (
    org_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    logical_id TEXT NOT NULL,
    cause TEXT NOT NULL CHECK (cause IN ('policy_denied','unknown_tool','invalid_arguments')),
    call JSONB NOT NULL,
    observation JSONB NOT NULL,
    usage JSONB NOT NULL DEFAULT '{"steps":0,"tool_calls":1,"input_tokens":0,"output_tokens":0}'
        CHECK (usage = '{"steps":0,"tool_calls":1,"input_tokens":0,"output_tokens":0}'::jsonb),
    event_hash TEXT NOT NULL,
    checkpoint_revision BIGINT NOT NULL CHECK (checkpoint_revision > 0),
    PRIMARY KEY (org_id,run_id,logical_id),
    UNIQUE (org_id,run_id,event_hash),
    FOREIGN KEY (org_id,run_id) REFERENCES fh_cloud.execution_checkpoints(org_id,run_id),
    FOREIGN KEY (org_id,run_id,event_hash)
        REFERENCES fh_cloud.execution_evidence(org_id,run_id,event_hash)
);
DO $policies$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'execution_checkpoints','execution_evidence','execution_calls','execution_logical_usage'
    ]
    LOOP
        EXECUTE format('ALTER TABLE fh_cloud.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE fh_cloud.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY tenant_scope ON fh_cloud.%I USING (org_id = current_setting(''fh.org_id'', true)) WITH CHECK (org_id = current_setting(''fh.org_id'', true))',
            table_name
        );
    END LOOP;
END
$policies$;
UPDATE fh_cloud.schema_version SET version=2 WHERE version=1;
