/* Stress-Test Agent -- UI behaviour.
   No framework, no build step. Everything the server returns is inserted with
   textContent, never innerHTML: the statement, the source and the generated
   programs are all untrusted text. */

'use strict';

const $ = (id) => document.getElementById(id);

const STAGES = ['COMPILE', 'JUDGE', 'ANALYZE', 'AUTHOR', 'VALIDATE', 'STRESS', 'SHRINK', 'EXPLAIN'];
const TERMINAL = ['DONE', 'FAILED', 'DEGRADED'];

/* ------------------------------------------------------------------ theme */

const savedTheme = localStorage.getItem('theme');
if (savedTheme) document.documentElement.setAttribute('data-theme', savedTheme);

$('theme-toggle').addEventListener('click', () => {
  const current = document.documentElement.getAttribute('data-theme');
  const isDark = current
    ? current === 'dark'
    : window.matchMedia('(prefers-color-scheme: dark)').matches;
  const next = isDark ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
});

/* ---------------------------------------------------------------- samples */

function addSample(input = '', output = '') {
  const row = document.createElement('div');
  row.className = 'sample';

  const inEl = document.createElement('textarea');
  inEl.rows = 3;
  inEl.className = 'io in';
  inEl.spellcheck = false;
  inEl.placeholder = 'sample input';
  inEl.value = input;

  const outEl = document.createElement('textarea');
  outEl.rows = 3;
  outEl.className = 'io out';
  outEl.spellcheck = false;
  outEl.placeholder = 'expected output';
  outEl.value = output;

  const drop = document.createElement('button');
  drop.type = 'button';
  drop.className = 'drop';
  drop.textContent = '×';
  drop.setAttribute('aria-label', 'Remove this sample');
  drop.addEventListener('click', () => row.remove());

  row.append(inEl, outEl, drop);
  $('samples').append(row);
}

function readSamples() {
  return Array.from(document.querySelectorAll('.sample'))
    .map((row) => ({
      input: row.querySelector('.in').value,
      output: row.querySelector('.out').value,
    }))
    .filter((s) => s.input.trim() && s.output.trim());
}

$('add-sample').addEventListener('click', () => addSample());
addSample();

/* -------------------------------------------------------------- example */

const EXAMPLE = {
  statement: `Balanced Partition

You are given n integers. Split all of them into two groups, so that every
integer belongs to exactly one group. A group may be empty.

Find the minimum possible absolute difference between the sum of the first
group and the sum of the second group.

Input
The first line contains a single integer n (1 <= n <= 18).
The second line contains n integers a_1, ..., a_n (1 <= a_i <= 1000).

Output
Print one integer: the minimum possible absolute difference.`,
  language: 'cpp',
  judge: 'WA',
  source: `#include <algorithm>
#include <iostream>
#include <vector>

int main() {
    int n;
    std::cin >> n;
    std::vector<int> a(n);
    for (int i = 0; i < n; i++) std::cin >> a[i];

    std::sort(a.begin(), a.end(), std::greater<int>());

    long long groupA = 0, groupB = 0;
    for (int i = 0; i < n; i++) {
        if (groupA <= groupB) groupA += a[i];
        else groupB += a[i];
    }

    std::cout << std::abs(groupA - groupB) << std::endl;
    return 0;
}`,
  samples: [['4\n1 2 3 4\n', '0\n'], ['1\n7\n', '7\n']],
};

$('load-example').addEventListener('click', () => {
  $('statement').value = EXAMPLE.statement;
  $('source').value = EXAMPLE.source;
  $('language').value = EXAMPLE.language;
  $('judge-says').value = EXAMPLE.judge;
  $('samples').replaceChildren();
  EXAMPLE.samples.forEach(([i, o]) => addSample(i, o));
  $('form-error').textContent = '';
});

/* --------------------------------------------------------------- stages */

function renderStages(seen) {
  const list = $('stages');
  list.replaceChildren();
  STAGES.forEach((name) => {
    const li = document.createElement('li');
    li.textContent = name.toLowerCase();
    if (seen.has(name)) li.dataset.state = seen.get(name);
    list.append(li);
  });
}

/* --------------------------------------------------------------- result */

function verdictClass(v) {
  if (v === 'AC') return 'v v-ok';
  if (v === 'WA' || v === 'CE' || v === 'RE') return 'v v-bad';
  return 'v v-warn';
}

function showResult(state, verdict, result, usage) {
  const block = $('verdict-block');
  block.hidden = false;

  const badge = $('verdict');
  badge.className = state === 'DEGRADED' ? 'v v-warn' : verdictClass(verdict);
  badge.textContent = state === 'DEGRADED' ? (verdict || '?') + ' (degraded)' : (verdict || state);

  $('usage').textContent = usage;

  const r = result || {};
  $('summary').textContent = r.explanation || r.degraded_reason || '';

  const ce = r.counterexample;
  const wrap = $('counterexample');
  if (ce) {
    wrap.hidden = false;
    $('ce-input').textContent = (ce.input || '').replace(/\s+$/, '');
    $('ce-expected').textContent = (ce.expected || '').trim();
    $('ce-actual').textContent = (ce.actual || '').trim() || '(no output)';
    $('ce-class').textContent = r.bug_class || 'unclassified';
    $('ce-why').textContent = r.explanation || '';
    $('ce-fix').textContent = r.suggested_fix || '';
    $('summary').textContent = r.rounds_run
      ? `Found in ${r.rounds_run} rounds, shrunk in ${r.shrink_steps} steps.`
      : '';
  } else {
    wrap.hidden = true;
  }
}

