'use strict';
// Real Chromium uses the shipped workbench and its actual HTTP/PG services.
// The only out-of-band operation is starting the independent trusted worker.
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', async () => {
  let browser;
  let stage = 'launch';
  let externalRequests = 0;
  const errors = [];
  try {
    const config = JSON.parse(input);
    const {chromium} = require(config.playwright_module);
    browser = await chromium.launch({headless: true, executablePath: config.browser_executable || undefined,
      args: ['--disable-background-networking']});
    const createPage = async ({javaScriptEnabled = true, blockScript = false} = {}) => {
      const context = await browser.newContext({javaScriptEnabled});
      await context.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== config.base_url) {
          externalRequests++;
          return route.abort();
        }
        if (blockScript && url.pathname === '/assets/workbench.js') {
          return route.abort('failed');
        }
        return route.continue();
      });
      const page = await context.newPage();
      page.setDefaultTimeout(12000);
      page.on('pageerror', error => errors.push(error.name));
      await page.goto(config.base_url);
      await page.locator('#login-button').waitFor({state: 'visible'});
      return page;
    };
    const action = async (page, method, suffix, callback, expected = 204) => {
      const [response] = await Promise.all([
        page.waitForResponse(r => r.url().endsWith(suffix) && r.request().method() === method),
        callback(),
      ]);
      assert.equal(response.status(), expected, `HTTP ${method} ${suffix}`);
      return response;
    };
    const login = async (page, email) => {
      await page.locator('#login-email').fill(email);
      await page.locator('#login-password').fill(config.password);
      await action(page, 'POST', '/v1/login', () => page.locator('#login-button').click(), 200);
      await page.locator('#logout-button').waitFor({state: 'visible'});
      await page.waitForFunction(org => document.getElementById('organization-select').value === org, config.org_id);
    };
    const workspace = async (page, id, {use = true, approve = false} = {}) => {
      await page.locator(`#workspace-select option[value="${id}"]`).waitFor({state: 'attached'});
      await page.locator('#workspace-select').selectOption(id);
      await page.locator(use ? '#task-section' : '#approval-section').waitFor({state: 'visible'});
      await page.locator('#task-section').waitFor({state: use ? 'visible' : 'hidden'});
      await page.locator('#approval-section').waitFor({state: approve ? 'visible' : 'hidden'});
      if (use) {
        await page.waitForFunction(() => document.getElementById('deployment-select').options.length > 1);
      } else {
        assert.equal(await page.locator('#deployment-select option').count(), 1,
          'approval-only reviewer must have no submission deployment');
      }
    };
    const work = () => {
      const payload = {ledger_path: config.ledger_path, worker: config.worker};
      const result = spawnSync(config.worker_python, [config.worker_script, '--worker'], {
        input: JSON.stringify(payload) + '\n', encoding: 'utf8', env: process.env, timeout: 30000,
      });
      assert.equal(result.status, 0, 'independent worker exit');
      const value = JSON.parse(result.stdout);
      assert.equal(value.failures, 0, 'independent worker failures');
      assert.equal(value.progressed, 1, 'independent worker progress');
    };
    stage = 'native-form-secret-protection';
    for (const mode of ['disabled-js', 'blocked-script']) {
      for (const form of ['login', 'invitation']) {
        stage = `native-form-${mode}-${form}-post`;
        const native = await createPage({javaScriptEnabled: mode !== 'disabled-js',
          blockScript: mode === 'blocked-script'});
        const endpoint = form === 'login' ? '/v1/login' : '/v1/invitations/accept';
        const token = `synthetic-private-invitation-token-${mode}`;
        if (form === 'login') {
          await native.locator('#login-email').fill(config.admin_email);
          await native.locator('#login-password').fill(config.password);
        } else {
          await native.locator('details summary').click();
          await native.locator('#invitation-token').fill(token);
          await native.locator('#invitation-password').fill(config.password);
        }
        const [response] = await Promise.all([
          native.waitForResponse(r => r.url().endsWith(endpoint) && r.request().method() === 'POST'),
          native.locator(form === 'login' ? '#login-button' : '#accept-button').click(),
        ]);
        // No-JS forms fail closed at Origin validation or the JSON-only body
        // contract. Neither rejection may place a credential in a URL/error.
        assert.ok([403, 422].includes(response.status()), 'native forms must fail closed');
        assert.equal((await response.json()).error,
          response.status() === 403 ? 'request_verification_failed' : 'invalid_request');
        stage = `native-form-${mode}-${form}-no-url-secrets`;
        await native.waitForURL(url => url.pathname === endpoint);
        const requestUrl = response.request().url();
        const pageUrl = native.url();
        assert.equal(new URL(requestUrl).search, '', 'native credentials must not enter request URL');
        assert.equal(new URL(pageUrl).search, '', 'native credentials must not enter page URL');
        const visible = requestUrl + pageUrl + await response.text();
        for (const secret of [config.password, token]) {
          assert.equal(visible.includes(secret), false, 'raw credential exposure');
          assert.equal(visible.includes(encodeURIComponent(secret)), false, 'encoded credential exposure');
        }
        await native.context().close();
      }
    }
    const admin = await createPage();
    stage = 'administrator-login-and-invitation';
    await login(admin, config.admin_email);
    await admin.locator('#invite-email').fill(config.member_email);
    const invitation = await action(admin, 'POST', `/v1/organizations/${config.org_id}/invitations`,
      () => admin.locator('#invite-submit-button').click(), 201);
    const token = (await invitation.json()).invitation_token;
    await admin.waitForFunction(() => document.getElementById('invite-output').textContent.length > 20);
    assert.equal((await admin.locator('#invite-output').textContent()).trim(), token);
    stage = 'browser-invitation-acceptance';
    const member = await createPage();
    await member.locator('details summary').click();
    await member.locator('#invitation-token').fill(token);
    await member.locator('#invitation-password').fill(config.password);
    await action(member, 'POST', '/v1/invitations/accept', () => member.locator('#accept-button').click(), 200);
    await member.locator('#logout-button').waitFor({state: 'visible'});
    const meResponse = await member.request.get(config.base_url + '/v1/me');
    assert.equal(meResponse.status(), 200);
    const account = (await meResponse.json()).account_id;
    stage = 'browser-workspace-and-deployment-management';
    await admin.reload();
    await admin.locator('#admin-section').waitFor({state: 'visible'});
    await admin.locator(`#workspace-member-select option[value="${account}"]`).waitFor({state: 'attached'});
    for (const [ws, domain] of [[config.workspace_id, 'forms'], [config.cancel_workspace_id, 'research']]) {
      await workspace(admin, ws);
      await admin.locator('#workspace-member-select').selectOption(account);
      await admin.locator('#workspace-member-active').check();
      await action(admin, 'PUT', `/workspaces/${ws}/members/${account}`,
        () => admin.locator('#workspace-member-form button').click());
      await admin.locator('#grant-deployment').fill(`synthetic-${domain}`);
      await admin.locator('#grant-member-select').selectOption(account);
      await admin.locator('#grant-use').check();
      await admin.locator('#grant-approve').uncheck();
      await admin.locator('#grant-active').check();
      await action(admin, 'PUT', `/deployments/synthetic-${domain}/grants/${account}`,
        () => admin.locator('#grant-form button').click());
    }
    stage = 'member-login-and-task';
    await member.locator('#logout-button').click();
    await member.locator('#login-button').waitFor({state: 'visible'});
    await login(member, config.member_email);
    await workspace(member, config.workspace_id);
    await member.locator('#deployment-select').selectOption('synthetic-forms');
    await member.locator('#task-input').fill('Review the synthetic form after independent approval.');
    const accepted = await action(member, 'POST', `/workspaces/${config.workspace_id}/runs`,
      () => member.locator('#submit-button').click(), 202);
    const runId = (await accepted.json()).run_id;
    await member.waitForFunction(id => document.getElementById('run-id').textContent === id, runId);
    assert.equal((await member.locator('#run-status').textContent()).trim(), '排队中');
    work();
    await member.waitForFunction(() => document.getElementById('run-status').textContent === '等待审批');
    stage = 'independent-reviewer-browser-approval';
    const reviewer = await createPage();
    await login(reviewer, config.reviewer_email);
    await workspace(reviewer, config.workspace_id, {use: false, approve: true});
    assert.equal(await reviewer.locator('#task-section').isVisible(), false);
    assert.equal(await reviewer.locator('#approval-section').isVisible(), true);
    await reviewer.locator(`#approvals-list button[data-run-id="${runId}"]`).click();
    await reviewer.locator('#approval-detail').waitFor({state: 'visible'});
    const proposal = JSON.parse(await reviewer.locator('#approval-proposal').textContent());
    assert.equal(proposal.name, 'oracle.write');
    assert.equal(proposal.arguments.value, 'approved');
    assert.match(await reviewer.locator('#approval-fingerprint').textContent(), /[a-f0-9]{64}/);
    await action(reviewer, 'POST', `/runs/${runId}/approve`,
      () => reviewer.locator('#approve-button').click(), 200);
    work();
    await member.waitForFunction(() => document.getElementById('run-status').textContent === '已完成');
    const result = JSON.parse(await member.locator('#run-result').textContent());
    assert.equal(result.domain, 'forms');
    assert.equal(result.tenant, config.org_id);
    assert.match(await member.locator('#run-usage').textContent(), /已结算 20 单位/);
    stage = 'browser-cancellation';
    await workspace(member, config.cancel_workspace_id);
    await member.locator('#deployment-select').selectOption('synthetic-research');
    await member.locator('#task-input').fill('Cancel this task while it is still queued.');
    const cancelling = await action(member, 'POST', `/workspaces/${config.cancel_workspace_id}/runs`,
      () => member.locator('#submit-button').click(), 202);
    const cancelId = (await cancelling.json()).run_id;
    await member.locator('#cancel-button').waitFor({state: 'visible'});
    await action(member, 'POST', `/runs/${cancelId}/cancel`,
      () => member.locator('#cancel-button').click(), 200);
    assert.match(await member.locator('#run-status').textContent(), /取消处理中/);
    work();
    await member.waitForFunction(() => document.getElementById('run-status').textContent === '已取消');
    stage = 'browser-revocation';
    await workspace(admin, config.workspace_id);
    await admin.locator('#workspace-member-select').selectOption(account);
    await admin.locator('#workspace-member-active').uncheck();
    await action(admin, 'PUT', `/workspaces/${config.workspace_id}/members/${account}`,
      () => admin.locator('#workspace-member-form button').click());
    const revoked = await member.request.get(config.base_url +
      `/v1/organizations/${config.org_id}/workspaces/${config.workspace_id}/runs/${runId}`);
    assert.equal(revoked.status(), 404);
    await member.reload();
    await member.locator('#scope-section').waitFor({state: 'visible'});
    await member.waitForFunction(ws => ![...document.getElementById('workspace-select').options]
      .some(option => option.value === ws), config.workspace_id);
    assert.equal(await member.locator('#runs-list button').count(), 0);
    stage = 'browser-logout';
    await action(member, 'POST', '/v1/logout', () => member.locator('#logout-button').click(), 204);
    assert.equal((await member.request.get(config.base_url + '/v1/me')).status(), 401);
    assert.equal(externalRequests, 0, 'browser must remain on loopback');
    assert.deepEqual(errors, [], 'browser script errors');
    console.log(JSON.stringify({passed: true, engine: 'Chromium', browser_version: browser.version(),
      invitation: true, login: true,
      management: true, task: true, independent_approval: true, cancel: true, revocation: true,
      reviewer_permissions: {can_submit: false, can_approve: true},
      no_script_secret_protection: {disabled_js: true, blocked_script: true},
      logout: true, external_requests: externalRequests}));
  } catch (error) {
    // Do not log page values, invitation tokens, credentials, DSNs or raw errors.
    console.log(JSON.stringify({passed: false, stage, error_type: error.name,
      actual_status: typeof error.actual === 'number' ? error.actual : undefined,
      external_requests: externalRequests}));
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
});
