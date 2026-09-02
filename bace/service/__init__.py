"""The service: a thin shell around the engine, one process per bench.

It does four things and rewrites no measurement logic:

1. **Queue.** A run, a bench read-back and a bench action are all jobs on one
   FIFO queue (`worker.RunWorker`), executed strictly one at a time. A second
   click while a run is live is queued, not lost and not refused.
2. **Broadcast.** Every event a job yields is wrapped in an `Envelope`
   (`wire.make_envelope`) and fanned out to WebSocket subscribers under the
   payload policy in `wire.py`, the one place that decides what an event
   weighs on the wire.
3. **Journal.** Every envelope is appended to `<out>/journal/<session>.jsonl`
   (`journal.Journal`), the file the history queries -- last-used params,
   settle times, shot time, the run index -- are answered from.
4. **Execute the pipeline tree.** `pipeline.py` turns a tree into a flat
   schedule and `executor.py` runs it, applying the three bindings: an
   illumination loop owns `led_v`, `jv_bace` hands its V_oc to the `bace` at
   the same level, and every `jv_* <-> bace` boundary is a relay transition.

**The bench lock.** One `RunWorker` thread owns every VISA instrument, and
everything that touches VISA is a job on that thread. VISA sessions are not
thread-safe, and the relay interlock in `drivers.routing` only holds when a
single sequence of calls reaches it -- two threads on the bus is the one
software mistake on this rig that costs hardware rather than a dataset. The
only things allowed to run beside a job are observers that never touch the
bus: the power monitor and the temperature read-back, both over HTTP to
consoles that own their own instruments.

Dependencies point inward. `experiment/` and `storage/` never import this
package; `pyvisa` and the real drivers are imported lazily inside `rigs.py`
so `--sim` works on a machine with no VISA backend; FastAPI and uvicorn are
needed only by `app.py` and `__main__.py`, so every other module here
imports without them.
"""
