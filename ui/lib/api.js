// Every route in `docs/service-contract.md`, wrapped once. The console is
// served by the service itself (`--ui DIR`, mounted at `/ui`, same origin), so
// there is no base URL to configure, no CORS and no proxy — `base` exists only
// so a test or a second window can point somewhere else.

export class ApiError extends Error {
  constructor(status, body, request) {
    super(`${request} -> ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
    /** The check list a 422 carries, so a caller can render it without parsing. */
    this.checks = (body && (body.checks || (body.detail && body.detail.checks))) || null;
    /** What FastAPI puts in `detail`, when it is a sentence rather than checks. */
    this.detail = body && body.detail;
    /**
     * The service's own sentence, which is written for this screen: *"the
     * 33220A output is ON; the chain fix is made with the LED off -- led-off
     * first, set the polarity, then set-led-pulse"*. Its handlers answer
     * `{"error": …}` (`app.py:123`) and FastAPI's own answer `{"detail": …}`,
     * so a client that reads only one of the two shows `-> 409` to an
     * operator instead of the reason and the remedy.
     */
    this.text = (body && (body.error || (typeof body.detail === 'string' ? body.detail : null)))
      || `${request} -> ${status}`;
    /** `warn` | `crit` on a refusal, and `true` on `refused`: §3's loudness. */
    this.level = (body && body.level) || null;
    this.refused = Boolean(body && body.refused);
  }
}

export function createApi({ base = '', fetch: fetchImpl = globalThis.fetch } = {}) {
  const root = base.replace(/\/$/, '');

  async function call(method, path, body, query) {
    const url = root + path + queryString(query);
    const init = { method, headers: {} };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const response = await fetchImpl(url, init);
    const text = await response.text();
    let parsed = null;
    if (text) {
      try { parsed = JSON.parse(text); } catch { parsed = { detail: text }; }
    }
    if (!response.ok) throw new ApiError(response.status, parsed, `${method} ${path}`);
    return parsed;
  }

  return {
    base: root,
    call,

    // -- the bench ---------------------------------------------------------
    /** The cached snapshot. Never touches VISA; safe to call at any time. */
    bench: () => call('GET', '/bench'),
    /** Enqueue a read-back job. 409 while a run is active. */
    read: ({ wait = true } = {}) => call('POST', '/bench/read', undefined, { wait }),
    /** One of the explicit actions; the service never performs them itself. */
    action: (name, args, { wait = true } = {}) =>
      call('POST', `/bench/actions/${encodeURIComponent(name)}`, args || {}, { wait }),

    // -- the catalogue -----------------------------------------------------
    modules: () => call('GET', '/modules'),
    /** The `edited` layer. A `null` value resets that one parameter. */
    setParams: (module, params) => call('PUT', `/modules/${encodeURIComponent(module)}/params`, params),
    /** Drop the whole edited layer. */
    resetParams: (module) => call('POST', `/modules/${encodeURIComponent(module)}/params/reset`),

    // -- runs --------------------------------------------------------------
    /** A manual run: one module node, the same code path a pipeline takes. */
    startRun: (module, params, name) => call('POST', '/runs', { module, params: params || {}, ...(name ? { name } : {}) }),
    stopRun: (runId, mode = 'after_shot') => call('POST', `/runs/${runId}/stop`, { mode }),
    /** Answers the pause that is open and no other; `temperature_k` binds the subtree. */
    resumeRun: (runId, detail) => call('POST', `/runs/${runId}/resume`, detail || {}),
    runs: (session) => call('GET', '/runs', undefined, session ? { session } : null),
    run: (runId) => call('GET', `/runs/${runId}`),
    /** Full precision, no decimation — what the charts draw from once a run ends. */
    runData: (runId, node) => call('GET', `/runs/${runId}/data`, undefined, node ? { node } : null),

    // -- pipelines ---------------------------------------------------------
    /** The Dry run button: validates, costs and schedules, and touches nothing. */
    validate: (tree) => call('POST', '/pipelines/validate', { tree }),
    startPipeline: (tree, name) => call('POST', '/pipelines', { tree, ...(name ? { name } : {}) }),
    lastPipeline: () => call('GET', '/pipelines/last'),
    savedPipelines: () => call('GET', '/pipelines/saved'),
    savePipeline: (tree, name) => call('POST', '/pipelines/save', { tree, name }),

    // -- the observers -----------------------------------------------------
    monitors: () => call('GET', '/monitors'),
    startMonitor: (kind, interval_s) => call('POST', `/monitors/${kind}`, { interval_s }),
    stopMonitor: (kind) => call('DELETE', `/monitors/${kind}`),

    /** The ring over HTTP, for a client without a socket. */
    events: (since = 0) => call('GET', '/events', undefined, { since }),
  };
}

function queryString(query) {
  if (!query) return '';
  const parts = Object.entries(query)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(typeof v === 'boolean' ? String(v) : v)}`);
  return parts.length ? '?' + parts.join('&') : '';
}
