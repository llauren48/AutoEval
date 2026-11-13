// Mock analysis API only. No real evaluation, analysis, or model request is made.
(() => {
  const state = () => sessionStorage.getItem('analysisState') || 'not_queued';
  const report = () => ({
    id: 'analysis-fixture', run_id: 'fixture-run', remote_model: '测试模型',
    created_at: '2026-11-13T00:00:00Z', validity: 'complete', evidence_status: 'verified',
    summary: { expected_samples: 1, executed_attempts: 1, successful_samples: 1, failed_samples: 0, missing_samples: 0, accuracy: 100, subset_scope: 'lite', dataset_samples: 198, metric_scope: 'selected_manifest', scoring_status: 'succeeded' },
    baseline_comparison: { status: 'missing', baseline_key: null, baseline_version: null, baseline_value: null, baseline_unit: null, baseline_direction: null, measured_value: null, difference: null },
    analysis_status: state(), analysis_id: state() === 'not_queued' ? null : 'analysis-job',
    sha256: 'fixture', snapshot_sha256: 'fixture', run_mode: 'exploration',
    execution_origin: sessionStorage.getItem('analysisOrigin') || 'real', gate_status: 'not_applicable',
  });
  const analysis = () => ({
    id: 'analysis-job', state: state(), retry_count: 0, error_code: null,
    started_at: null, finished_at: null, result: null,
  });
  const response = (data, status = 200) => new Response(JSON.stringify({ data, next_offset: null }), {
    status, headers: { 'Content-Type': 'application/json' },
  });
  window.analysisCalls = [];
  window.fetch = async (url, options = {}) => {
    const href = String(url);
    if (href.includes('/reports?')) return response([report()]);
    if (href.endsWith('/analysis:start') || href.endsWith('/analysis:retry')) {
      window.analysisCalls.push({ href, method: options.method });
      sessionStorage.setItem('analysisState', 'queued');
      return response(analysis(), 202);
    }
    if (href.endsWith('/analysis')) return response(analysis());
    if (href.includes('capabilities')) return response({ evaluation_enabled: true });
    return response(report());
  };
})();
