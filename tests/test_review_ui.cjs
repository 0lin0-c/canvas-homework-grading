const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup({failSubmit = false, localConfirmed = false, receipt = null, reviews = {}} = {}) {
  const ids = new Map(), requests = [], buttons = [];
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.disabled = false; this.isConnected = true; this.value = ''; }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    before() {} after() {} scrollIntoView() {}
    reportValidity() { return true; }
    setAttribute() {}
    set innerHTML(value) {
      if (value.includes('id="score"')) {
        ids.set('score', new Element('input')); ids.set('comment', new Element('textarea'));
      }
    }
  }
  for (const id of ['workspace', 'empty', 'review', 'feedback', 'start', 'startForm', 'assignment', 'assignmentDate', 'missingButton', 'error', 'progress']) ids.set(id, new Element('div'));
  const url = 'https://oc.sjtu.edu.cn/courses/1/assignments/2';
  const key = url + ':real:1:now:r1';
  const stored = new Map([['canvas-error-review-drafts-v1', JSON.stringify(localConfirmed ? {[key]: {score:'97', comment:'人工评语', confirmed:true}} : {})]]);
  const context = vm.createContext({
    document: {getElementById: id => ids.get(id), createElement: tag => { const e = new Element(tag); if (tag === 'button') buttons.push(e); return e; }},
    localStorage: {getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value)},
    sessionStorage: {getItem: () => null, setItem() {}},
    window: {location: {}},
    fetch: async (url, options) => {
      const body = JSON.parse(options.body); requests.push({url, body});
      if (url === '/api/assignment-review') return {ok:true, json: async () => ({batch:reviews[body.assignment_url] || null})};
      if (url === '/api/review-state') return {ok:true, json: async () => ({snapshot:'before', score:receipt?.score ?? null, comment_count:2, receipt:body.student_id === '1' ? receipt : null})};
      return {ok:!failSubmit, json: async () => failSubmit ? {error:'测试：待核验'} : {state:'submitted', snapshot:'after', score:97, comment_id:'50'}};
    }
  });
  let script = fs.readFileSync(path.join(__dirname, '../prototypes/error-review.js'), 'utf8');
  script = script.replace('initializeAssignments();', '');
  vm.runInContext(script, context);
  vm.runInContext(`task = ${JSON.stringify({mode:'real', assignment_url:url, review_revision:'r1', students:[{id:'1', name:'测试学生', submitted_at:'now', proposed_score:100, comment:'模型草稿', questions:[]}]})}; render();`, context);
  return {ids, requests, buttons, stored, key, context};
}

test('batch button matches references before grading and displays matched scope', async () => {
  const ui = setup();
  await new Promise(resolve => setImmediate(resolve));
  const stages = [];
  ui.context.fetch = async (url, options) => {
    stages.push(url);
    if (url === '/api/prepare-batch') {
      assert.match(ui.ids.get('progress').textContent, /更新题库索引/);
      return {ok:true, json:async () => ({submitted_count:1, attachment_count:1, page_count:2,
        unsubmitted_count:0, reference_match:{chapter_title:'习题一', question_numbers:[21,23]}})};
    }
    assert.equal(url, '/api/grade-yxin');
    assert.match(ui.ids.get('progress').textContent, /已匹配习题一第 21、23 题/);
    return {ok:false, headers:{get:() => 'application/json'}, json:async () => ({error:'测试停止于模型调用前'})};
  };
  await ui.ids.get('startForm').onsubmit({preventDefault() {}});
  assert.deepEqual(stages, ['/api/prepare-batch', '/api/grade-yxin']);
  assert.equal(ui.ids.get('start').disabled, false);
});

test('missing reference error prevents model request and restores batch button', async () => {
  const ui = setup();
  await new Promise(resolve => setImmediate(resolve));
  const stages = [];
  ui.context.fetch = async url => {
    stages.push(url);
    return {ok:false, json:async () => ({error:'习题二匹配未完成；第1题：缺少官方答案'})};
  };
  await ui.ids.get('startForm').onsubmit({preventDefault() {}});
  assert.deepEqual(stages, ['/api/prepare-batch']);
  assert.match(ui.ids.get('error').textContent, /第1题：缺少官方答案/);
  assert.equal(ui.ids.get('start').disabled, false);
  assert.equal(ui.ids.get('assignmentDate').disabled, false);
});

