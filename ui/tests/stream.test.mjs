// The socket's four rules, each of which is a real bug in a client that does
// the obvious thing. The fake below is a WebSocket only in the two ways that
// matter: it records the URL it was opened with, and it lets a test deliver a
// frame or a close code.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createStream } from '../lib/stream.js';

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.closedWith = null;
    FakeSocket.opened.push(this);
  }
  static opened = [];
  static reset() { FakeSocket.opened = []; }
  static get last() { return FakeSocket.opened[FakeSocket.opened.length - 1]; }
  close(code) { this.closedWith = code ?? 1000; }
  deliver(frame) { this.onmessage({ data: JSON.stringify(frame) }); }
  drop(code) { this.onclose({ code }); }
}

const hello = (session, seq, startedAt = 1) => ({
  seq: null, ts: 1, run_id: null, node_path: '', type: 'Hello',
  data: { session: { id: session, started_at: startedAt }, seq,
          bench: { session: { id: session }, state: 'idle' } },
});
const frame = (seq, type = 'Notice') => ({ seq, ts: 1, run_id: null, node_path: '', type,
                                           data: { level: 'info', text: String(seq) }, decimated: {} });

function harness(options = {}) {
  FakeSocket.reset();
  const frames = [];
  const hellos = [];
  const changes = [];
  const timers = [];
  const stream = createStream({
    url: 'ws://bench/events',
    WebSocketImpl: FakeSocket,
    onFrame: (f) => frames.push(f),
    onHello: (f) => hellos.push(f),
    onSessionChange: (c) => changes.push(c),
    setTimeoutImpl: (fn) => { timers.push(fn); return timers.length; },
    clearTimeoutImpl: () => {},
    ...options,
  });
  return { stream, frames, hellos, changes, timers, fire: () => timers.shift()() };
}

test('a first connection asks for no replay and takes the cursor from Hello', () => {
  const { stream, frames } = harness();
  stream.start(null);
  assert.equal(FakeSocket.last.url, 'ws://bench/events', 'no ?since on a fresh page');

  FakeSocket.last.deliver(hello('S1', 120));
  assert.equal(stream.state.lastSeq, 120);

  FakeSocket.last.deliver(frame(119));
  assert.equal(frames.length, 0, 'a frame at or below the cursor is a duplicate');
  FakeSocket.last.deliver(frame(121));
  assert.equal(frames.length, 1);
  assert.equal(stream.state.stats.duplicates, 1);
});

test('a Hello on a reconnect must not swallow the replay', () => {
  // `data.seq` is the newest seq the service has; the replay that follows
  // carries lower ones. Adopting it here would drop everything we reconnected
  // for — the completed shots and the partial loop curve.
  const { stream, frames } = harness();
  stream.start(40);
  assert.equal(FakeSocket.last.url, 'ws://bench/events?since=40');

  FakeSocket.last.deliver(hello('S1', 400));
  assert.equal(stream.state.lastSeq, 40, 'the cursor stays where we asked from');
  FakeSocket.last.deliver(frame(41));
  FakeSocket.last.deliver(frame(42));
  assert.deepEqual(frames.map((f) => f.seq), [41, 42], 'the replay is delivered, not deduped away');
});

test('a restarted service is reconnected to with since=0, not without since', () => {
  // `since` is fixed when the socket opens and the server replays from it
  // *after* Hello, so by the time the client sees the new session id the stale
  // replay has already been computed. And `since=0` is not the same request as
  // no `since`: the replay is guarded by `if since is not None`, so an absent
  // parameter loses everything the new process emitted before we noticed.
  const { stream, changes } = harness();
  stream.start(null);
  FakeSocket.last.deliver(hello('S1', 120));
  const first = FakeSocket.last;

  first.deliver(hello('S2', 3));
  assert.equal(first.closedWith, 1000, 'the socket opened on the old cursor is closed');
  assert.equal(FakeSocket.last.url, 'ws://bench/events?since=0');
  assert.deepEqual(changes, [{ from: 'S1', to: 'S2' }]);
  assert.equal(stream.state.lastSeq, 0, 'the old session\'s cursor died with it; the new one starts at 0');

  FakeSocket.last.deliver(hello('S2', 3));
  assert.equal(stream.state.session, 'S2');
});