async function loadExtras(id) {
  try {
    const [aRes, sRes] = await Promise.all([
      fetch(`/submissions/${id}/artifacts`),
      fetch(`/submissions/${id}/steps`),
    ]);

    const arts = (await aRes.json()).artifacts || [];
    const box = $('artifacts');
    box.replaceChildren();
    arts
      .filter((a) => a.kind !== 'spec')
      .forEach((a) => {
        const div = document.createElement('div');
        div.className = 'artifact';
        const h = document.createElement('h4');
        h.textContent = a.revision ? `${a.kind} (revision ${a.revision})` : a.kind;
        const pre = document.createElement('pre');
        pre.textContent = a.content;
        div.append(h, pre);
        box.append(div);
      });
    $('artifacts-wrap').hidden = arts.length === 0;

    const steps = (await sRes.json()).steps || [];
    const body = $('log');
    body.replaceChildren();
    steps.forEach((s) => {
      const tr = document.createElement('tr');
      const cells = [
        [String(s.seq), 'seq'],
        [s.stage, ''],
        [s.kind, ''],
        [s.status, 'st-' + s.status],
        [Object.keys(s.output || {}).length ? JSON.stringify(s.output) : '', ''],
      ];
      cells.forEach(([text, cls]) => {
        const td = document.createElement('td');
        td.textContent = text;
        if (cls) td.className = cls;
        tr.append(td);
      });
      body.append(tr);
    });
    $('log-wrap').hidden = steps.length === 0;
  } catch (err) {
    /* extras are a nicety; a failure here must not blank the result */
  }
}

/* --------------------------------------------------------------- submit */

let stream = null;

$('submit-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('form-error').textContent = '';

  const payload = {
    statement: $('statement').value,
    language: $('language').value,
    source_code: $('source').value,
    problem_id: '',
    samples: readSamples(),
    official_tests: readSamples(),
    external_verdict: $('judge-says').value,
  };

  if (!payload.statement.trim() || !payload.source_code.trim()) {
    $('form-error').textContent = 'Statement and solution are both required.';
    return;
  }

  const run = $('run');
  run.disabled = true;
  run.textContent = 'Running…';
  if (stream) stream.close();

  $('idle').hidden = true;
  $('live').hidden = false;
  $('verdict-block').hidden = true;
  $('counterexample').hidden = true;
  $('artifacts-wrap').hidden = true;
  $('log-wrap').hidden = true;

  const seen = new Map();
  renderStages(seen);

  let id;
  try {
    const res = await fetch('/submissions', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const body = await res.json();
    id = body.id;
    $('submission-id').textContent = id.slice(0, 8);
  } catch (err) {
    $('form-error').textContent = `Could not submit: ${err.message}`;
    run.disabled = false;
    run.textContent = 'Run';
    return;
  }

  const finish = async (state, verdict, result) => {
    let usage = '';
    try {
      const s = await (await fetch(`/submissions/${id}`)).json();
      usage = `${s.tokens_used} tokens / ${s.llm_calls} calls`;
      if (!result) { result = s.result; verdict = s.verdict; state = s.state; }
    } catch (err) { /* fall back to what the stream gave us */ }

    for (const [k, v] of seen) if (v === 'active') seen.set(k, 'done');
    renderStages(seen);

    showResult(state, verdict, result, usage);
    await loadExtras(id);

    run.disabled = false;
    run.textContent = 'Run';
    if (stream) stream.close();
  };

  stream = new EventSource(`/submissions/${id}/stream`);

  stream.addEventListener('step', (msg) => {
    const step = JSON.parse(msg.data);
    if (STAGES.includes(step.stage)) {
      for (const [k, v] of seen) if (v === 'active') seen.set(k, 'done');
      seen.set(step.stage, step.status === 'fail' ? 'fail' : 'active');
      renderStages(seen);
    }
  });

  stream.addEventListener('done', (msg) => {
    const d = JSON.parse(msg.data);
    finish(d.state, d.verdict, d.result);
  });

  stream.onerror = () => {
    // The stream closes when the server finishes; fall back to a direct poll
    // rather than showing an error the user cannot act on.
    if (stream.readyState === EventSource.CLOSED) finish(null, null, null);
  };
});

/* ----------------------------------------------------------------- misc */

$('copy-input').addEventListener('click', async (event) => {
  try {
    await navigator.clipboard.writeText($('ce-input').textContent);
    event.target.textContent = 'copied';
    setTimeout(() => { event.target.textContent = 'copy failing input'; }, 1400);
  } catch (err) {
    event.target.textContent = 'copy failed';
  }
});

fetch('/healthz')
  .then((r) => r.json())
  .then((h) => { $('model-badge').textContent = h.model; })
  .catch(() => { $('model-badge').textContent = 'api unreachable'; });
