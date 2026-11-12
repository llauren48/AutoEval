// Mock data only. Never used by the product or a real evaluation.
(() => {
    window.hostReady = false;
    window.generated = 0;
    const config = { id: 'config-1', config_key: 'gpqa', display_name: '验证配置', version: 1, version_label: 'v1', kind: 'standard', items: { GPQA: { scope: 'Lite', output: 64, concurrency: 1, temperature: 0, top_p: 1 } }, profile_bindings: { GPQA: 'profile-1' } };
    const target = { id: 'target-1', remote_model: 'deepseek-flash', base_url: 'https://api.deepseek.com', model_key: '验证模型', model_version: 'v1', default_config_version_id: 'config-1' };
    window.fetch = async (url, init = {}) => {
        const p = String(url);
        let data = [];
        if (p.endsWith('/runs') && init.method === 'POST') {
            window.generated++;
            throw Error('generation forbidden');
        }
        if (p.includes('/system/capabilities'))
            data = { evaluation_enabled: true };
        else if (p.includes('connection-tests'))
            data = { outcome: 'matched' };
        else if (p.includes('/validate'))
            data = { environment_ready: window.hostReady, evaluation_ready: window.hostReady };
        else if (p.includes('execution-spec'))
            data = { bindings_complete: true, spec: { items: [{ sample_count: 1, manifest_id: 'manifest-1', tool: { revision: 'fixed' } }] } };
        else if (p.includes('/target-bindings'))
            data = [target];
        else if (p.includes('/test-configs'))
            data = [config];
        else if (p.includes('/resources'))
            data = [{ id: 'resource-1', binding_key: '验证资源' }, { id: 'resource-2', binding_key: '备用验证资源' }];
        return new Response(JSON.stringify({ data, next_offset: null }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    };
})();
