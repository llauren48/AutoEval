(() => {
  window.monitorCalls = 0;
  window.monitorDelay = 0;
  window.fixtureHidden = false;
  window.logFailure = false;
  window.detail = {
    id: 'fixture-run', state: 'running', remote_model: 'deepseek-flash', resource_kind: 'aisbench_monitor',
    created_at: '2026-11-12T00:00:00Z', updated_at: '2026-11-12T00:00:00Z', run_mode: 'exploration',
    gate_status: 'not_applicable', execution_unresolved: false, events: [], report: null,
    cleanup: { state: 'not_required', attempt_count: 0, last_error_code: null },
    snapshot: { resource_kind: 'aisbench_monitor', remote_model: 'deepseek-flash', sample_count: 5, parameters: {} },
  };
  window.monitor = {
    collection_status: 'available', stale: false, source: 'local_monitor', task_name: 'ae-fixture/GPQA_diamond',
    process_id: 123, tool_status: 'inferencing', finish_count: 2, total_count: 5,
    observed_at: '2026-11-12T00:00:00Z', snapshot_kind: 'live', progress_description: 'infer', time_cost: '0:00:02',
  };
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
      if (window.logFailure) status = 503;
      data = { content: 'fixture archived log', sha256: 'log-sha', source: 'archived_job_log' };
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
