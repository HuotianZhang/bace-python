"""The service queues bench jobs and serves the console from one process.

Runs, read-backs and actions share the FIFO queue in `worker.RunWorker`.
Each job runs alone; another run request queues while one is active.
`wire.make_envelope` wraps events for subscribers under the payload policy.
`journal.Journal` appends journal envelopes to
`<out>/journal/<session>.jsonl`. The journal supplies last-used parameters,
settle times, shot times and the run index.

`pipeline` resolves a tree into a schedule; `executor` runs it. Illumination
loops own LED levels, a J-V source supplies V_oc to `bace` at the same level,
and J-V to `bace` boundaries require interlocked relay transitions
(`docs/service-contract.md`, sections 2 and 7).

One worker owns the VISA bus. VISA sessions are not thread-safe, and the relay
interlock requires a single sequence of calls. Concurrent bus access can
damage hardware. Power and HTTP temperature monitors can run beside jobs.
A direct GPIB temperature monitor reads only while holding the idle bus lock;
the worker feeds the heater watchdog during its own sleeps.

Dependencies point inward. `experiment` and `storage` never import this
package. Real drivers and `pyvisa` are imported lazily in `rigs` so `--sim`
needs no VISA backend. Only `app` and `__main__` require FastAPI or uvicorn."""
