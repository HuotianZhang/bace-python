"""Standalone instrument consoles: one instrument, one process, one port.

`docs/history/service-plan.md` fixed this shape for the two instruments that already
had their own programs -- "the existing standalone consoles stay standalone",
the 1918-C on :8918 and
the Lake Shore 331 on :8331 -- and `bace/drivers/newport1918c/console.py` and
`lakeshore331/console.py` are the *clients* the service reaches them through.
This package is the other half: consoles written here, in this repository, for
instruments the measurement service would otherwise be the only way to reach.

A console here is not part of `bace.service` and knows nothing about it. It
opens one instrument, serves one page, and needs neither the `service` extra
nor `ui/`:

    python -m bace.consoles.keithley --sim          # no hardware at all
    python -m bace.consoles.keithley                # the 2400 on GPIB0::24

**One process owns an instrument.** That is the rule the whole repository is
built on, and it is why these are separate programs rather than more tabs:
a console and the service cannot both hold the same GPIB session, and the one
that starts second gets a confusing error rather than a second session. So a
console is what you run *instead of* the service, on a bench where you want
the instrument by hand.
"""
