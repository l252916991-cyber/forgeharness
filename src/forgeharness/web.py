# ruff: noqa: RUF001  (user-facing Chinese punctuation is intentional)
"""Dependency-free browser shell, kept separate from the HTTP control plane."""

UI_SCRIPT = """
'use strict';
const byId = id => document.getElementById(id);
const sid = byId('sid'), question = byId('question'), out = byId('out');
const jobState = byId('job-state'), attach = byId('attach'), uploadButton = byId('upload');
let mediaIds = [];
async function request(path, options = {}) {
  const headers = new Headers(options.headers);
  const apiKey = byId('api-key').value;
  if (apiKey) headers.set('Authorization', 'Bearer ' + apiKey);
  const response = await fetch(path, {...options, headers});
  const payload = response.headers.get('content-type')?.includes('application/json')
    ? await response.json() : await response.text();
  if (!response.ok) throw new Error(
    response.status === 401 ? '认证失败，请输入有效的访问令牌'
      : typeof payload === 'string' ? payload : JSON.stringify(payload));
  return payload;
}
async function newSession() {
  const session = await request('/v1/sessions', {method: 'POST'});
  sid.value = session.id;
  mediaIds = [];
  jobState.textContent = '新会话已创建';
}
async function ask() {
  if (!sid.value) await newSession();
  if (!question.value.trim()) throw new Error('请先输入问题');
  out.textContent = '模型处理中…';
  const answer = await request('/v1/sessions/' + encodeURIComponent(sid.value) + '/messages', {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({text: question.value, media_ids: mediaIds})
  });
  out.textContent = JSON.stringify(answer, null, 2);
}
async function upload() {
  const file = byId('file').files[0];
  if (!file) throw new Error('请选择文件或截图');
  if (file.size > 10 * 1024 * 1024) throw new Error('文件不能超过10MB');
  if (!sid.value) await newSession();
  const form = new FormData();
  form.append('file', file);
  uploadButton.disabled = true;
  try {
    let job = await request('/v1/documents', {
      method: 'POST', headers: {'Idempotency-Key': crypto.randomUUID()}, body: form
    });
    for (let attempt = 0; attempt < 180; attempt++) {
      jobState.textContent = '任务 ' + job.id + ': ' + job.status;
      if (job.status === 'failed') throw new Error(job.error || '索引失败, 请重试上传');
      if (job.status === 'succeeded') {
        if (attach.checked) mediaIds = [...new Set([...mediaIds, job.document_id])];
        jobState.textContent += attach.checked ? '; 已附加到下一次提问' : '; 已进入知识库';
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 2000));
      job = await request('/v1/jobs/' + encodeURIComponent(job.id));
    }
    throw new Error('任务仍在后台处理, 请使用上方job id查询状态');
  } finally { uploadButton.disabled = false; }
}
function safe(action) { return () => action().catch(error => {out.textContent = error.message;}); }
byId('new-session').addEventListener('click', safe(newSession));
byId('send').addEventListener('click', safe(ask));
uploadButton.addEventListener('click', safe(upload));
"""

UI = (
    """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>ForgeHarness</title>
<style>body{font:16px system-ui;max-width:900px;margin:40px auto;padding:0 20px}
textarea,input{width:100%;padding:10px;margin:6px 0}button{padding:10px 18px}
pre{white-space:pre-wrap;background:#f4f4f4;padding:16px;border-radius:8px}</style></head>
<body><h1>ForgeHarness R&amp;D Knowledge Agent</h1>
<p>本地、可审计、带引用的研发知识智能体。API文档: <a href="/docs">/docs</a></p>
<label for="api-key">访问令牌（服务启用认证时填写，仅保留在当前页面）</label>
<input id="api-key" type="password" autocomplete="off" spellcheck="false">
<button id="new-session">新建会话</button><input id="sid" placeholder="session id">
<input id="file" type="file"
accept=".md,.txt,.py,.json,.pdf,.png,.jpg,.jpeg,.js,.ts,.go,.java,.rs,.c,.cpp,.h,.yaml,.yml">
<label><input id="attach" type="checkbox" checked style="width:auto">将文件/图片附加到提问</label>
<button id="upload">上传并索引(最多10MB)</button><pre id="job-state">尚未上传</pre>
<textarea id="question" rows="5" placeholder="询问项目文档或代码"></textarea>
<button id="send">发送</button><pre id="out">等待输入</pre>
<script>"""
    + UI_SCRIPT
    + """</script></body></html>"""
)
