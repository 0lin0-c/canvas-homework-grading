const $ = id => document.getElementById(id);
let task = null, selected = null, busy = false;
const storageKey = 'canvas-error-review-drafts-v1';
let drafts = {};
try { drafts = JSON.parse(localStorage.getItem(storageKey) || '{}'); } catch { /* Start with empty local drafts. */ }
function button(text, action, className = '') {
  const node = document.createElement('button'); node.type = 'button'; node.textContent = text;
  node.className = className; node.onclick = action; return node;
}
function textNode(tag, text, className = '') {
  const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
}
function openScan(url, title) {
  $('scanTitle').textContent = title; $('fullScan').src = url; $('fullScan').alt = title; $('scanViewer').showModal();
}
function findings(student) { return student.questions.filter(q => ['incorrect', 'uncertain', 'missing'].includes(q.status)); }
function suggestedComment(student) {
  return (student.comment || '').split('\n').filter(line => line.trim() && !line.includes('两级模型判断不同'))
    .map((line, index) => line.replace(/^\d+\.\s*/, `${index + 1}. `)
      .replace(/学生(?=的|写|计算|将|仅|所|未|漏|在|把|给|作答|答案|两页|提交)/g, '你')).join('\n');
}
function draftKey(student) { return task.assignment_url + ':' + task.mode + ':' + student.id + ':' + (student.submitted_at || 'demo') + ':' + (student.review_revision || task.review_revision || '1'); }
function render() {
  $('workspace').hidden = !task; $('empty').hidden = !!task; if (!task) return;
  const visible = task.students;
  if (!visible.some(s => s.id === selected)) selected = visible[0]?.id ?? null;
  $('review').replaceChildren();
  if (!visible.length) {
    $('review').append(textNode('h2', '暂无学生答卷')); return;
  }
  const student = visible.find(s => s.id === selected);
  try { sessionStorage.setItem('canvas-review-position:' + task.assignment_url, selected); } catch { /* Keep navigation available without storage. */ }
  const studentPicker = document.createElement('select'); studentPicker.className = 'student-picker';
  studentPicker.setAttribute('aria-label', '选择学生');
  for (const [index, entry] of visible.entries()) {
    const option = textNode('option', `${index + 1}. ${entry.name}`); option.value = entry.id; studentPicker.append(option);
  }
  studentPicker.value = selected;
  studentPicker.onchange = () => {
    if (busy) { studentPicker.value = selected; return; }
    selected = studentPicker.value; $('feedback').textContent = ''; render();
  };
  const heading = document.createElement('div'); heading.className = 'review-heading'; heading.append(studentPicker, textNode('span', `${visible.indexOf(student) + 1} / ${visible.length} · ${task.codex_run ? 'Codex 双模型复核' : task.mode === 'real' ? '真实扫描件' : '虚构数据'}`, 'badge')); $('review').append(heading);
  const questions = student.review_all_questions ? student.questions : findings(student);
  let questionIndex = 0;
  const navigation = document.createElement('div'); navigation.className = 'question-navigation';
  const questionLabel = textNode('span', ''); questionLabel.setAttribute('aria-live', 'polite');
  const previousQuestion = button('← 上一题', () => {
    questionIndex = (questionIndex - 1 + questions.length) % questions.length;
    show(questions[questionIndex]);
  }, 'next-question');
  const nextQuestion = button('下一题 →', () => {
    questionIndex = (questionIndex + 1) % questions.length;
    show(questions[questionIndex]);
  }, 'next-question');
  nextQuestion.disabled = questions.length < 2;
  previousQuestion.disabled = questions.length < 2;
  const questionControls = document.createElement('div'); questionControls.append(previousQuestion, nextQuestion);
  navigation.append(questionLabel, questionControls); navigation.hidden = !questions.length;
  const detail = document.createElement('div'); $('review').append(navigation, detail);
  function show(question) {
    const reference = task.reference_materials?.questions?.[question.number];
    const questionImages = reference ? reference.question_images : (question.images || []).filter(image => image.kind === 'question');
    const answerImages = reference ? reference.answer_images : (question.images || []).filter(image => image.kind === 'answer');
    questionLabel.textContent = `第${question.number}题${question.status === 'correct' ? ' · 模型判定正确，待人工复核' : ''}`;
    detail.replaceChildren();
    const source = document.createElement('details'); source.className = 'question-original';
    source.append(textNode('summary', '查看原题'));
    if (!questionImages.length) source.append(textNode('p', '原题截图待补充。', 'muted'));
    for (const image of questionImages) {
      source.append(textNode('p', image.source, 'source'));
      const open = button('', () => openScan(image.path, image.source), 'scan-button');
      const img = document.createElement('img'); img.src = image.path; img.alt = image.source + '，点击放大'; img.style.maxWidth = '600px';
      img.loading = 'lazy'; open.append(img); source.append(open);
    }
    detail.append(source);
    const grid = document.createElement('div'); grid.className = 'comparison';
    for (const kind of ['student', 'answer']) {
      const card = document.createElement('section'); card.className = 'answer-card'; card.append(textNode('h3', kind === 'student' ? '学生答卷原图' : '官方答案原图'));
      const images = kind === 'answer' ? answerImages : (question.images || []).filter(image => image.kind === kind);
      if (kind === 'student' && !images.length && student.submission_pages?.length) {
        const pages = student.submission_pages;
        let pageIndex = 0;
        const pageLabel = textNode('p', '', 'source');
        const pageImage = document.createElement('img'); pageImage.loading = 'lazy';
        const open = button('', () => openScan(pages[pageIndex].path, pageLabel.textContent), 'scan-button');
        open.append(pageImage);
        function showPage() {
          pageLabel.textContent = `完整答卷 · 第${pageIndex + 1}/${pages.length}页（未逐题定位，请翻页核对）`;
          pageImage.src = pages[pageIndex].path; pageImage.alt = pageLabel.textContent;
        }
        const back = button('← 上一页答卷', () => { pageIndex = (pageIndex - 1 + pages.length) % pages.length; showPage(); }, 'next-question');
        const forward = button('下一页答卷 →', () => { pageIndex = (pageIndex + 1) % pages.length; showPage(); }, 'next-question');
        back.disabled = forward.disabled = pages.length < 2;
        card.append(pageLabel, back, forward, open); showPage();
        grid.append(card); continue;
      }
      if (!images.length) card.append(textNode('p', '暂无原图，不使用文字或生成图片替代。', 'muted'));
      for (const image of images) {
        card.append(textNode('p', image.source, 'source')); const open = button('', () => openScan(image.path, image.source), 'scan-button');
        const img = document.createElement('img'); img.src = image.path; img.alt = image.source + '，点击放大';
        img.onerror = () => { open.textContent = '原图加载失败'; open.disabled = true; }; open.append(img); card.append(open);
      }
      grid.append(card);
    }
    detail.append(grid);
  }
  if (questions.length) show(questions[0]);
  else detail.append(textNode('p', '没有需逐题查看的问题，请复核建议评分和评语。', 'muted'));
  const saved = drafts[draftKey(student)];
  const form = document.createElement('div'); form.className = 'review-fields';
  form.innerHTML = '<div><label for="score">建议评分</label><input id="score" class="score" type="number" min="0" max="100" step="0.5" required><span class="outof">/100</span></div><div><label for="comment">评语草稿</label><textarea id="comment"></textarea></div>';
  $('review').append(form); $('score').value = saved?.score ?? student.proposed_score ?? '';
  if (student.late_days === null) form.before(textNode('p', '迟交时长缺失或无效，请人工核对后评分。', 'help'));
  else if (student.late_days > 0) form.before(textNode('p', `迟交按 ${student.late_days} 天计，不足一天按一天算，每天扣 0.5 分；本次实际扣 ${student.late_deduction} 分（与错题扣分累加，最低 95 分）。`, 'help'));
  $('comment').value = saved?.comment != null && (saved.submitted || saved.comment !== student.comment)
    ? saved.comment : suggestedComment(student);
  const internalNotes = student.questions.filter(q => q.reason?.startsWith('两级模型判断不同'));
  if (internalNotes.length) {
    const notes = document.createElement('details');
    notes.append(textNode('summary', '助教复核提示（不发送给对方）'));
    for (const q of internalNotes) notes.append(textNode('p', `第${q.number}题：${q.reason}`, 'help'));
    form.before(notes);
  }
  if (student.model_comparison?.['6'] && student.model_comparison?.['5.6']) {
    const six = student.model_comparison['6'], older = student.model_comparison['5.6'];
    const countChanges = pair => pair.questions.filter((q, i) => q.status !== pair.grader_questions[i].status).length;
    const differences = six.questions.filter((q, i) => q.status !== older.questions[i].status).map(q => `第${q.number}题`);
    const olderScore = older.submission_mismatch ? '暂不定分' : `${Math.max(95, 100 - older.questions.filter(q => q.status === 'incorrect').length)}分`;
    const compare = document.createElement('details'); compare.className = 'model-comparison';
    compare.append(textNode('summary', `模型对照 · 6版批改/审查分歧${countChanges(six)}题 · 5.6版批改/审查分歧${countChanges(older)}题`));
    compare.append(textNode('p', `6版：${six.grader} 批改 → ${six.reviewer} 审查；5.6版：${older.grader} 批改 → ${older.reviewer} 审查。5.6版建议评分：${olderScore}。`, 'help'));
    compare.append(textNode('p', differences.length ? `两版最终判断不同：${differences.join('、')}。` : '两版最终判断一致。', 'help'));
    form.after(compare);
  } else if (student.model_comparison?.['5.6']) {
    const pair = student.model_comparison['5.6'];
    const differences = pair.questions.filter((q, i) => q.status !== pair.grader_questions[i].status).length;
    form.after(textNode('p', `${pair.grader} 批改 → ${pair.reviewer} 审查；两级判断不同 ${differences} 题，已交给人工核对。`, 'help'));
  }
  const real = task.mode === 'real';
  if (student.questions.some(q => q.status === 'uncertain' || q.status === 'missing')) form.before(textNode('p', student.proposed_score == null ? '提交内容与作业不对应，暂不能给出建议评分。' : '这是按已确认错误计算的建议评分；待核对项处理后再确认最终成绩。', 'help'));
  const actions = document.createElement('div'); actions.className = 'actions';
  actions.append(textNode('p', real ? '点击“确认并提交”即将当前评分和评语提交到 Canvas，核验成功后显示“已提交”。空评语只提交成绩。' : '示例模式：确定后仅保存本地复核结果。', 'help'));
  const remoteStatus = textNode('p', '', 'help'); actions.append(remoteStatus);
  const identity = {assignment_url: task.assignment_url, student_id: student.id,
    review_revision: student.review_revision || task.review_revision};
  let remoteSnapshot = null;
  const scoreField = $('score'), commentField = $('comment');
  async function api(path, body) {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Canvas 请求失败，草稿已保留。');
    return result;
  }
  const controls = document.createElement('div'); controls.className = 'action-buttons';
  const confirm = button(real ? '读取 Canvas 状态…' : (saved?.confirmed ? '已确定' : '确定'), async () => {
    if (busy || !scoreField.reportValidity() || (real && !remoteSnapshot)) return;
    const key = draftKey(student);
    const draft = {score:scoreField.value, comment:commentField.value, confirmed:!real};
    const updated = {...drafts, [key]:draft};
    try { localStorage.setItem(storageKey, JSON.stringify(updated)); } catch { $('feedback').textContent = '本地保存失败，请重试。'; return; }
    drafts = updated;
    if (!real) {
      $('feedback').textContent = student.name + '：示例结果已保存在本地。';
      confirm.textContent = '已确定'; confirm.disabled = true; return;
    }
    busy = true; studentPicker.disabled = true; $('assignmentDate').disabled = true; confirm.disabled = true; previous.disabled = true; next.disabled = true;
    scoreField.disabled = true; commentField.disabled = true; $('start').disabled = true;
    confirm.textContent = '提交并核验中…'; $('feedback').textContent = '';
    try {
      const result = await api('/api/submit-review', {...identity, ...draft, snapshot:remoteSnapshot});
      if (result.state !== 'submitted') throw Error('Canvas 未确认提交结果，请核验后再操作。');
      remoteSnapshot = result.snapshot;
      drafts[key] = {...draft, submitted:true, snapshot:result.snapshot, comment_id:result.comment_id};
      confirm.textContent = '已提交';
      $('feedback').textContent = student.name + '：评分和评语已提交到 Canvas，并已回读核验。';
      if (!draft.comment.trim()) $('feedback').textContent = student.name + '：成绩已提交并核验（未发送评语）。';
      try { localStorage.setItem(storageKey, JSON.stringify(drafts)); }
      catch { $('feedback').textContent += ' 浏览器状态保存失败，但 Canvas 已提交成功。'; }
      remoteStatus.textContent = `Canvas 已核验成绩：${result.score} / 100`;
    } catch (error) {
      $('feedback').textContent = error.message;
      confirm.textContent = '提交 / 核验 Canvas'; confirm.disabled = false;
    } finally {
      busy = false; studentPicker.disabled = false; $('assignmentDate').disabled = false; previous.disabled = next.disabled = visible.length < 2;
      scoreField.disabled = false; commentField.disabled = false; $('start').disabled = false;
    }
  }, 'primary'); confirm.disabled = real || !!saved?.confirmed;
  const previous = button('上一个', () => {
    selected = visible[(visible.indexOf(student) - 1 + visible.length) % visible.length].id;
    $('feedback').textContent = ''; render();
    $('review').scrollIntoView({behavior:'smooth', block:'start'});
  }); previous.disabled = visible.length < 2;
  const next = button('下一个', () => {
    selected = visible[(visible.indexOf(student) + 1) % visible.length].id;
    $('feedback').textContent = ''; render();
    $('review').scrollIntoView({behavior:'smooth', block:'start'});
  }); next.disabled = visible.length < 2;
  async function refreshRemote() {
    remoteSnapshot = null;
    confirm.disabled = true;
    try {
      const result = await api('/api/review-state', identity);
      if (!confirm.isConnected) return;
      remoteSnapshot = result.snapshot;
      remoteStatus.textContent = `Canvas 当前成绩：${result.score ?? '未评分'}；已有 ${result.comment_count} 条评论。`;
      const currentDraft = drafts[draftKey(student)];
      const receipt = result.receipt;
      if (receipt && (!currentDraft || currentDraft.submitted)) {
        scoreField.value = String(receipt.score); commentField.value = receipt.comment;
      }
      const submitted = !!receipt && scoreField.value.trim() !== '' && Number(scoreField.value) === receipt.score
        && commentField.value.replace(/\r\n/g, '\n').trim() === receipt.comment;
      if (submitted) {
        drafts[draftKey(student)] = {score:scoreField.value, comment:commentField.value,
          submitted:true, snapshot:remoteSnapshot, comment_id:receipt.comment_id};
        try { localStorage.setItem(storageKey, JSON.stringify(drafts)); } catch { /* Canvas receipt remains authoritative. */ }
      }
      confirm.textContent = submitted ? '已提交' : '确认并提交'; confirm.disabled = !!submitted;
    } catch (error) {
      if (confirm.isConnected) { remoteStatus.textContent = error.message; confirm.textContent = '暂不能提交'; }
    }
  }
  controls.append(previous, confirm, next); actions.append(controls); $('review').append(actions);
  if (real) refreshRemote();
  for (const id of ['score', 'comment']) $(id).oninput = () => {
    confirm.textContent = real ? '确认并提交' : '确定'; confirm.disabled = real && !remoteSnapshot; $('feedback').textContent = '';
    drafts[draftKey(student)] = {score:$('score').value, comment:$('comment').value};
    try { localStorage.setItem(storageKey, JSON.stringify(drafts)); } catch { $('feedback').textContent = '草稿暂存失败，请保持页面打开。'; }
  };
}
async function loadBatch() {
  const entered = $('assignment').value.trim().replace(/\/$/, '');
  const response = await fetch('/api/assignment-review', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({assignment_url:entered})});
  const result = await response.json();
  if (!response.ok) throw Error(result.error || '加载作业结果失败。');
  const batch = result.batch;
  if (!batch) {
    task = null; selected = null; render();
    $('progress').hidden = false; $('progress').textContent = '这份作业尚未批改。点击“批量修改作业”，自动匹配题库与官方答案并开始批改；缺少资料时会提示具体题号。';
    return;
  }
  if (!batch.yxin_usage && !batch.codex_run) throw Error('现有结果是旧模型测试，尚未生成双模型批改与审查结果。');
  if (entered && entered !== batch.assignment_url) throw Error('现有结果不属于输入的作业链接，请让 Agent 批改该作业。');
  task = batch; selected = null;
  try { selected = sessionStorage.getItem('canvas-review-position:' + batch.assignment_url); } catch { /* Start with the first student. */ }
  $('assignment').value = batch.assignment_url; render();
}
async function selectAssignment(url) {
  if (busy) return;
  busy = true; $('assignmentDate').disabled = true; $('start').disabled = true; $('assignment').disabled = true;
  $('assignment').value = url; $('assignmentDate').value = url;
  task = null; selected = null; render();
  $('error').textContent = ''; $('feedback').textContent = ''; $('progress').hidden = true;
  $('missingButton').hidden = false; $('missingButton').textContent = '未提交';
  try {
    await loadBatch();
    try { localStorage.setItem('canvas-selected-assignment', url); } catch { /* Selection still works without storage. */ }
    window.history?.replaceState(null, '', 'review-workbench.html?assignment=' + encodeURIComponent(url));
  } catch (error) { $('error').textContent = error.message; }
  finally { busy = false; $('assignmentDate').disabled = false; $('start').disabled = false; $('assignment').disabled = false; }
}
async function initializeAssignments() {
  try {
    const response = await fetch('/api/assignments', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || '作业列表加载失败。');
    const entries = result.assignments;
    $('assignmentDate').replaceChildren();
    for (const item of entries) {
      const option = textNode('option', item.title); option.value = item.assignment_url;
      $('assignmentDate').append(option);
    }
    if (!entries.length) throw Error('当前课程暂无作业。');
    const requested = new URLSearchParams(window.location.search).get('assignment');
    let saved = null;
    try { saved = localStorage.getItem('canvas-selected-assignment'); } catch { /* Default to the first assignment. */ }
    const url = entries.find(item => item.assignment_url === requested)?.assignment_url
      || entries.find(item => item.assignment_url === saved)?.assignment_url || entries[0].assignment_url;
    await selectAssignment(url);
    if (result.warning) $('error').textContent = result.warning;
  } catch (error) {
    $('error').textContent = error.message;
    $('assignmentDate').replaceChildren(textNode('option', '作业列表暂不可用'));
    render();
  }
}
$('assignmentDate').onchange = () => selectAssignment($('assignmentDate').value);
$('startForm').onsubmit = async event => {
  event.preventDefault(); if (busy) return;
  busy = true; $('assignmentDate').disabled = true; $('error').textContent = ''; $('start').disabled = true; $('assignment').disabled = true;
  $('missingButton').hidden = false; $('missingButton').disabled = true;
  $('progress').hidden = false; $('progress').textContent = '正在更新题库索引、匹配原题与官方答案，并读取 Canvas 答卷…';
  try {
    const preparedResponse = await fetch('/api/prepare-batch', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({assignment_url:$('assignment').value.trim()})});
    const prepared = await preparedResponse.json();
    if (!preparedResponse.ok) throw Error(prepared.error || 'Canvas 答卷采集失败，未开始批改。');
    $('missingButton').textContent = `未提交（${prepared.unsubmitted_count}）`;
    const matched = prepared.reference_match;
    const referenceStatus = matched ? `已匹配${matched.chapter_title}第 ${matched.question_numbers.join('、')} 题；` : '';
    const rosterStatus = prepared.total_count != null ? `全班 ${prepared.total_count} 人，已提交 ${prepared.submitted_count} 人，未提交 ${prepared.unsubmitted_count} 人。` : '';
    $('progress').textContent = `${rosterStatus}${referenceStatus}已读取 ${prepared.submitted_count} 份答卷、${prepared.attachment_count} 个附件、${prepared.page_count} 页；正在比对已有结果并批改新交或更新的答卷…`;
    const response = await fetch('/api/grade-yxin', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({assignment_url:$('assignment').value.trim()})});
    if (!(response.headers.get('Content-Type') || '').includes('application/json')) throw Error('请使用 tools/serve.py 启动批改服务。');
    const result = await response.json();
    if (!response.ok) throw Error(result.error || '批改或审查失败，原结果保留。');
    task = result; selected = null; render();
    try {
      const rosterResponse = await fetch('/api/assignment-roster', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({assignment_url:$('assignment').value.trim()})});
      if (rosterResponse.ok) {
        const roster = await rosterResponse.json();
        $('missingButton').textContent = `未提交（${roster.unsubmitted.length}）`;
      }
    } catch { /* The reminder page will display the Canvas connection error. */ }
    const summary = result.run_summary;
    const usage = result.yxin_usage?.totals;
    $('progress').textContent = rosterStatus + (summary?.no_change
      ? `没有新交或更新的答卷；复用 ${summary.reused_count} 份已有批改结果，本次未调用模型。`
      : usage?.total_tokens != null
        ? `新批改 ${summary?.graded_count ?? result.students.length} 份 · 复用 ${summary?.reused_count ?? 0} 份 · 本次输入 ${usage.prompt_tokens} · 输出 ${usage.completion_tokens} · 合计 ${usage.total_tokens} token`
        : `新批改 ${summary?.graded_count ?? result.students.length} 份 · 复用 ${summary?.reused_count ?? 0} 份；接口未返回完整 token 用量。`);
  } catch (error) {
    $('error').textContent = error.message; $('progress').hidden = true;
  } finally { busy = false; $('assignmentDate').disabled = false; $('start').disabled = false; $('assignment').disabled = false; $('missingButton').disabled = false; }
};
$('missingButton').onclick = () => {
  const assignment = $('assignment').value.trim();
  if (assignment) window.location.href = 'unsubmitted.html?assignment=' + encodeURIComponent(assignment);
};
initializeAssignments();
