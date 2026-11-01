-- Explicit offline schema2 -> schema3. Stop API writers and workers first.
-- Existing actors, run owners, approvals, receipts and evidence are untouched.
LOCK TABLE fh_cloud.schema_version IN ACCESS EXCLUSIVE MODE;
ALTER TABLE fh_cloud.schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE fh_cloud.schema_version ADD CONSTRAINT schema_version_version_check
    CHECK (version IN (1,2,3));
CREATE TABLE fh_cloud.accounts (
    account_id TEXT PRIMARY KEY CHECK (account_id ~ '^acct_[a-f0-9]{32}$'),
    email_normalized TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    lock_marker INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE fh_cloud.org_members (
    org_id TEXT NOT NULL REFERENCES fh_cloud.organizations(org_id),
    account_id TEXT NOT NULL REFERENCES fh_cloud.accounts(account_id),
    role TEXT NOT NULL CHECK (role IN ('owner','admin','member')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    version BIGINT NOT NULL DEFAULT 1,
    lock_marker INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(org_id,account_id)
);
CREATE TABLE fh_cloud.workspaces (
    org_id TEXT NOT NULL REFERENCES fh_cloud.organizations(org_id),
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    lock_marker INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(org_id,workspace_id)
);
CREATE TABLE fh_cloud.workspace_members (
    org_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    lock_marker INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(org_id,workspace_id,account_id),
    FOREIGN KEY(org_id,workspace_id) REFERENCES fh_cloud.workspaces(org_id,workspace_id),
    FOREIGN KEY(org_id,account_id) REFERENCES fh_cloud.org_members(org_id,account_id)
);
CREATE TABLE fh_cloud.deployments (
    org_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    binding_digest TEXT NOT NULL CHECK (binding_digest ~ '^[a-f0-9]{64}$'),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    lock_marker INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(org_id,deployment_id),
    FOREIGN KEY(org_id,workspace_id) REFERENCES fh_cloud.workspaces(org_id,workspace_id)
);
CREATE TABLE fh_cloud.deployment_grants (
    org_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    can_use BOOLEAN NOT NULL DEFAULT FALSE,
    can_approve BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    lock_marker INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(org_id,deployment_id,account_id),
    FOREIGN KEY(org_id,deployment_id) REFERENCES fh_cloud.deployments(org_id,deployment_id),
    FOREIGN KEY(org_id,account_id) REFERENCES fh_cloud.org_members(org_id,account_id)
);
CREATE TABLE fh_cloud.invitations (
    org_id TEXT NOT NULL REFERENCES fh_cloud.organizations(org_id),
    invitation_id TEXT NOT NULL,
    token_digest TEXT NOT NULL UNIQUE,
    email_normalized TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner','admin','member')),
    invited_by TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY(org_id,invitation_id),
    FOREIGN KEY(org_id,invited_by) REFERENCES fh_cloud.org_members(org_id,account_id)
);
CREATE TABLE fh_cloud.sessions (
    session_id TEXT PRIMARY KEY,
    token_digest TEXT NOT NULL UNIQUE,
    csrf_digest TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES fh_cloud.accounts(account_id),
    org_id TEXT REFERENCES fh_cloud.organizations(org_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    revoked_at TIMESTAMPTZ
);
CREATE TABLE fh_cloud.login_failures (
    key_digest TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE fh_cloud.authentication_audit (
    audit_id TEXT PRIMARY KEY,
    key_digest TEXT NOT NULL CHECK (key_digest ~ '^[a-f0-9]{64}$'),
    account_id TEXT REFERENCES fh_cloud.accounts(account_id),
    action TEXT NOT NULL CHECK (action IN ('login','invitation_accept')),
    outcome TEXT NOT NULL CHECK (outcome IN ('allowed','denied','throttled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE fh_cloud.identity_audit (
    org_id TEXT NOT NULL REFERENCES fh_cloud.organizations(org_id),
    audit_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    object_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('allowed','denied')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(org_id,audit_id)
);
CREATE TABLE fh_cloud.run_bindings (
    org_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    binding_digest TEXT NOT NULL CHECK (binding_digest ~ '^[a-f0-9]{64}$'),
    scope JSONB NOT NULL,
    PRIMARY KEY(org_id,run_id),
    FOREIGN KEY(org_id,run_id) REFERENCES fh_cloud.runs(org_id,run_id),
    FOREIGN KEY(org_id,deployment_id) REFERENCES fh_cloud.deployments(org_id,deployment_id),
    FOREIGN KEY(org_id,workspace_id) REFERENCES fh_cloud.workspaces(org_id,workspace_id)
);
CREATE TABLE fh_cloud.approval_decisions (
    org_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    action_digest TEXT NOT NULL CHECK (action_digest ~ '^[a-f0-9]{64}$'),
    decision TEXT NOT NULL CHECK (decision IN ('approve','reject')),
    decider_id TEXT NOT NULL,
    approval_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(org_id,run_id,action_digest),
    FOREIGN KEY(org_id,run_id) REFERENCES fh_cloud.runs(org_id,run_id),
    FOREIGN KEY(org_id,decider_id) REFERENCES fh_cloud.members(org_id,actor_id),
    FOREIGN KEY(org_id,approval_id) REFERENCES fh_cloud.approvals(org_id,approval_id),
    CHECK ((decision='approve' AND approval_id IS NOT NULL)
        OR (decision='reject' AND approval_id IS NULL))
);
CREATE INDEX fh_cloud_member_accounts ON fh_cloud.org_members(account_id,org_id);
CREATE INDEX fh_cloud_run_binding_workspace ON fh_cloud.run_bindings(org_id,workspace_id,run_id);
CREATE INDEX fh_cloud_sessions_account ON fh_cloud.sessions(account_id);
DO $policies$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['org_members','workspaces','workspace_members',
        'deployments','deployment_grants','identity_audit','run_bindings','approval_decisions']
    LOOP
        EXECUTE format('ALTER TABLE fh_cloud.%I ENABLE ROW LEVEL SECURITY',table_name);
        EXECUTE format('ALTER TABLE fh_cloud.%I FORCE ROW LEVEL SECURITY',table_name);
        EXECUTE format('CREATE POLICY tenant_scope ON fh_cloud.%I USING '
            || '(org_id=current_setting(''fh.org_id'',true)) WITH CHECK '
            || '(org_id=current_setting(''fh.org_id'',true))',table_name);
    END LOOP;
    -- Invitation lookup needs the presented token before an org is known. The
    -- identity role alone has DML; its trusted global transaction sets this flag.
    FOREACH table_name IN ARRAY ARRAY['accounts','sessions','login_failures','invitations',
        'authentication_audit']
    LOOP
        EXECUTE format('ALTER TABLE fh_cloud.%I ENABLE ROW LEVEL SECURITY',table_name);
        EXECUTE format('ALTER TABLE fh_cloud.%I FORCE ROW LEVEL SECURITY',table_name);
        EXECUTE format('CREATE POLICY identity_scope ON fh_cloud.%I USING '
            || '(current_setting(''fh.identity_service'',true)=''on'' AND '
            || 'has_table_privilege(current_user,''fh_cloud.accounts'',''UPDATE'')) WITH CHECK '
            || '(current_setting(''fh.identity_service'',true)=''on'' AND '
            || 'has_table_privilege(current_user,''fh_cloud.accounts'',''UPDATE''))',table_name);
    END LOOP;
END
$policies$;
-- Runtime can see only public account columns and only in its current tenant.
CREATE POLICY account_tenant_read ON fh_cloud.accounts FOR SELECT USING (
    EXISTS (SELECT 1 FROM fh_cloud.org_members m
        WHERE m.account_id=accounts.account_id AND m.org_id=current_setting('fh.org_id',true))
);
-- Row locks require UPDATE on an inert column; no identity mutation is granted.
CREATE POLICY account_tenant_lock ON fh_cloud.accounts FOR UPDATE USING (
    EXISTS (SELECT 1 FROM fh_cloud.org_members m
        WHERE m.account_id=accounts.account_id AND m.org_id=current_setting('fh.org_id',true))
) WITH CHECK (EXISTS (SELECT 1 FROM fh_cloud.org_members m
        WHERE m.account_id=accounts.account_id AND m.org_id=current_setting('fh.org_id',true)));
CREATE POLICY identity_membership_lookup ON fh_cloud.org_members FOR SELECT USING (
    current_setting('fh.identity_service',true)='on'
    AND has_table_privilege(current_user,'fh_cloud.accounts','UPDATE')
    AND account_id=current_setting('fh.account_id',true)
);
CREATE POLICY identity_organization_lookup ON fh_cloud.organizations FOR SELECT USING (
    current_setting('fh.identity_service',true)='on'
    AND has_table_privilege(current_user,'fh_cloud.accounts','UPDATE') AND EXISTS (
        SELECT 1 FROM fh_cloud.org_members m WHERE m.org_id=organizations.org_id
        AND m.account_id=current_setting('fh.account_id',true))
);
UPDATE fh_cloud.schema_version SET version=3 WHERE version=2;
