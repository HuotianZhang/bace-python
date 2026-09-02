// The WebSocket, and everything that can go wrong with it. No view knows about
// any of this: `docs/ui-plan.md` decision 2 puts the whole reconnect
// discipline in one module, because every rule below is a bug in a client that
// does the obvious thing.
//
// The four that matter:
//
//   * **`seq` is per service session, not per socket.** A restarted service
//     starts its counter at zero, so a cursor carried across the restart
//     breaks twice over: `?since=<the old high seq>` replays nothing, and then
//     every numbered frame the new process sends is below the cursor and gets
//     discarded — a bench that goes quietly stale until someone reloads.
//     `Hello` carries `data.session.id`, and its own envelope `seq` is null,
//     so it is exempt from the dedupe it configures.
//
//   * **Reconnect, not just reset.** `since` is a query parameter, fixed when
//     the socket opened; the server sends `Hello` and *then* replays from it.
//     By the time a client sees the new session id the stale replay has
//     already been computed. So a session change closes the socket and opens a
//     new one — with `since=0`, which is not the same request as no `since` at
//     all: the replay is guarded by `if since is not None`, so an absent
//     parameter loses everything the new process emitted before the client
//     noticed, which is exactly the completed shots this exists to recover.
//
//   * **A `Hello` on a reconnect must not advance the cursor.** `data.seq` is
//     the newest seq the service has; the replay that follows carries lower
//     ones. Adopting it would drop the whole replay — the one thing we
//     reconnected for. It is adopted only when we deliberately asked for no
//     replay, which is the first connection of a fresh page.
//
//   * **Falling behind is normal.** A `--sim --fast` scan outruns any socket:
//     the service sends a `Notice` carrying `data.since` and closes with 1008.
//     That is not an error to report, it is a reconnect to make.

export const EPHEMERAL_SEQ = null;

