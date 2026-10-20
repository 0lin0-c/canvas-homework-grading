const $ = id => document.getElementById(id);
const assignmentUrl = new URLSearchParams(location.search).get('assignment') || '';
let roster = null;
function selectedIds() { return [...document.querySelectorAll('#students input:checked')].map(node => node.value); }
function preview() {
  if (!roster) { $('preview').textContent = ''; return; }
  $('preview').textContent = `主题：作业提交提醒：${roster.title}\n\n同学你好：\nCanvas 显示你尚未提交“${roster.title}”。请尽快通过Canvas提交：\n${roster.assignment_url}\n如果你已经提交，请核对 Canvas 作业页面的状态。`;
}
async function loadRoster() {
  if (!assignmentUrl) throw Error('缺少作业链接，请返回复核页面重新输入。');
  const response = await fetch('/api/assignment-roster', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({assignment_url:assignmentUrl})});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || '读取提交状态失败。');
  roster = data;
  $('summary').textContent = `${data.title} · 全班 ${data.total_count} 人 · 已提交 ${data.submitted.length} 人 · 未提交 ${data.unsubmitted.length} 人${data.excluded_count ? ` · 免交或不可见 ${data.excluded_count} 人` : ''}`;
  $('students').replaceChildren();
  for (const student of data.unsubmitted) {
    const row = document.createElement('label'); row.className = 'student';
    const check = document.createElement('input'); check.type = 'checkbox'; check.value = student.id; check.checked = true;
    row.append(check, document.createTextNode(student.name)); $('students').append(row);
  }
  $('send').disabled = !data.unsubmitted.length;
  $('selectAll').disabled = !data.unsubmitted.length;
  $('content').hidden = false; preview();
}
$('selectAll').onchange = () => document.querySelectorAll('#students input').forEach(node => { node.checked = $('selectAll').checked; });
$('send').onclick = async () => {
  $('error').textContent = ''; $('success').textContent = '';
  const ids = selectedIds();
  if (!ids.length) { $('error').textContent = '请先勾选至少一位未提交的同学。'; return; }
  $('send').disabled = true;
  try {
    const response = await fetch('/api/remind-unsubmitted', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({assignment_url:assignmentUrl,recipient_ids:ids})});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || '发送状态未知，请到 Canvas 收件箱核对。');
    $('success').textContent = `已向 ${data.sent_count} 位同学发送单独的 Canvas 收件箱提醒。`;
  } catch (error) { $('error').textContent = error.message; $('send').disabled = false; }
};
loadRoster().catch(error => { $('summary').textContent = '暂无法读取实时名单'; $('error').textContent = error.message; });
