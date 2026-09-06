#!/usr/bin/env node
/** Real local API acceptance. No keys, model fixtures, or fabricated strategies. */
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { isDeepStrictEqual } from 'node:util';
import assert from 'node:assert/strict';
import { request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';

const args = process.argv.slice(2);
if (args.includes('--help')) {
  console.log('Usage: node scripts/probe_dialogue_acceptance.mjs [--dialogues D01,D02] [--output DIR] [--cases FILE] [--base LOCAL_URL] [--continue-independent] [--self-check]');
  process.exit(0);
}
const selfCheck = args.includes('--self-check');
const stopOnFailure = !args.includes('--continue-independent') || args.includes('--stop-on-failure');
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const base = option('--base', 'http://127.0.0.1:8011');
if (!['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) throw Error('Local API only');
const plan = selfCheck ? { dialogues: [] }
  : JSON.parse(readFileSync(option('--cases', 'docs/local-acceptance-100-cases.json'), 'utf8'));
const selected = option('--dialogues', '').split(',').filter(Boolean);
const out = resolve(option('--output', `var/acceptance-100/${Date.now()}`));
if (!selfCheck) mkdirSync(out, { recursive: true, mode: 0o700 });
const config = { capacityMode: 'point_in_time_volume', participationRate: 0.05,
  slippageBps: 5, allocationRatio: 1, limitHandling: 'wait_for_unlock', commissionRate: 0.0003,
  minimumCommissionCny: 5, retryUnfilledExits: true, maxExitAttempts: 20,
  warmupCalendarDays: 180, settlementExtensionDays: 14, runRobustness: true };
const sessions = new Map();
const records = [];
let activeRecord;
const save = () => writeFileSync(join(out, 'results.json'), JSON.stringify({
  schema: 'real-dialogue-acceptance.v1', base, startedAt, updatedAt: new Date().toISOString(),
  note: 'Mechanical checks are not semantic or UX acceptance. Review every original response.', records,
}, null, 2), { mode: 0o600 });
const startedAt = new Date().toISOString();
const clone = value => value === undefined ? undefined : structuredClone(value);
const sameStrategy = (left, right) => Boolean(left && right && isDeepStrictEqual(left, right));
// Explicit plan dependencies, not guesses made by parsing Chinese user text.
const resultDependentInputs = new Set([
  '005', '014', '020', '039', '045', '055', '056', '057', '060',
  '068', '085', '093', '094', '095', '097', '099',
]);

function freezeTarget(state, turn) {
  const selection = turn.after?.find(item => ['choose_proposal', 'choose_optimization'].includes(item.kind));
  if (!selection) return undefined;
  const index = selection.index ?? 1;
  const choice = selection.kind === 'choose_proposal'
    ? state.draft?.idea_route?.proposals?.[index - 1]
    : state.review?.optimizationCandidates?.[index - 1];
  const strategy = choice?.strategy ?? (choice?.id === state.draft?.suggested_strategy_choice_id
    ? state.draft?.suggested_strategy : undefined);
  return clone({
    kind: selection.kind, index, choice, strategy,
    symbol: choice?.instrument_symbol ?? strategy?.instrument?.symbol,
    sourceDraft: state.draft, sourceRunId: state.reviewRunId,
    dslVerification: strategy ? 'exact_public_dsl' : 'unavailable_needs_semantic_review',
  });
}

function prerequisites(turn, state, target) {
  const issues = [];
  const latest = state.runs?.at(-1);
  const needsResult = ['rerun', 'optimize_rerun'].includes(turn.outcome)
    || resultDependentInputs.has(turn.id)
    || turn.after?.some(item => item.kind === 'wait_for_review');
  if (needsResult && latest?.state !== 'succeeded') issues.push('no successful preceding run');
  if (target && (!target.choice?.id || !target.symbol || !target.sourceDraft)) {
    issues.push(`missing real ${target.kind} at index ${target.index}`);
  }
  if (target?.kind === 'choose_optimization'
    && (!target.strategy || !target.sourceRunId || target.sourceRunId !== latest?.id)) {
    issues.push('optimization must contain public DSL and belong to the latest successful run');
  }
  return issues;
}

function targetIssues(target, current) {
  if (!target) return [];
  const issues = [];
  if (current?.strategy?.instrument?.symbol !== target.symbol) issues.push('selected symbol differs from frozen target');
  if (target.strategy && !sameStrategy(target.strategy, current?.strategy)) {
    issues.push('selected DSL differs from the frozen real candidate');
  }
  return issues;
}

function plannedActions(turn, draft, inputFailed) {
  const actions = [...(turn.after ?? [])];
  if (!inputFailed && draft?.status === 'ready'
      && (['rerun', 'optimize_rerun'].includes(turn.outcome) || draft.run_requested === true)) {
    actions.unshift({ kind: 'run', trigger: 'when_ready', role: 'acceptance' });
  }
  return actions;
}

async function request(method, path, body, headers = {}, observe = false) {
  const progressId = observe ? randomUUID() : undefined;
  let finished = false;
  const poll = progressId ? (async () => {
    while (!finished) {
      try {
        const response = await fetch(`${base}/api/v1/dialogue-progress/${progressId}`);
        if (response.ok && activeRecord) {
          const progress = await response.json();
          // Deliberately exclude the provider's private reasoning text.
          const events = progress.events.map(({ stage, message, elapsed_ms }) => ({ stage, message, elapsed_ms }));
          activeRecord.progress = events;
          const direction = events.find(e => e.stage === 'strategy_direction');
          if (direction) activeRecord.firstDirection ??= direction;
          save();
        }
      } catch { /* A progress endpoint is optional; the real POST remains authoritative. */ }
      await delay(2000);
    }
  })() : undefined;
  const started = Date.now();
  try {
    // Native fetch has a separate 300-second headers timeout, shorter than
    // the long-model deadline below. Use Node's HTTP client without that cap.
    const { status, data } = await new Promise((resolveResponse, reject) => {
      const url = new URL(`${base}${path}`);
      const send = url.protocol === 'https:' ? httpsRequest : httpRequest;
      const pending = send(url, {
        method, headers: { 'Content-Type': 'application/json', ...headers,
          ...(progressId ? { 'X-Dialogue-Progress-ID': progressId } : {}) },
        signal: AbortSignal.timeout(method === 'GET' ? 30000 : 900000),
      }, response => {
        const chunks = [];
        response.on('data', chunk => chunks.push(chunk));
        response.on('error', reject);
        response.on('end', () => {
          try {
            resolveResponse({ status: response.statusCode,
              data: JSON.parse(Buffer.concat(chunks).toString('utf8')) });
          } catch (error) { reject(error); }
        });
      });
      pending.on('error', reject);
      pending.end(body === undefined ? undefined : JSON.stringify(body));
    });
    if (activeRecord) activeRecord.calls.push({ method, path, status, ms: Date.now() - started });
    if (status < 200 || status >= 300) {
      const error = new Error(`HTTP ${status}: ${data.detail ?? data.message ?? data.title ?? 'request failed'}`);
      error.problem = data;
      throw error;
    }
    return data;
  } finally { finished = true; if (poll) await poll; }
}

async function textTurn(state, input) {
  const current = state.draft;
  const relatedRunIds = (state.runs ?? []).filter(item => item.state === 'succeeded')
    .slice(-20).map(item => item.id);
  const context = {
    ...(relatedRunIds.length ? { related_run_ids: relatedRunIds } : {}),
    ...(state.review && relatedRunIds.includes(state.review.runId) ? { related_review: {
      run_id: state.review.runId, response_hash: state.review.modelProvenance.responseHash,
    } } : {}),
  };
  let response;
  if (current?.status === 'needs_clarification') {
    response = await request('POST', `/api/v1/strategy-drafts/${current.draft_id}/revisions/${current.revision}/clarification-answers`,
      { answer: input, ...context }, {}, true);
  } else {
    response = await request('POST', '/api/v1/strategy-drafts', {
      utterance: input, instrument_context: null, as_of_date: option('--as-of', '2026-09-05'),
      ...context,
    }, current ? { 'X-Conversation-Parent-Draft-ID': current.draft_id } : {}, true);
  }
  state.draft = response.draft ?? response;
  if (state.draft.backtest_review) {
    state.review = state.draft.backtest_review;
    state.reviewRunId = state.review.runId;
  }
  return response;
}

async function run(state) {
  if (state.draft?.status !== 'ready') throw Error('prerequisite_unavailable: no ready draft');
  const runConfig = { ...config,
    ...(state.draft.refresh_data === true ? { refreshData: true } : {}) };
  const submitted = await request('POST', '/api/v1/backtest-runs', {
    strategy: state.draft.strategy, config: runConfig,
  });
  if (!submitted.id || submitted.replayed) throw Error('Expected a new run, not a replay');
  state.review = undefined;
  state.reviewRunId = undefined;
  const deadline = Date.now() + 900000;
  let final = submitted;
  while (!['succeeded', 'failed', 'cancelled'].includes(final.state)) {
    if (Date.now() > deadline) throw Error(`Run still pending: ${submitted.id}`);
    await delay(1000);
    final = await request('GET', `/api/v1/backtest-runs/${submitted.id}`);
  }
  const result = { id: final.id, state: final.state, error: final.error,
    draftId: state.draft.draft_id, revision: state.draft.revision,
    strategy: clone(state.draft.strategy), config: runConfig };
  state.runs ??= [];
  state.runs.push(result);
  if (final.state === 'succeeded') {
    result.summary = await request('GET', `/api/v1/backtest-runs/${final.id}/summary`);
    const series = await request('GET', `/api/v1/backtest-runs/${final.id}/series`);
    const trades = await request('GET', `/api/v1/backtest-runs/${final.id}/trades`);
    result.seriesCount = Array.isArray(series) ? series.length : series.points?.length;
    result.tradeCount = Array.isArray(trades) ? trades.length : trades.trades?.length;
  }
  return result;
}

async function action(state, item, target) {
  if (item.kind === 'run') return run(state);
  if (item.kind === 'wait_for_review') {
    const latest = state.runs?.at(-1);
    if (latest?.state !== 'succeeded') throw Error('prerequisite_unavailable: no successful result');
    state.review = await request('POST', `/api/v1/backtest-runs/${latest.id}/review`, undefined, {}, true);
    state.reviewRunId = latest.id;
    return state.review;
  }
  if (item.kind === 'choose_proposal') {
    const choice = target?.kind === item.kind ? target.choice : undefined;
    if (!choice) throw Error('prerequisite_unavailable: no frozen real proposal');
    // Use the current revision of the same conversation when it still awaits an answer.
    // Otherwise try the original real button context and report any stale-revision error.
    const source = state.draft?.status === 'needs_clarification'
      && state.draft.draft_id === target.sourceDraft.draft_id ? state.draft : target.sourceDraft;
    const response = await request('POST',
      `/api/v1/strategy-drafts/${source.draft_id}/revisions/${source.revision}/clarification-answers`,
      { answer: choice.id }, {}, true);
    state.draft = response.draft ?? response;
    if (state.draft.status !== 'ready' || targetIssues(target, state.draft).length) {
      throw Error('Frozen proposal recovery did not produce the selected ready strategy');
    }
    return { selectedId: choice.id, selectedSymbol: choice.instrument_symbol, response };
  }
  if (item.kind === 'choose_optimization') {
    const choice = target?.kind === item.kind ? target.choice : undefined;
    if (!choice?.strategy || !target.sourceDraft) throw Error('prerequisite_unavailable: no frozen real optimization');
    state.draft = await request('POST', `/api/v1/strategy-drafts/${target.sourceDraft.draft_id}/revisions`, {
      strategy: choice.strategy, utterance: choice.suggestedUtterance, recover_if_missing: true,
    });
    if (state.draft.status !== 'ready' || targetIssues(target, state.draft).length) {
      throw Error('Frozen optimization recovery did not preserve the exact candidate DSL');
    }
    return { selectedId: choice.id, draft: state.draft };
  }
  throw Error(`Unsupported action ${item.kind}`);
}

function mechanicalChecks(turn, previous, current, response, target) {
  const issues = [];
  if (['ready', 'rerun', 'optimize_rerun'].includes(turn.outcome) && current?.status !== 'ready') {
    issues.push(`expected ready, received ${current?.status}/${current?.diagnostic_code}`);
  }
  if (turn.outcome === 'clarify' && current?.status === 'ready') issues.push('ambiguous input unexpectedly accepted');
  if (turn.outcome === 'suggest' && !(current?.idea_route?.proposals?.length
      || current?.backtest_review?.optimizationCandidates?.length || response.data)) {
    issues.push('no structured choices or real data returned');
  }
  if (turn.outcome === 'answer' && previous?.strategy && current?.strategy
      && !sameStrategy(previous.strategy, current.strategy)) {
    issues.push('answer unexpectedly changed the strategy');
  }
  if (['answer', 'clarify', 'suggest'].includes(turn.outcome) && current?.run_requested) {
    issues.push('non-executing input unexpectedly requested a run');
  }
  if (['rerun', 'optimize_rerun'].includes(turn.outcome) && !current?.run_requested) {
    issues.push('model did not request execution; helper must not substitute for automatic rerun');
  }
  return [...issues, ...targetIssues(target, current)];
}

async function executePlan() {
let stopRequested = false;
let encounteredFailure = false;
for (const dialogue of plan.dialogues) {
  if (selected.length && !selected.includes(dialogue.id)) continue;
  if (existsSync(join(out, 'STOP'))) break;
  if (dialogue.reset) sessions.set(dialogue.session, {});
  const state = sessions.get(dialogue.session) ?? { blockedBy: 'missing_session_start' };
  sessions.set(dialogue.session, state);
  for (const turn of dialogue.turns) {
    if (existsSync(join(out, 'STOP'))) { stopRequested = true; break; }
    const previous = clone(state.draft);
    const target = freezeTarget(state, turn);
    const record = { id: turn.id, dialogue: dialogue.id, session: dialogue.session, input: turn.input,
      expect: turn.expect, expectedOutcome: turn.outcome, startedAt: new Date().toISOString(),
      previousDraft: previous, frozenTarget: target, calls: [], actions: [], verdict: 'running' };
    activeRecord = record;
    records.push(record); save();
    const missing = prerequisites(turn, state, target);
    if (state.blockedBy || missing.length) {
      record.verdict = state.blockedBy ? 'blocked_by_previous' : 'prerequisite_unavailable';
      record.blockedBy = state.blockedBy;
      record.prerequisiteIssues = missing;
      record.inputVerdict = 'not_sent';
    } else {
      console.log(`START ${turn.id} ${turn.input}`);
      try {
        record.response = await textTurn(state, turn.input);
        record.inputIssues = mechanicalChecks(turn, previous, state.draft, record.response, target);
        record.inputVerdict = record.inputIssues.length ? 'fail' : 'executed_needs_semantic_review';
      } catch (error) {
        record.inputVerdict = 'fail'; record.error = error.message; record.problem = error.problem;
      }
      record.verdict = record.inputVerdict;
      // This immutable original verdict gates recovery; an after-action failure cannot rewrite it.
      const inputFailed = record.inputVerdict === 'fail';
      if (turn.id === '094') record.noncacheCoverage = {
        covered: false, uiVerdict: 'needs_semantic_review',
        reason: 'Pending an explicit model refresh flag and a successful run with actual cache evidence.',
      };
      save();
      let recovered = false;
      for (const rawItem of plannedActions(turn, state.draft, inputFailed)) {
        const item = { role: 'acceptance',
          trigger: rawItem.kind === 'run' ? 'when_ready'
            : rawItem.kind === 'wait_for_review' ? 'when_result_available' : undefined,
          ...rawItem };
        if (item.trigger === 'on_input_failure' && !inputFailed) continue;
        if (item.trigger === 'after_recovery_ready' && (!recovered || state.draft?.status !== 'ready')) continue;
        if (inputFailed && item.role !== 'recovery' && !recovered) continue;
        if (item.trigger === 'when_ready' && state.draft?.status !== 'ready') continue;
        if (item.trigger === 'when_result_available' && state.runs?.at(-1)?.state !== 'succeeded') continue;
        if (item.kind === 'run' && record.actions.some(value => value.kind === 'run' && value.result)) continue;
        try {
          const value = await action(state, item, target);
          record.actions.push({ ...item, result: value });
          if (item.role === 'recovery' && item.kind.startsWith('choose_')) {
            recovered = state.draft?.status === 'ready' && !targetIssues(target, state.draft).length;
            record.recoveryReady = recovered;
          }
          if (item.kind === 'run' && value.state !== 'succeeded') record.verdict = 'fail';
        } catch (error) {
          record.actions.push({ ...item, error: error.message, problem: error.problem });
          record.verdict = 'fail';
        }
        save();
      }
      if (['rerun', 'optimize_rerun'].includes(turn.outcome)) {
        record.executionCovered = !inputFailed && record.actions.some(item => item.kind === 'run'
          && item.role !== 'recovery' && item.result?.state === 'succeeded');
        if (!inputFailed && !record.executionCovered) {
          record.verdict = 'fail';
          record.executionIssues = ['No successful new run for the original natural-language edit'];
        }
      }
      if (turn.id === '094') {
        const freshRun = record.actions.find(item => item.kind === 'run'
          && item.role !== 'recovery' && item.result?.state === 'succeeded')?.result;
        const source = freshRun?.summary?.dataProvenance;
        record.noncacheCoverage = {
          covered: state.draft?.refresh_data === true && freshRun?.config?.refreshData === true
            && source?.refreshRequested === true && source?.historyCacheStatus === 'forced'
            && (source?.indicatorSeries === 0 || (source?.indicatorCacheStatuses?.length === source?.indicatorSeries
              && source.indicatorCacheStatuses.every(value => value === 'forced'))),
          uiVerdict: 'not_checked_by_api_runner',
          evidence: source,
        };
        if (!record.noncacheCoverage.covered) record.verdict = 'fail';
      }
    }
    record.finishedAt = new Date().toISOString(); save();
    console.log(`DONE ${turn.id} ${record.verdict}`);
    activeRecord = undefined;
    if (['fail', 'prerequisite_unavailable', 'blocked_by_previous'].includes(record.verdict)) {
      encounteredFailure = true;
      state.blockedBy ??= record.id;
      if (stopOnFailure) stopRequested = true;
    }
  }
  if (stopRequested) break;
}
if (encounteredFailure) process.exitCode = 1;
if (stopRequested) console.log(`STOP failed baseline/dependency or requested; inspect ${join(out, 'results.json')}`);
console.log(`REPORT ${join(out, 'results.json')}`);
}

function runSelfChecks() {
  // Synthetic shapes exercise runner bookkeeping only; never submitted as product strategies.
  const strategy = { instrument: { symbol: 'example-a' }, entry: { token: 'entry-a' } };
  const state = { draft: { draft_id: 'source', revision: 1, status: 'ready', strategy },
    runs: [{ id: 'source-run', state: 'succeeded' }], reviewRunId: 'source-run',
    review: { optimizationCandidates: [{ id: 'candidate-a', strategy, suggestedUtterance: 'example' }] } };
  const turn = { id: '015', outcome: 'optimize_rerun', after: [
    { kind: 'choose_optimization', index: 1, trigger: 'on_input_failure', role: 'recovery' },
  ] };
  const target = freezeTarget(state, turn);
  assert.equal(target.choice.id, 'candidate-a');
  assert.deepEqual(prerequisites(turn, state, target), []);
  state.review.optimizationCandidates[0].id = 'mutated-later';
  assert.equal(target.choice.id, 'candidate-a');
  assert.equal(targetIssues(target, { strategy: { ...strategy, instrument: { symbol: 'example-b' } } }).length, 2);
  assert.deepEqual(targetIssues(target, { strategy: { entry: { token: 'entry-a' }, instrument: { symbol: 'example-a' } } }), []);
  assert.equal(plannedActions(turn, state.draft, false)[0].kind, 'run');
  assert.equal(plannedActions(turn, state.draft, true).some(item => item.role === 'acceptance'), false);
  assert.equal(mechanicalChecks(turn, state.draft, state.draft, {}, undefined).length, 1);
  assert.deepEqual(mechanicalChecks(turn, state.draft,
    { ...state.draft, run_requested: true }, {}, undefined), []);
  assert.equal(mechanicalChecks({ outcome: 'answer' }, state.draft,
    { ...state.draft, run_requested: true }, {}, undefined).length, 1);
  assert.equal(prerequisites(turn, { ...state, runs: [] }, target).length, 2);
  const proposal = freezeTarget({ draft: { draft_id: 'p', idea_route: { proposals: [
    { id: 'first', instrument_symbol: 'example-a' }, { id: 'second', instrument_symbol: 'example-b' },
  ] } } }, { after: [{ kind: 'choose_proposal', index: 2 }] });
  assert.equal(proposal.choice.id, 'second');
  assert.equal(proposal.dslVerification, 'unavailable_needs_semantic_review');
  assert.deepEqual(targetIssues(proposal, { strategy: { instrument: { symbol: 'example-b' } } }), []);
  console.log('Runner-only self-checks passed; no HTTP, model, data, or product acceptance calls.');
}

if (selfCheck) runSelfChecks();
else await executePlan();