export function createStream({
  url = defaultUrl(),
  WebSocketImpl = globalThis.WebSocket,
  onHello = () => {},
  onFrame = () => {},
  onStatus = () => {},
  onSessionChange = () => {},
  retryDelays = [250, 500, 1000, 2000, 5000],
  setTimeoutImpl = globalThis.setTimeout,
  clearTimeoutImpl = globalThis.clearTimeout,
} = {}) {
  let socket = null;
  let closed = false;
  let retry = 0;
  let timer = null;

  /** The dedupe cursor. Belongs to `session`, and dies with it. */
  let lastSeq = null;
  let session = null;
  /** `id` plus `started_at`: which *incarnation* of the service this is. */
  let incarnation = null;
  /**
   * The seq the service was at when this socket opened. Everything at or below
   * it is replay — the ring catching us up — and everything above it is
   * happening now. A consumer that acts on a frame (re-reading the bench when
   * a run parks, say) needs to know which it is, or opening the console fires
   * one of those actions per historical frame.
   */
  let head = null;
  /** What we asked this socket to replay from; `null` means "no replay". */
  let asked = null;

  const stats = { frames: 0, gaps: 0, duplicates: 0, drops: 0, reconnects: 0, tracesGone: 0 };

  function status(state, detail) {
    onStatus({ state, session, lastSeq, stats: { ...stats }, ...(detail || {}) });
  }

  function open(since) {
    if (closed) return;
    asked = since;
    // Asking for a replay *is* a statement about the cursor: the server sends
    // frames with `seq > since`, so anything at or below it is a duplicate
    // from the moment the socket opens.
    if (since !== null && since !== undefined && lastSeq === null) lastSeq = since;
    const target = since === null || since === undefined ? url : `${url}?since=${since}`;
    status('connecting', { since });
    let ws;
    try {
      ws = new WebSocketImpl(target);
    } catch (error) {
      return schedule();
    }
    socket = ws;
    ws.onopen = () => { retry = 0; };
    ws.onmessage = (event) => {
      // A socket we have already replaced can still deliver what the service
      // queued before we closed it. Those frames belong to the session we just
      // walked away from: folded, they would advance the cursor past zero and
      // the new socket's `since=0` replay would then be discarded as
      // duplicates — losing exactly the completed shots the reconnect exists
      // to recover. `onclose` has always made this check; `onmessage` must too.
      if (ws !== socket) return;
      handle(parse(event.data));
    };
    ws.onerror = () => {};
    ws.onclose = (event) => {
      if (ws !== socket) return;              // a socket we already replaced
      socket = null;
      if (closed) return status('closed');
      if (event && event.code === 1008) {
        // Dropped for falling behind. The `Notice` before it said where to
        // resume; reconnect at once rather than backing off, because the run
        // is still going and every frame missed is a shot off the chart.
        stats.reconnects += 1;
        return open(lastSeq === null ? 0 : lastSeq);
      }
      schedule();
    };
  }

  function schedule() {
    if (closed) return;
    const delay = retryDelays[Math.min(retry, retryDelays.length - 1)];
    retry += 1;
    status('offline', { retry_in_ms: delay });
    timer = setTimeoutImpl(() => {
      stats.reconnects += 1;
      open(lastSeq === null ? null : lastSeq);
    }, delay);
  }

  function handle(frame) {
    if (!frame) return;

    if (frame.type === 'Hello') {
      const who = (frame.data && frame.data.session) || {};
      const id = who.id;
      // `session_id` is stamped to the whole second, so two processes started
      // inside the same second share it — and a cursor kept across that
      // restart would discard every frame the new process sends until its
      // counter passed the old high. `started_at` is the float the session
      // was actually created at, so the pair identifies the incarnation.
      const now = `${id}@${who.started_at ?? ''}`;
      if (incarnation !== null && now !== incarnation) {
        // The service restarted under us. Drop the cursor and the session
        // state, and reopen: this socket's replay was computed from a cursor
        // that matches nothing in the new session.
        const previous = session;
        session = id;
        incarnation = now;
        lastSeq = null;
        onSessionChange({ from: previous, to: id });
        const dead = socket;
        socket = null;
        try { dead && dead.close(); } catch { /* already gone */ }
        stats.reconnects += 1;
        return open(0);
      }
      session = id;
      incarnation = now;
      head = frame.data ? frame.data.seq : null;
      if (asked === null || asked === undefined) {
        // We asked for no replay, so we are current as of `data.seq`. On a
        // reconnect we did ask, and adopting it here would swallow the replay.
        lastSeq = frame.data ? frame.data.seq : null;
      }
      onHello(frame);
      status('live');
      return;
    }

    const seq = frame.seq;
    if (seq === EPHEMERAL_SEQ || seq === undefined) {
      // Live-only, never in the ring: `StepPhase`, and the drop `Notice`.
      // Passed through without touching the cursor — a client that dedupes
      // these away would drop the very notice that says why it is closing.
      if (isDropNotice(frame)) {
        stats.drops += 1;
        lastSeq = pickSince(frame, lastSeq);
        status('behind', { since: lastSeq });
      }
      stats.frames += 1;
      return onFrame(frame, { replay: false });
    }
    if (lastSeq !== null && seq <= lastSeq) {
      stats.duplicates += 1;
      return;
    }
    // A gap means the ring no longer held those frames — worth counting on
    // the status line, and not worth a second reconnect: replaying from a
    // cursor the ring has already passed would only report it again.
    if (lastSeq !== null && seq > lastSeq + 1) stats.gaps += 1;
    lastSeq = seq;
    stats.frames += 1;
    if (hasReplayedTraces(frame)) stats.tracesGone += 1;
    onFrame(frame, { replay: head !== null && seq <= head });
  }

  return {
    start(since = null) { closed = false; open(since); },
    close() {
      closed = true;
      if (timer) clearTimeoutImpl(timer);
      const dead = socket;
      socket = null;
      try { dead && dead.close(); } catch { /* already gone */ }
      status('closed');
    },
    /** For the status line, and for the tests. */
    get state() { return { session, incarnation, lastSeq, head, asked, stats: { ...stats }, connected: !!socket }; },
    /** Feed a frame in as if it had arrived — the offline replay uses this. */
    inject(frame) { handle(frame); },
  };
}

/** `decimated[name].replay` means: redraw the loop curve, the trace is gone. */
export function hasReplayedTraces(frame) {
  const decimated = frame && frame.decimated;
  if (!decimated) return false;
  return Object.values(decimated).some((entry) => entry && entry.replay === true);
}

export function isDropNotice(frame) {
  return frame.type === 'Notice' && frame.data && typeof frame.data.since === 'number';
}

function pickSince(frame, fallback) {
  const since = frame.data && frame.data.since;
  return typeof since === 'number' ? since : fallback;
}

function parse(data) {
  if (typeof data !== 'string') return null;
  try { return JSON.parse(data); } catch { return null; }
}

export function defaultUrl() {
  const { protocol, host } = globalThis.location || { protocol: 'http:', host: '127.0.0.1:8900' };
  return (protocol === 'https:' ? 'wss://' : 'ws://') + host + '/events';
}
