(() => {
  window.monitorCalls = 0;
  window.monitorDelay = 0;
  window.fixtureHidden = false;
  window.logFailure = false;
  window.caseQueries = [];
  window.detail = {
    id: 'fixture-run', state: 'running', remote_model: 'deepseek-flash', resource_kind: 'aisbench_monitor',
    created_at: '2026-11-12T00:00:00Z', updated_at: '2026-11-12T00:00:00Z', run_mode: 'exploration',
    gate_status: 'not_applicable', execution_unresolved: false, events: [],
    report: { id: 'report-1', validity: 'complete', summary: { successful_samples: 5, failed_samples: 0, missing_samples: 0, accuracy: 80 } },
    cleanup: { state: 'not_required', attempt_count: 0, last_error_code: null },
    snapshot: { resource_kind: 'aisbench_monitor', remote_model: 'deepseek-flash', sample_count: 5, parameters: {} },
  };
  window.monitor = {
    collection_status: 'available', stale: false, source: 'local_monitor', task_name: 'ae-fixture/GPQA_diamond',
    process_id: 123, tool_status: 'inferencing', finish_count: 2, total_count: 5,
    observed_at: '2026-11-12T00:00:00Z', snapshot_kind: 'live', progress_description: 'infer', time_cost: '0:00:02',
  };
  window.cases = [
    { id: 'a1', dataset_key: 'GPQA', sample_id: 'case-1', attempt_no: 1, state: 'failed', error_code: 'UPSTREAM_TIMEOUT',
      evidence_available: true, evidence_sha256: 'f'.repeat(64), created_at: '2026-11-12T00:00:00Z', finished_at: null,
      tool_metadata: { tool_started_at: null, tool_finished_at: null, duration_sec: null, input_tokens: null, output_tokens: null, diagnostics: [], status: 'failed' } },
    { id: 'a2', dataset_key: 'GPQA', sample_id: 'case-1', attempt_no: 2, state: 'succeeded', error_code: null,
      evidence_available: true, evidence_sha256: 'a'.repeat(64), created_at: '2026-11-12T00:01:00Z', finished_at: '2026-11-12T00:01:05Z',
      tool_metadata: { tool_started_at: '2026-11-12T00:01:00Z', tool_finished_at: '2026-11-12T00:01:05Z', duration_sec: 5, input_tokens: 12, output_tokens: 3, diagnostics: [], status: 'succeeded' } },
  ];
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.fixtureHidden });
  window.fetch = async (input) => {
    const url = new URL(String(input), 'http://local');
    let data = [];
    let status = 200;
    if (url.pathname === '/api/autoeval/v1/runs/fixture-run/monitor') {
      window.monitorCalls += 1;
      if (window.monitorDelay) await new Promise(resolve => setTimeout(resolve, window.monitorDelay));
      data = window.monitor;
    } else if (url.pathname === '/api/autoeval/v1/runs/fixture-run/monitor/snapshots') {
      data = [{ id: 'snap-1', kind: window.monitor.snapshot_kind, sha256: 'abc', created_at: '2026-11-12T00:00:00Z' }];
    } else if (url.pathname === '/api/autoeval/v1/runs/fixture-run/monitor/snapshots/snap-1') {
      data = { projection: window.monitor, raw: { task_name: window.monitor.task_name } };
    } else if (url.pathname === '/api/autoeval/v1/runs/fixture-run/monitor/log') {
      if (window.logFailure) { status = 503; data = null; }
      else data = { content: 'fixture archived log', sha256: 'log-sha', source: 'archived_job_log' };
    } else if (url.pathname === '/api/autoeval/v1/runs/fixture-run/case-executions') {
      window.caseQueries.push(url.search);
      data = window.cases.filter(row => !url.searchParams.get('state') || row.state === url.searchParams.get('state'));
    } else if (url.pathname === '/api/autoeval/v1/case-executions/a1/evidence' || url.pathname === '/api/autoeval/v1/case-executions/a2/evidence') {
      const id = url.pathname.includes('a1') ? 'a1' : 'a2';
      data = { id, state: id === 'a1' ? 'failed' : 'succeeded', prompt: 'Fixture prompt', response: id === 'a1' ? null : 'A', error_code: id === 'a1' ? 'UPSTREAM_TIMEOUT' : null };
    } else if (url.pathname === '/api/autoeval/v1/runs/fixture-run/unified-output') {
      data = { validity: 'complete', observed_cases: 2, expected_cases: 2, missing_cases: 0, token_coverage: { known_attempts: 2, total_attempts: 2, input_tokens_known_sum: 12, output_tokens_known_sum: 3 } };
    } else if (url.pathname === '/api/autoeval/v1/runs/fixture-run') {
      data = window.detail;
    } else if (url.pathname === '/api/autoeval/v1/runs') {
      data = [window.detail];
    } else if (url.pathname.endsWith('/capabilities')) {
      data = { evaluation_enabled: false, analysis_enabled: false, customer_ssh_enabled: false, workspace_mode: 'single', subject: 'fixture' };
    }
    return new Response(JSON.stringify({ data, next_offset: null, request_id: 'fixture' }), {
      status, headers: { 'Content-Type': 'application/json' },
    });
  };
})();
