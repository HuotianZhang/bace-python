// The API wrapper uses the service's same-origin `/ui` mount, requiring no configured
// base URL, CORS or proxy. `base` allows tests or another window to target a
// different address.

import { splitLines } from './assistant.js';

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
     * The service returns operator-facing reasons in `error`; FastAPI uses `detail`.
     * Reading both prevents a bare 409 from hiding the reason and remedy.
     */
    this.text = (body && (body.error || (typeof body.detail === 'string' ? body.detail : null)))
      || `${request} -> ${status}`;
    /**
     * Refusals carry `warn` or `crit` severity and a `refused` flag
     * (`docs/service-contract.md` §3).
     */
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

    // -- the service -------------------------------------------------------
    /**
     * The exit request aborts a run, parks the bench, closes the journal and ends the
     * process. Without `force`, an active run causes a 409
     * (`docs/service-contract.md` §9).
     */
    shutdown: (force = false) => call('POST', '/service/shutdown', { force }),

    // -- the bench ---------------------------------------------------------
    /**
     * The cached snapshot never touches VISA and is safe to request during a run
     * (`docs/service-contract.md` §4).
     */
    bench: () => call('GET', '/bench'),
    /**
     * The read-back request queues a worker job and returns 409 during an active run
     * (`docs/service-contract.md` §4).
     */
    read: ({ wait = true } = {}) => call('POST', '/bench/read', undefined, { wait }),
    /**
     * Explicit bench actions require operator requests (`docs/service-contract.md` §4).
     */
    action: (name, args, { wait = true } = {}) =>
      call('POST', `/bench/actions/${encodeURIComponent(name)}`, args || {}, { wait }),
    /**
     * A saved bench retains every resolved catalogue value and source in
     * `<out>/bench/<name>.json`. This preserves the edited layer across sessions.
     */
    saveBench: (name) => call('POST', '/bench/save', { name }),
    /** The listing includes saved values in descending save-time order. */
    savedBench: () => call('GET', '/bench/saved'),
    /**
     * Deletion applies only to saved benches. Recipes have no delete operation, and
     * run folders retain experimental records.
     */
    deleteBench: (name) => call('DELETE', `/bench/saved/${encodeURIComponent(name)}`),
    // Saved values return through `setParams`, the same validation path as field
    // edits. Invalid values receive field-specific refusals while other modules can
    // apply. `restoreBench` in `ui/views/bench.js` handles partial application.

    // -- the catalogue -----------------------------------------------------
    modules: () => call('GET', '/modules'),
    /** The `edited` layer. A `null` value resets that one parameter. */
    // The service merges one `[sample]` key; `null` restores the `run.toml` value.
    // The answer includes the session block, changes and active run. The run retains
    // its queued identity, which the panel must show (`docs/service-contract.md`
    // §3a).
    setSample: (values) => call('PUT', '/session/sample', values),
    setParams: (module, params) => call('PUT', `/modules/${encodeURIComponent(module)}/params`, params),
    /** The reset removes the complete edited layer. */
    resetParams: (module) => call('POST', `/modules/${encodeURIComponent(module)}/params/reset`),

    // -- runs --------------------------------------------------------------
    /** A manual run: one module node, the same code path a pipeline takes. */
    startRun: (module, params, name) => call('POST', '/runs', { module, params: params || {}, ...(name ? { name } : {}) }),
    stopRun: (runId, mode = 'after_shot') => call('POST', `/runs/${runId}/stop`, { mode }),
    /**
     * Resume answers only the open pause; `temperature_k` binds its subtree
     * (`docs/service-contract.md` §§2, 7).
     */
    resumeRun: (runId, detail) => call('POST', `/runs/${runId}/resume`, detail || {}),
    runs: (session) => call('GET', '/runs', undefined, session ? { session } : null),
    run: (runId) => call('GET', `/runs/${runId}`),
    /**
     * The data endpoint supplies full precision without decimation for completed-run
     * charts (`docs/service-contract.md` §6).
     */
    runData: (runId, node) => call('GET', `/runs/${runId}/data`, undefined, node ? { node } : null),

    // -- pipelines ---------------------------------------------------------
    /**
     * Dry run validates, estimates and schedules without touching instruments.
     * Optional `name` affects `folder_pattern`, so it must match Start to preview the
     * same destination (`docs/service-contract.md` §7).
     */
    validate: (tree, name) => call('POST', '/pipelines/validate', { tree, ...(name ? { name } : {}) }),
    startPipeline: (tree, name) => call('POST', '/pipelines', { tree, ...(name ? { name } : {}) }),
    lastPipeline: () => call('GET', '/pipelines/last'),
    savedPipelines: () => call('GET', '/pipelines/saved'),
    savePipeline: (tree, name) => call('POST', '/pipelines/save', { tree, name }),

    // -- the observers -----------------------------------------------------
    monitors: () => call('GET', '/monitors'),
    startMonitor: (kind, interval_s) => call('POST', `/monitors/${kind}`, { interval_s }),
    stopMonitor: (kind) => call('DELETE', `/monitors/${kind}`),
    /**
     * The power monitor's readings the service holds, `[ts, watts, trustworthy]` rows.
     */
    powerHistory: (since, limit) => call('GET', '/monitors/power/history', undefined, { since, limit }),
    clearPowerHistory: () => call('DELETE', '/monitors/power/history'),
    /** The same readings as a CSV file — a URL, for a download link. */
    powerHistoryCsv: () => `${root}/monitors/power/history.csv`,

    /** The ring over HTTP, for a client without a socket. */
    events: (since = 0) => call('GET', '/events', undefined, { since }),

    // -- the assistant -----------------------------------------------------
    assistantStatus: () => call('GET', '/assistant'),
    forgetConversation: (id) => call('DELETE', `/assistant/conversations/${id}`),
    /**
     * Each question streams NDJSON to `onFrame`; the promise settles at stream end.
     * The operator's key travels in a header, never the URL
     * (`docs/service-contract.md` §8a).
     */
    ask: async ({ text, conversation, key, provider = 'anthropic', model, base_url, onFrame, signal }) => {
      const path = '/assistant/chat';
      const response = await fetchImpl(root + path, {
        method: 'POST', signal,
        headers: { 'Content-Type': 'application/json', ...(key ? { 'X-API-Key': key } : {}) },
        body: JSON.stringify({ text, provider, ...(conversation ? { conversation } : {}),
                               ...(model ? { model } : {}), ...(base_url ? { base_url } : {}) }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new ApiError(response.status, body, `POST ${path}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let rest = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const out = splitLines(rest + decoder.decode(value, { stream: true }));
        rest = out.rest;
        out.frames.forEach(onFrame);
      }
      splitLines(`${rest}${decoder.decode()}\n`).frames.forEach(onFrame);
    },
  };
}

function queryString(query) {
  if (!query) return '';
  const parts = Object.entries(query)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(typeof v === 'boolean' ? String(v) : v)}`);
  return parts.length ? '?' + parts.join('&') : '';
}
