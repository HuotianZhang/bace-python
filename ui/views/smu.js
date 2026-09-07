// The SMU tab — the Keithley 2400 on its own.
//
// One card, no charts, no run: the instrument's front panel with the service
// between it and the browser. Everything it does is a bench action
// (`POST /bench/actions/smu-*`) or the display's own monitor
// (`POST /monitors/smu`), so it takes the worker like any by-hand action and
// is refused while a run holds it — which is the correct answer, because a run
// has the GPIB and has reset the instrument out from under any panel.
//
// The whole panel is `lib/smu.js`, so `keithley.html` — the same panel in a
// window of its own, for an operator working the probe station with the rest
// of the console somewhere else — is that module plus a socket.

import { h, fill } from '../lib/dom.js';
import { smuPanelModel, createSmuPanel } from '../lib/smu.js';

/**
 * The panel's actions against a service. Shared by this tab and
 * `keithley.html`, so the two cannot drift: `notify` is where a refusal goes
 * (the shell's strip here, the page's own line there), and `after` is how each
 * host gets the monitor list back — the action itself answers with the bench.
 */
export function smuContext({ api, store, notify, after = () => {} }) {
  let inflight = false;

  async function act(name, args) {
    if (inflight) return;
    inflight = true;
    try {
      const out = await api.action(name, args || {});
      if (out && out.bench) store.applyBench(out.bench, { readBack: true });
      if (out && out.result && out.result.refused) {
        notify(out.result.text || `${name} was refused`, out.result.level || 'warn');
      }
      return out;
    } catch (error) {
      notify(error.text || error.message, error.level === 'crit' ? 'crit' : 'warn');
      return null;
    } finally {
      inflight = false;
      await after();
    }
  }

  return {
    source: (patch) => act('smu-source', patch),
    // Two actions, not one with a flag: `smu-off` is the bench's existing
    // verb — the one Park uses and the one the chain strip offers — and a
    // second spelling of it would be a second thing to keep true.
    output: (on) => act(on ? 'smu-on' : 'smu-off'),
    read: () => act('smu-read'),
    async refresh(seconds) {
      if (inflight) return;
      inflight = true;
      try {
        // Stop first whatever is asked for: the service allows one monitor and
        // answers a second start with a 409, so an interval change is a stop
        // and a start. A 404 from the stop is the ordinary case — nothing was
        // running — and is not news.
        try { await api.stopMonitor('smu'); } catch (error) {
          if (error.status !== 404) throw error;
        }
        if (seconds !== null) await api.startMonitor('smu', seconds);
      } catch (error) {
        notify(error.text || error.message, 'warn');
      } finally {
        inflight = false;
        await after();
      }
    },
  };
}

export default {
  route: 'smu',
  title: 'keithley',

  mount(container, { store, api, notify }) {
    const refreshMonitors = async () => {
      try { store.applyMonitors((await api.monitors()).monitors); } catch { /* the next /bench says */ }
    };
    const panel = createSmuPanel(smuContext({ api, store, notify, after: refreshMonitors }));

    fill(container,
      h('h1', 'keithley',
        h('span.sub', 'the SourceMeter by itself — no module, no run, no file')),
      panel.el);

    // The monitor list is not on the stream and `GET /bench` carries it only
    // when something asks for one; the switch has to open lit if the service
    // is already reading, so the tab asks once on the way up.
    refreshMonitors();
    const off = store.subscribe((state) => panel.update(smuPanelModel(state)));
    return { dispose: off };
  },
};