test('legacy local confirmation keeps draft and still requires Canvas submission', async () => {
  const ui = setup({localConfirmed:true});
  await new Promise(resolve => setImmediate(resolve));
  const submit = ui.buttons.find(b => b.textContent === '确认并提交');
  assert.ok(submit); assert.equal(submit.disabled, false);
  assert.equal(ui.ids.get('comment').value, '人工评语');
  assert.equal(ui.ids.get('score').value, '97');
  await submit.onclick();
  assert.equal(submit.textContent, '已提交'); assert.equal(submit.disabled, true);
  const sent = ui.requests.find(r => r.url === '/api/submit-review').body;
  assert.equal(sent.comment, '人工评语'); assert.equal(sent.score, '97');
  assert.equal(sent.student_id, '1'); assert.equal(sent.snapshot, 'before');
  const saved = JSON.parse(ui.stored.get('canvas-error-review-drafts-v1'))[ui.key];
  assert.equal(saved.submitted, true); assert.equal(saved.snapshot, 'after');
  ui.ids.get('comment').value = '修改后的评语'; ui.ids.get('comment').oninput();
  assert.equal(submit.disabled, false);
  assert.equal(JSON.parse(ui.stored.get('canvas-error-review-drafts-v1'))[ui.key].submitted, undefined);
});

test('failed submission preserves manual draft and never displays submitted', async () => {
  const ui = setup({failSubmit:true, localConfirmed:true});
  await new Promise(resolve => setImmediate(resolve));
  const submit = ui.buttons.find(b => b.textContent === '确认并提交');
  await submit.onclick();
  assert.equal(submit.textContent, '提交 / 核验 Canvas');
  assert.equal(ui.ids.get('feedback').textContent, '测试：待核验');
  const saved = JSON.parse(ui.stored.get('canvas-error-review-drafts-v1'))[ui.key];
  assert.equal(saved.comment, '人工评语'); assert.equal(saved.submitted, undefined);
  assert.equal(ui.ids.get('score').disabled, false);
});

test('automatic feedback addresses the recipient and excludes internal model discussion', () => {
  const ui = setup();
  const result = vm.runInContext(`suggestedComment({comment:'1. 第1题：学生写成了并集。\\n2. 第2题：两级模型判断不同。请人工核对。\\n3. 第6题：学生计算的是0.125。'})`, ui.context);
  assert.equal(result, '1. 第1题：你写成了并集。\n2. 第6题：你计算的是0.125。');
});

test('previous and next question both navigate the current findings', () => {
  const ui = setup();
  vm.runInContext(`task.students[0].questions = [1, 2, 6].map(number => ({number, status:'incorrect', images:[]})); render();`, ui.context);
  const prev = ui.buttons.find(b => b.textContent === '← 上一题' && !b.disabled);
  const next = ui.buttons.find(b => b.textContent === '下一题 →' && !b.disabled);
  const label = ui.ids.get('review').children[1].children[0];
  assert.equal(label.textContent, '第1题');
  next.onclick(); assert.equal(label.textContent, '第2题');
  prev.onclick(); assert.equal(label.textContent, '第1题');
  prev.onclick(); assert.equal(label.textContent, '第6题');
});

test('regenerated all-correct review shows questions and original answer pages', () => {
  const ui = setup();
  vm.runInContext(`task.students[0].review_all_questions = true;
    task.students[0].questions = [1,2].map(number => ({number,status:'correct',images:[]}));
    task.students[0].submission_pages = [{path:'page1.png'},{path:'page2.png'}]; render();`, ui.context);
  const navigation = ui.ids.get('review').children[1];
  assert.equal(navigation.hidden, false);
  assert.match(navigation.children[0].textContent, /第1题/);
  const detail = ui.ids.get('review').children[2];
  const card = detail.children[1].children[0];
  assert.match(card.children[1].textContent, /第1\/2页/);
  card.children[3].onclick();
  assert.match(card.children[1].textContent, /第2\/2页/);
  assert.equal(card.children[4].children[0].src, 'page2.png');
});

