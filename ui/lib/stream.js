// The stream owns reconnect and deduplication for all views. Sequence cursors belong
// to service sessions, not sockets. Session changes require reopening with `since=0`
// because replay is chosen at socket creation; an absent query requests no replay.
// Hello has null sequence and cannot advance a replay cursor to `data.seq`, which
// would discard retained frames. Falling behind yields a Notice cursor and close
// 1008, requiring prompt reconnect rather than an operator error
// (`docs/service-contract.md` §6).

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
  /** `id` and `started_at` identify the service process together. */
  let incarnation = null;
  /**
   * The opening service sequence distinguishes replay from live events. Consumers
   * triggering refetches need this distinction to avoid treating each retained event
   * as a fresh action.
   */
  let head = null;
  /** The replay cursor requested for this socket is `null` when replay is disabled. */
  let asked = null;

  const stats = { frames: 0, gaps: 0, duplicates: 0, drops: 0, reconnects: 0, tracesGone: 0 };

  function status(state, detail) {
    onStatus({ state, session, lastSeq, stats: { ...stats }, ...(detail || {}) });
  }

  function open(since) {
    if (closed) return;
    asked = since;
    // Replay supplies only `seq > since`, so the requested cursor already classifies
    // lower sequences as duplicates when the socket opens (`docs/service-contract.md`
    // §6).
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
      // Replaced sockets can still deliver queued messages. Ignoring them prevents
      // another session's frames from advancing the reset cursor and discarding the
      // replacement socket's replay.
      if (ws !== socket) return;
      handle(parse(event.data));
    };
    ws.onerror = () => {};
    ws.onclose = (event) => {
      if (ws !== socket) return;              // The callback ignores a socket replaced
                                              // by a subsequent connection.
      socket = null;
      if (closed) return status('closed');
      if (event && event.code === 1008) {
        // Close 1008 follows a falling-behind Notice with a resume cursor. Immediate
        // reconnect minimizes missing live data.
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
      // Session IDs have whole-second precision and can collide across rapid
      // restarts. Pairing `started_at` with the ID distinguishes processes so an old
      // high cursor cannot discard another process's frames.
      const now = `${id}@${who.started_at ?? ''}`;
      if (incarnation !== null && now !== incarnation) {
        // A session change invalidates state and cursor. The socket must reopen
        // because its replay request targets a different session.
        const previous = session;
        session = id;
        incarnation = now;
        lastSeq = null;
        onSessionChange({ from: previous, to: id });
        const dead = socket;
        socket = null;
        try { dead && dead.close(); } catch { /* The socket can be closed before this callback. */ }
        stats.reconnects += 1;
        return open(0);
      }
      session = id;
      incarnation = now;
      head = frame.data ? frame.data.seq : null;
      if (asked === null || asked === undefined) {
        // Without requested replay, `data.seq` establishes the cursor. During replay
        // it must not advance it or retained frames would be discarded.
        lastSeq = frame.data ? frame.data.seq : null;
      }
      onHello(frame);
      status('live');
      return;
    }

    const seq = frame.seq;
    if (seq === EPHEMERAL_SEQ || seq === undefined) {
      // Null-sequence live frames bypass deduplication without advancing the cursor.
      // This includes the Notice explaining an imminent close
      // (`docs/service-contract.md` §3).
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
    // A sequence gap means the ring has discarded events. Status counts it;
    // reconnecting to the same cursor would only repeat the gap.
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
      try { dead && dead.close(); } catch { /* The socket can be closed before cleanup. */ }
      status('closed');
    },
    /** For the status line, and for the tests. */
    get state() { return { session, incarnation, lastSeq, head, asked, stats: { ...stats }, connected: !!socket }; },
    /** Offline replay can feed frames through the live receive path. */
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
