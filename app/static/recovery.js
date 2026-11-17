/* 备份恢复页：制作备份、预演差异、执行恢复、续跑；动作不落指标公式，只驱动恢复接口。 */
const RECOVERY_ACTIONS = {
  async backup() {
    const result = await recoveryRequest('/api/recovery/backups', { note: '' });
    setTimeout(() => window.location.reload(), 500);
    return `备份 ${result.backup_id} 已制作，批次 ${result.run_count}、快照 ${result.snapshot_count}`;
  },

  async preview(button) {
    const backupId = button.dataset.backup;
    const result = await recoveryRequest(`/api/recovery/preview/${encodeURIComponent(backupId)}`);
    renderPreview(result);
    return `预演完成：保全 ${result.runs.preserve_count} 批，还原 ${result.runs.restore_count} 批`;
  },

  async restore(button) {
    const backupId = button.dataset.backup;
    const ok = window.confirm(`确认用备份 ${backupId} 恢复？后续批次将保全为只读凭据，业务库切回备份基线。`);
    if (!ok) return '已取消恢复';
    const result = await recoveryRequest('/api/recovery/restore', { backup_id: backupId });
    setTimeout(() => window.location.reload(), 600);
    return result.status === 'finished'
      ? `恢复完成：${result.steps_done}/${result.steps_total} 步，保全 ${result.preserved_count} 批`
      : `恢复单 ${result.job_id} 状态 ${result.status}，可续跑`;
  },

  async resume(button) {
    const jobId = button.dataset.job;
    const result = await recoveryRequest(`/api/recovery/restore/${encodeURIComponent(jobId)}/resume`);
    setTimeout(() => window.location.reload(), 500);
    return result.status === 'finished'
      ? `续跑完成：${result.steps_done}/${result.steps_total} 步`
      : `恢复单 ${result.job_id} 仍为 ${result.status}`;
  },
};

async function recoveryRequest(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload && payload.detail ? `（${JSON.stringify(payload.detail)}）` : '';
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message + detail);
  }
  return payload;
}

function renderPreview(result) {
  const panel = document.getElementById('preview-panel');
  const empty = document.getElementById('preview-empty');
  const body = document.getElementById('preview-body');
  const tableBody = document.querySelector('#preview-table tbody');
  panel.hidden = false;
  empty.hidden = true;

  body.innerHTML = '';
  const rows = [
    ['备份号', result.backup_id],
    ['备份批次/快照/隔离', `${result.runs.restore_count} / ${result.snapshots.restore_count} / ${result.quarantine.backup_rows}`],
    ['保全批次', String(result.runs.preserve_count)],
    ['保全独有快照', String(result.snapshots.preserve_count)],
    ['序号高水位', `${result.seq.hwm_before} → ${result.seq.hwm_after}`],
    ['备份摘要', result.payload_sha256],
  ];
  rows.forEach(([key, value]) => {
    const term = document.createElement('dt');
    term.textContent = key;
    const data = document.createElement('dd');
    data.className = 'mono';
    data.textContent = value;
    const wrap = document.createElement('div');
    wrap.className = 'manifest-row';
    wrap.append(term, data);
    body.append(wrap);
  });

  tableBody.innerHTML = '';
  result.runs.preserve.forEach((run) => appendPreviewRow(tableBody, '保全', run));
  result.runs.restore.forEach((run) => appendPreviewRow(tableBody, '还原', run));
}

function appendPreviewRow(tbody, kind, run) {
  const tr = document.createElement('tr');
  const cells = [
    kind,
    run.run_id,
    String(run.version_seq),
    `${run.version_label_old || '—'} → ${run.version_label_new || '—'}`,
    (run.created_at || '').slice(0, 10),
  ];
  cells.forEach((text, index) => {
    const td = document.createElement('td');
    td.textContent = text;
    if (index === 2) td.className = 'num';
    tr.append(td);
  });
  tbody.append(tr);
}

function flash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const handler = RECOVERY_ACTIONS[button.dataset.action];
  if (!handler) return;
  const target = document.querySelector('[data-flash]');
  button.disabled = true;
  flash(target, '…', false);
  try {
    const message = await handler(button);
    flash(target, message || '完成', false);
  } catch (error) {
    flash(target, error.message, true);
  } finally {
    button.disabled = false;
  }
});