test('a socket we have replaced is not listened to any more', () => {
  // The service queues frames before we close the stale socket, and they can
  // still be dispatched afterwards. Folded, they would push the cursor above
  // zero — and the new socket's `since=0` replay would then be thrown away as
  // duplicates, losing the completed shots the reconnect exists to recover.
  const { stream, frames } = harness();
  stream.start(null);
  FakeSocket.last.deliver(hello('S1', 120));
  const stale = FakeSocket.last;

  stale.deliver(hello('S2', 3));                 // the service restarted
  assert.equal(FakeSocket.last.url, 'ws://bench/events?since=0');
  assert.equal(stream.state.lastSeq, 0);

  stale.deliver(frame(90));                      // in flight when we closed it
  assert.equal(stream.state.lastSeq, 0, 'the old session cannot move the new cursor');
  assert.equal(frames.length, 0);

  FakeSocket.last.deliver(hello('S2', 3));
  FakeSocket.last.deliver(frame(1));
  assert.deepEqual(frames.map((f) => f.seq), [1], 'and the replay from 0 still arrives');
});

test('a service restarted inside one second is still a new service', () => {
  // `session_id` is stamped to the whole second, so two processes started in
  // the same second share it — and a cursor kept across that restart would
  // discard every frame the new process sends until its counter passed the
  // old high: a bench that goes quietly stale. `started_at` separates them.
  const { stream, changes } = harness();
  stream.start(null);
  FakeSocket.last.deliver(hello('20260902_212747', 900, 1788384467.54));
  assert.equal(stream.state.lastSeq, 900);

  FakeSocket.last.deliver(hello('20260902_212747', 2, 1788384467.91));
  assert.equal(changes.length, 1, 'the same id, a different process');
  assert.equal(FakeSocket.last.url, 'ws://bench/events?since=0');
  assert.equal(stream.state.lastSeq, 0);
});

test('being dropped at 1008 reconnects at once, from the notice the service sent', () => {
  const { stream, frames } = harness();
  stream.start(null);
  FakeSocket.last.deliver(hello('S1', 900));
  const socket = FakeSocket.last;

  socket.deliver({ seq: null, ts: 1, run_id: null, node_path: '', type: 'Notice',
                   data: { level: 'warning', text: 'dropped: … reconnect with since=612', since: 612 },
                   decimated: {} });
  assert.equal(frames.length, 1, 'the drop notice is delivered, not deduped');
  assert.equal(stream.state.lastSeq, 612, 'the cursor follows the notice');

  socket.drop(1008);
  assert.equal(FakeSocket.last.url, 'ws://bench/events?since=612');
  assert.equal(stream.state.stats.drops, 1);
});

test('an ordinary close backs off and resumes from the cursor', () => {
  const { stream, timers, fire } = harness();
  stream.start(null);
  FakeSocket.last.deliver(hello('S1', 7));
  FakeSocket.last.drop(1006);
  assert.equal(timers.length, 1, 'a lost socket waits before retrying');
  fire();
  assert.equal(FakeSocket.last.url, 'ws://bench/events?since=7');
});

test('StepPhase passes the dedupe untouched', () => {
  // Live-only, `seq` null, never in the ring: a client that dedupes on seq
  // passes it through, and one that reconnects redraws from StepStarted.
  const { stream, frames } = harness();
  stream.start(null);
  FakeSocket.last.deliver(hello('S1', 500));
  FakeSocket.last.deliver({ seq: null, ts: 1, run_id: 'r', node_path: 'bace', type: 'StepPhase',
                            data: { index: 0, phase: 'acquire light', k: 3, of: 7 }, decimated: {} });
  assert.equal(frames.length, 1);
  assert.equal(stream.state.lastSeq, 500, 'an ephemeral frame consumes no number');
});

test('a gap in the ring is counted, not chased', () => {
  const { stream } = harness();
  stream.start(null);
  FakeSocket.last.deliver(hello('S1', 10));
  FakeSocket.last.deliver(frame(11));
  FakeSocket.last.deliver(frame(40));
  assert.equal(stream.state.stats.gaps, 1);
  assert.equal(stream.state.lastSeq, 40);
});
