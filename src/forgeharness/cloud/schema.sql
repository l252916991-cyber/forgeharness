-- First isolated Cloud transaction slice. Apply only with the migration role.
-- The runtime receives DML grants separately; it must never own these objects.
CREATE SCHEMA fh_cloud;
CREATE TABLE fh_cloud.schema_version (version INTEGER PRIMARY KEY CHECK (version=1));
INSERT INTO fh_cloud.schema_version VALUES (1);
CREATE TABLE fh_cloud.organizations (
    org_id TEXT PRIMARY KEY,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    lock_marker INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE fh_cloud.members (
    org_id TEXT NOT NULL REFERENCES fh_cloud.organizations(org_id),
    actor_id TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    can_execute BOOLEAN NOT NULL DEFAULT FALSE,
    can_approve BOOLEAN NOT NULL DEFAULT FALSE,
    lock_marker INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (org_id, actor_id)
);
CREATE TABLE fh_cloud.runs (
    org_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    definition JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','reconciling','succeeded','failed','cancelled','exhausted')),
    used_units BIGINT NOT NULL DEFAULT 0 CHECK (used_units >= 0),
    reserved_units BIGINT NOT NULL DEFAULT 0 CHECK (reserved_units >= 0),
    epoch BIGINT NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    worker_id TEXT,
    lease_actor_id TEXT,
    lease_until TIMESTAMPTZ,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (org_id, run_id),
    UNIQUE (org_id, owner_id, request_key),
    FOREIGN KEY (org_id, owner_id) REFERENCES fh_cloud.members(org_id, actor_id)
);
CREATE TABLE fh_cloud.approvals (
    org_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    approver_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (org_id, approval_id),
    FOREIGN KEY (org_id, run_id) REFERENCES fh_cloud.runs(org_id, run_id),
    FOREIGN KEY (org_id, approver_id) REFERENCES fh_cloud.members(org_id, actor_id)
);
CREATE TABLE fh_cloud.invocations (
    org_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    effect_class TEXT NOT NULL CHECK (effect_class IN ('read','write','idempotent_write')),
    arguments_digest TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    reserve_units BIGINT NOT NULL CHECK (reserve_units >= 0),
    state TEXT NOT NULL CHECK (state IN ('admitted','sent','unknown','settled')),
    admission_epoch BIGINT NOT NULL,
    receipt_id TEXT,
    PRIMARY KEY (org_id, run_id, invocation_id),
    FOREIGN KEY (org_id, run_id) REFERENCES fh_cloud.runs(org_id, run_id),
    FOREIGN KEY (org_id, approval_id) REFERENCES fh_cloud.approvals(org_id, approval_id)
);
CREATE TABLE fh_cloud.receipts (
    org_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    actual_units BIGINT NOT NULL CHECK (actual_units >= 0),
    result_digest TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('completed','not_executed')),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (org_id, run_id, invocation_id),
    UNIQUE (org_id, receipt_id),
    FOREIGN KEY (org_id, run_id, invocation_id)
        REFERENCES fh_cloud.invocations(org_id, run_id, invocation_id)
);
CREATE TABLE fh_cloud.outbox (
    org_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    delivered_at TIMESTAMPTZ,
    PRIMARY KEY (org_id, event_id),
    FOREIGN KEY (org_id, run_id) REFERENCES fh_cloud.runs(org_id, run_id)
);
CREATE INDEX fh_cloud_outbox_pending ON fh_cloud.outbox (org_id, created_at)
    WHERE delivered_at IS NULL;
-- No self-referencing membership policy: context comes from the trusted host.
-- A service with arbitrary SQL and shared credentials is outside this boundary.
DO $policies$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['organizations','members','runs','approvals','invocations','receipts','outbox']
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
