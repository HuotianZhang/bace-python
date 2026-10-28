// Every route in `docs/service-contract.md`, wrapped once. The console is
// served by the service itself (`--ui DIR`, mounted at `/ui`, same origin), so
// there is no base URL to configure, no CORS and no proxy — `base` exists only
// so a test or a second window can point somewhere else.

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

    // -- the service -------------------------------------------------------
    /**
     * End the service: any run is aborted, the bench parked, the journal
     * closed, the process ended. Without `force` a run holding the bench is
     * a 409 that names it.
     */
    shutdown: (force = false) => call('POST', '/service/shutdown', { force }),

    // -- the bench ---------------------------------------------------------
    /** The cached snapshot. Never touches VISA; safe to call at any time. */
    bench: () => call('GET', '/bench'),
    /** Enqueue a read-back job. 409 while a run is active. */
    read: ({ wait = true } = {}) => call('POST', '/bench/read', undefined, { wait }),
    /** One of the explicit actions; the service never performs them itself. */
    action: (name, args, { wait = true } = {}) =>
      call('POST', `/bench/actions/${encodeURIComponent(name)}`, args || {}, { wait }),
    /**
     * The whole bench to `<out>/bench/<name>.json` — every module of the
     * catalogue, resolved, value and source. The edited layer is a session,
     * and this is what outlives it.
     */
    saveBench: (name) => call('POST', '/bench/save', { name }),
    /** Every saved bench, newest first, values included. */
    savedBench: () => call('GET', '/bench/saved'),
    /** Delete one. The only file the console removes: a recipe has no delete
     *  and a run's folder is the record of an experiment. */
    deleteBench: (name) => call('DELETE', `/bench/saved/${encodeURIComponent(name)}`),
    // There is no `loadBench`. A saved value goes back onto the bench through
    // `setParams` — the same edit a card's field makes — so one the spec now
    // refuses is refused with the sentence the field would have given, and
    // the rest of the file still lands (`views/bench.js: applyPreset`).

    // -- the catalogue -----------------------------------------------------
    modules: () => call('GET', '/modules'),
    /** The `edited` layer. A `null` value resets that one parameter. */
    // One `[sample]` key at a time, merged in the service; `null` is the way
    // back to what `run.toml` opened with. The answer carries the whole
    // session block, what changed, and the run holding the bench — which
    // keeps the identity it was queued under, and which the panel says.
    setSample: (values) => call('PUT', '/session/sample', values),
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
    /**
     * The Dry run button: validates, costs and schedules, and touches nothing.
     * `name` only decides the folder the answer quotes (`folder_pattern`), so
     * it is optional — but a Dry run that named a different folder from the
     * Start beside it would be describing a different run.
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
    /** The power monitor's readings the service holds, `[ts, watts, trustworthy]` rows. */
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
     * One question. The answer is NDJSON, read as it arrives: `onFrame` gets
     * each frame, and the promise settles when the stream ends. The key is
     * the asker's and goes in a header, never in the URL.
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