test('returning to a submitted student restores the verified receipt without browser cache', async () => {
  const ui = setup({receipt:{score:97, comment:'已提交评语', comment_id:'50'}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.ids.get('score').value, '97');
  assert.equal(ui.ids.get('comment').value, '已提交评语');
  vm.runInContext(`task.students.push({...task.students[0],id:'2'}); render();`, ui.context);
  await new Promise(resolve => setImmediate(resolve));
  ui.buttons.findLast(b => b.textContent === '下一个').onclick();
  await new Promise(resolve => setImmediate(resolve));
  ui.buttons.findLast(b => b.textContent === '上一个').onclick();
  await new Promise(resolve => setImmediate(resolve));
  const submit = ui.buttons.findLast(b => b.className === 'primary');
  assert.equal(submit.textContent, '已提交'); assert.equal(submit.disabled, true);
  assert.equal(ui.requests.filter(r => r.url === '/api/submit-review').length, 0);
});

test('verified receipt never overwrites a different unsent manual draft', async () => {
  const ui = setup({localConfirmed:true, receipt:{score:96, comment:'先前评语', comment_id:'50'}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.ids.get('score').value, '97');
  assert.equal(ui.ids.get('comment').value, '人工评语');
  const submit = ui.buttons.findLast(b => b.className === 'primary');
  assert.equal(submit.textContent, '确认并提交'); assert.equal(submit.disabled, false);
});

test('date switching loads the matching assignment and restores its own manual draft', async () => {
  const a = 'https://oc.sjtu.edu.cn/courses/1/assignments/2';
  const b = 'https://oc.sjtu.edu.cn/courses/1/assignments/3';
  const batch = url => ({mode:'real', assignment_url:url, review_revision:'r1', yxin_usage:{},
    students:[{id:'1', name:'测试学生', submitted_at:'now', proposed_score:99.5, comment:'', questions:[]}]});
  const ui = setup({localConfirmed:true, reviews:{[a]:batch(a), [b]:batch(b)}});
  await new Promise(resolve => setImmediate(resolve));
  await vm.runInContext(`selectAssignment(${JSON.stringify(b)})`, ui.context);
  assert.equal(ui.ids.get('assignment').value, b);
  assert.equal(ui.ids.get('score').value, 99.5);
  ui.ids.get('score').value = '98.5'; ui.ids.get('score').oninput();
  await vm.runInContext(`selectAssignment(${JSON.stringify(a)})`, ui.context);
  assert.equal(ui.ids.get('score').value, '97');
  assert.equal(ui.ids.get('comment').value, '人工评语');
  await vm.runInContext(`selectAssignment(${JSON.stringify(b)})`, ui.context);
  assert.equal(ui.ids.get('score').value, '98.5');
  assert.equal(ui.stored.get('canvas-selected-assignment'), b);
  assert.equal(ui.requests.filter(r => r.url === '/api/submit-review').length, 0);
});

test('an ungraded date clears the previous result and switching is blocked while busy', async () => {
  const ui = setup();
  await new Promise(resolve => setImmediate(resolve));
  vm.runInContext('busy = true;', ui.context);
  await vm.runInContext("selectAssignment('https://oc.sjtu.edu.cn/courses/1/assignments/3')", ui.context);
  assert.equal(ui.requests.filter(r => r.url === '/api/assignment-review').length, 0);
  vm.runInContext('busy = false;', ui.context);
  await vm.runInContext("selectAssignment('https://oc.sjtu.edu.cn/courses/1/assignments/3')", ui.context);
  assert.equal(ui.ids.get('workspace').hidden, true);
  assert.equal(ui.ids.get('empty').hidden, false);
  assert.match(ui.ids.get('progress').textContent, /尚未批改/);
});
