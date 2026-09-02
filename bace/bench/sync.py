"""Where the extraction pulse sits inside the LED cycle.

The one thing the whole measurement rests on and that nothing has ever checked:
the 81150A must be armed when the light goes **off**, not when it comes on. BACE
holds the device at V_pre under illumination until it reaches its light V_oc,
then steps to V_coll once the carriers have stopped being generated. Arm on the
wrong edge and the extraction happens 135 ns after the LED turns *on*, in the
middle of generation — and the result still looks like a transient, still
integrates to a plausible charge, and still lands in the same files.

The chain, and why it can go wrong silently:

    33220A output ──► LED (via a fixed-gain amplifier, non-inverting)
    33220A Sync   ──► 81150A external arm  (EDGE, POS, 1.0 V, 10 kohm)
    81150A output ──► x4 amplifier ──► device      (delay + 47 ns after the arm)
    81150A Sync   ──► scope CHAN3 (the run's trigger)

`:OUTP:POL` on the 33220A is what makes a *rising* Sync edge mean "light off":
inverting the waveform flips which half of the cycle the LED is lit for, while
leaving the Sync alone (33220A User's Guide p.67 — "the Sync signal associated
with the waveform is not inverted"). Nothing in this package sets that polarity
and nothing read it back until now, so the run has been inheriting whatever the
last session left on the front panel.

**This stage answers it without trusting any of that.** It looks at the LED
drive itself rather than at the Sync, so no manual and no polarity setting has
to be believed: put the 33220A's main output on CHAN1 through a tee, take one
wide single-shot record covering a whole 2 ms period, and read off whether the
light was on or off either side of the arm edge.

Wiring for the stage:

    CHAN1  33220A main output, tee, 1 Mohm   the LED drive, 0.400 / 1.020 V
    CHAN2  device current                    already there, untouched
    CHAN3  81150A Sync                       already there, the arm instant
    CHAN4  81150A main output, tee, 1 Mohm   the extraction pulse (optional)

1 Mohm, not 50 ohm: a 50 ohm input would load both generator outputs.

The stage only ever touches the **scope**. It sets no levels, enables no output
and moves no relay, so it is safe to run with a sample connected — and it
restores every scope setting it changed.
"""
from __future__ import annotations

import contextlib

import numpy as np

LED_CHANNEL = 1
BIAS_CHANNEL = 4

# (query, how to write the answer back)
_SCOPE_STATE = (
    (":TIM:RANG?", ":TIM:RANG {};"),
    (":TIM:POS?", ":TIM:POS {};"),
    (":ACQ:POIN?", ":ACQ:POIN {};"),
    (":ACQ:AVER?", ":ACQ:AVER {};"),
    (":ACQ:AVER:COUN?", ":ACQ:AVER:COUN {};"),
    (":TRIG:SWE?", ":TRIG:SWE {};"),
    (":TRIG:EDGE:SOUR?", ":TRIG:EDGE:SOUR {};"),
    (":TRIG:EDGE:SLOP?", ":TRIG:EDGE:SLOP {};"),
)


def _channel_state(ch: int) -> tuple[tuple[str, str], ...]:
    return ((f":CHAN{ch}:DISP?", f":CHAN{ch}:DISP {{}};"),
            (f":CHAN{ch}:INP?", f":CHAN{ch}:INP {{}};"),
            (f":CHAN{ch}:RANG?", f":CHAN{ch}:RANG {{}};"),
            (f":CHAN{ch}:OFFS?", f":CHAN{ch}:OFFS {{}};"))


# -- analysis, pure ------------------------------------------------------
def _edges(y: np.ndarray, threshold: float) -> tuple[np.ndarray, np.ndarray]:
    above = np.asarray(y, dtype=float) > threshold
    if above.size < 2:
        return np.empty(0, int), np.empty(0, int)
    rise = np.flatnonzero(~above[:-1] & above[1:]) + 1
    fall = np.flatnonzero(above[:-1] & ~above[1:]) + 1
    return rise, fall



def _is_logic(y, min_swing_v: float) -> tuple[bool, float, float]:
    """Does this channel carry a two-level signal, or is it an unused input?

    Learned from the rig, 2026-09-01. CHAN3 was not carrying the 81150A Sync and
    swung 2.3 mV of noise. Edges were being found at the trace's own midpoint,
    which for noise with one outlier sits *below* the noise band — so every
    sample read as "high", exactly one rising edge was found (just after the
    outlier), and a verdict was reported from it. The outlier was the LED edge
    coupling into an unterminated input, which is why the phantom edge landed
    320 ns from a real LED edge and looked plausible.

    Two guards, and the first is the one that matters: a logic line swings
    volts, so anything under `min_swing_v` is not a signal. The second asks
    whether the samples actually cluster at two levels — noise is unimodal and
    spreads across the whole span, a pulse train puts ~everything on one rail or
    the other.
    """
    y = np.asarray(y, dtype=float)
    ptp = float(y.max() - y.min())
    if ptp < min_swing_v:
        return False, ptp, 0.0
    near = float((np.minimum(np.abs(y - y.min()), np.abs(y - y.max()))
                  < 0.1 * ptp).mean())
    return near >= 0.9, ptp, near


def sync_phase(led, arm, dt: float, t0: float, *, led_threshold_v: float,
               bias=None, guard_s: float = 2e-5,
               min_swing_v: float = 0.5) -> dict:
    """Was the 81150A armed at the LED's falling edge or its rising one?

    `led` is the 33220A output in volts, `arm` the 81150A Sync, `bias` the
    81150A output if it was captured. `dt` and `t0` come from the record.

    Edges are found at each trace's **own midpoint**, not at
    `led_threshold_v`. The drive levels here are 0.400 and 1.020 V against a
    turn-on threshold of 1.0 V, so a threshold-crossing test would be deciding
    an edge on 20 mV of margin. The midpoint sits 300 mV from either level. The
    threshold is then used for the separate question of which of the two levels
    actually lights the diode.

    `guard_s` is how far either side of the arm edge the LED level is sampled.
    The LED half-period is 1 ms, so anything from a microsecond up is clear of
    the transition; 20 us keeps well away from edge ringing.
    """
    led = np.asarray(led, dtype=float)
    arm = np.asarray(arm, dtype=float)
    out: dict = {}

    lo, hi = float(led.min()), float(led.max())
    mid = (hi + lo) / 2.0
    out["led low / high (V)"] = f"{lo:.4f} / {hi:.4f}"
    # A drive that never crosses its own turn-on threshold is not a light source.
    on_is_high = hi >= led_threshold_v - 0.02
    out["LED lit on the"] = "high level" if on_is_high else "(never — see verdict)"

    led_rise, led_fall = _edges(led, mid)
    if led_rise.size >= 2:
        out["LED period (ms)"] = round(float(np.median(np.diff(led_rise)) * dt * 1e3), 4)
    if led_rise.size and led_fall.size and led_rise.size >= 2:
        period = float(np.median(np.diff(led_rise)))
        first = led_fall[led_fall > led_rise[0]]
        if first.size:
            out["LED duty (%)"] = round(float((first[0] - led_rise[0]) / period * 100.0), 1)

    if not on_is_high:
        out["verdict"] = "no illumination"
        out["explanation"] = (
            f"the drive never rises above the LED turn-on threshold "
            f"{led_threshold_v:g} V (peak {hi:.4f} V), so nothing about the phase "
            "can be concluded — the diode is dark for the whole cycle")
        return out

    # -- which channel marks the moment the 81150A fired? -----------------
    # The Sync is the natural marker, but it is an input that can simply not be
    # connected — and a channel with nothing on it must not be allowed to
    # produce a verdict. The 81150A's own output answers the same question and
    # is the better evidence anyway: it is the field itself, not a sync pulse
    # that stands in for it.
    bias_arr = None if bias is None else np.asarray(bias, dtype=float)
    arm_ok, arm_ptp, arm_near = _is_logic(arm, min_swing_v)
    out["arm channel swing (V)"] = round(arm_ptp, 4)
    if bias_arr is not None:
        bias_ok, bias_ptp, _ = _is_logic(bias_arr, min_swing_v)
        out["81150A output swing (V)"] = round(bias_ptp, 4)
    else:
        bias_ok = False

    if arm_ok:
        marker, out["marker"] = _edges(arm, (float(arm.max()) + float(arm.min())) / 2.0)[0], \
            "81150A Sync"
    elif bias_ok:
        r, f = _edges(bias_arr, (float(bias_arr.max()) + float(bias_arr.min())) / 2.0)
        marker = np.sort(np.concatenate([r, f]))
        out["marker"] = "81150A output — the Sync channel was flat"
        out["marker note"] = (
            f"the Sync channel swings {arm_ptp * 1e3:.1f} mV, which is noise, not "
            "a logic line: nothing is connected to it, or the Sync output is off. "
            "The 81150A's own output is used instead — it answers the same "
            "question and is the field itself rather than a stand-in for it")
    else:
        marker = np.empty(0, int)
        out["marker"] = "none"

    out["marker edges in record"] = int(marker.size)
    if marker.size == 0:
        out["verdict"] = "no arm edge"
        out["explanation"] = (
            f"neither the Sync channel ({arm_ptp * 1e3:.1f} mV) nor the 81150A "
            "output carries a two-level signal, so there is nothing to time "
            "against. The generator is not producing a waveform, or neither "
            "channel is connected to it. Enable the 81150A and rerun — with the "
            "relay parked on the SourceMeter side and that output off if the "
            "sample must stay unbiased")
        return out
    arm_rise = marker

    guard = max(1, int(round(guard_s / dt)))
    usable = arm_rise[(arm_rise > guard) & (arm_rise < led.size - guard - 1)]
    if usable.size == 0:
        out["verdict"] = "arm edge too close to the record ends"
        out["explanation"] = (
            "every arm edge sits within the guard band of a record end, so the "
            "LED level cannot be sampled on both sides of it. Widen the window")
        return out

    i = int(usable[0])
    out["marker edge at (ns of record)"] = round(i * dt * 1e9, 1)
    out["marker edge vs trigger (ns)"] = round((i * dt + t0) * 1e9, 1)

    before_high = bool(led[i - guard] > mid)
    after_high = bool(led[i + guard] > mid)
    out["LED before / after the marker"] = (
        f"{'on' if before_high else 'off'} / {'on' if after_high else 'off'}")

    all_led = np.concatenate([led_rise, led_fall]) if led_rise.size or led_fall.size \
        else np.empty(0, int)
    if all_led.size:
        out["marker to nearest LED edge (ns)"] = round(
            float(np.min(np.abs(all_led - i))) * dt * 1e9, 1)

    if bias_arr is not None and arm_ok and bias_ok:
        b = bias_arr
        b_rise, b_fall = _edges(b, (float(b.max()) + float(b.min())) / 2.0)
        b_all = np.concatenate([b_rise, b_fall])
        after = b_all[b_all > i]
        if after.size:
            out["arm to 81150A output edge (ns)"] = round(
                float(after[0] - i) * dt * 1e9, 1)
        out["81150A output low / high (V)"] = f"{b.min():.4f} / {b.max():.4f}"
    elif bias_arr is not None:
        out["81150A output low / high (V)"] = \
            f"{bias_arr.min():.4f} / {bias_arr.max():.4f}"

    if before_high and not after_high:
        out["verdict"] = "armed on light-off"
        out["explanation"] = (
            "the LED was on before the arm and off after it, so extraction "
            "starts after the light stops. This is BACE")
    elif after_high and not before_high:
        out["verdict"] = "armed on light-on"
        out["explanation"] = (
            "the LED was OFF before the arm and ON after it: the 81150A fires "
            "just as the light turns on, so extraction happens during carrier "
            "generation rather than after it. Every charge measured this way is "
            "of something else. The arm is half an LED period (1 ms) from where "
            "it should be — invert the 33220A output (:OUTP:POL INV), which "
            "flips the LED phase while leaving the Sync alone, or arm on the "
            "opposite slope")
    else:
        out["verdict"] = "arm not on an LED edge"
        out["explanation"] = (
            f"the LED was {'on' if before_high else 'off'} on both sides of the "
            "arm, so the 81150A is not armed by the LED cycle at all — check "
            "that the 33220A Sync actually reaches the 81150A external trigger "
            "input, and that :ARM:SOUR is EXT")
    return out


# -- putting the generators into the state the phase check needs ---------
@contextlib.contextmanager
def drive_generators(c, rm, rig_config, run_config, led_drive, confirm=None):
    """Pulse mode on the 33220A, both outputs on, and everything put back after.

    Needed because nothing else in the harness leaves the LED running. A J-V
    scan leaves the 33220A in DC (`FUNC:SHAP DC`); `--configure` sets pulse but
    leaves the output off; `--outputs` enables it for one query and turns it
    straight back off; `--measure` never touches it at all. So after any DC work
    the generator has to be put back by hand, and this is that hand.

    **The 81150A output is the dangerous half.** Its amplifier feeds the device
    whenever the relay is on the amplifier side, so this reads the relay line
    back before enabling anything and refuses if it cannot prove where the relay
    is. The safe position for a phase measurement is the *SourceMeter* side with
    the SourceMeter output off: the amplifier is then disconnected, the 81150A
    can run freely, and nothing reaches the sample.
    """
    from bace.drivers.agilent33220a import Agilent33220A
    from bace.drivers.routing import TO_AMPLIFIER, TO_SOURCEMETER

    from .checks import _wrap

    # -- where is the relay? ---------------------------------------------
    position = None
    try:
        from bace.drivers.shutter import Shutter
        relay = Shutter(rig_config.dio_dll_path or None,
                        module_id=rig_config.dio_module_id,
                        module_nr=rig_config.relay_module_nr)
        with relay.open() as r:
            position = r.read_line()
    except Exception as exc:
        c.data["relay line"] = f"unreadable ({type(exc).__name__}: {exc})"
    else:
        c.data["relay line"] = {
            TO_AMPLIFIER: "amplifier — the 81150A reaches the device",
            TO_SOURCEMETER: "SourceMeter — the amplifier is disconnected",
        }.get(position, f"module {rig_config.relay_module_nr} = {position}")

    # -- is the SourceMeter driving into it? -----------------------------
    smu_res = _wrap(c, rm, rig_config.sourcemeter_address, ":SYST:ERR?")
    if smu_res is not None:
        try:
            live = str(smu_res.query(":OUTP?")).strip()
            c.data["Keithley :OUTP?"] = live
            if live.startswith("1"):
                c.fail("the Keithley output is ON. Turn it off before driving "
                       "the generators — with the relay on its side that is a "
                       "source into the device")
                yield False
                return
        finally:
            try:
                smu_res.close()
            except Exception:
                pass

    led_res = _wrap(c, rm, rig_config.led_address, ":SYST:ERR?")
    bias_res = _wrap(c, rm, rig_config.bias_address, ":SYST:ERR?")

    # -- what would enabling the 81150A actually put on the device? -------
    # Asking "enable anyway?" without saying what levels it is holding is an
    # unanswerable question. The generator keeps whatever the last session left,
    # and this rig has been found free-running at +5.06 V at the device.
    already_on = False
    if bias_res is not None:
        try:
            already_on = str(bias_res.query(":OUTP1?")).strip().startswith("1")
            c.data["81150A :OUTP1? (found)"] = "ON" if already_on else "OFF"
            hi = float(str(bias_res.query(":VOLT1:HIGH?")).strip())
            lo = float(str(bias_res.query(":VOLT1:LOW?")).strip())
            amp = rig_config.pulse_amp
            c.data["81150A levels (generator)"] = f"{hi:+.4g} / {lo:+.4g} V"
            c.data["81150A levels (at the device)"] = \
                f"{hi * amp:+.4g} / {lo * amp:+.4g} V"
            levels = f"it is holding {hi * amp:+.4g} / {lo * amp:+.4g} V at the device"
        except Exception:
            levels = "and its levels could not be read"
    else:
        levels = ""

    safe = position == TO_SOURCEMETER or already_on
    if not safe:
        why = ("the relay is on the amplifier side, so enabling the 81150A puts "
               "its output on the device" if position == TO_AMPLIFIER else
               "the relay position could not be read back, so there is no proof "
               "the amplifier is disconnected")
        answer = confirm(f"{why} — {levels}. Enable it anyway? (yes/no)") \
            if confirm is not None else "no"
        c.data["81150A enable, asked"] = str(answer)
        if str(answer).strip().lower() in ("y", "yes", "j", "ja"):
            safe = True
            c.warn(f"{why}. Enabled on an explicit yes — {levels}")
        else:
            c.warn(why + " — the 33220A will be driven but the 81150A will not, "
                   "so CHAN3 may be flat. Move the relay to the SourceMeter side "
                   "(SourceMeter output off) and rerun, or answer yes")
    saved_led: dict[str, str] = {}
    saved_bias: str | None = None
    try:
        if led_res is not None:
            for q in ("FUNC:SHAP?", ":VOLT:HIGH?", ":VOLT:LOW?", ":VOLT:OFFS?",
                      ":FREQ?", "FUNC:PULS:DCYC?", ":OUTP?"):
                try:
                    saved_led[q] = str(led_res.query(q)).strip()
                except Exception:
                    pass
            c.data["33220A was"] = (
                f"{saved_led.get('FUNC:SHAP?', '?')}, output "
                f"{saved_led.get(':OUTP?', '?')}")
            g = Agilent33220A(led_res)
            # The 81150A is armed by this generator's Sync, so the frequency has
            # to be the run's, not a driver default.
            g.set_pulse(led_drive[0], led_drive[1],
                        frequency_hz=run_config.pulse_frequency_hz,
                        duty_percent=run_config.duty_percent)
            g.enable_output(True)
            c.data["33220A set to"] = (
                f"PULSE {led_drive[0]:g}/{led_drive[1]:g} V, "
                f"{run_config.pulse_frequency_hz:g} Hz, "
                f"{run_config.duty_percent:g} %, output ON")

        if bias_res is not None and already_on:
            c.data["81150A output"] = "already on, left alone"
        elif bias_res is not None and safe:
            saved_bias = "0"
            bias_res.write(":OUTP1 ON;")
            c.data["81150A output"] = "enabled for this record (levels untouched)"
        elif bias_res is not None:
            c.data["81150A output"] = "left as found"

        yield True
    finally:
        if bias_res is not None:
            try:
                if saved_bias is not None:
                    bias_res.write(f":OUTP1 {saved_bias};")
                bias_res.close()
            except Exception:
                pass
        if led_res is not None:
            try:
                shape = saved_led.get("FUNC:SHAP?", "").upper()
                if shape.startswith("DC"):
                    led_res.write("FUNC:SHAP DC;")
                    if ":VOLT:OFFS?" in saved_led:
                        led_res.write(f":VOLT:OFFS {saved_led[':VOLT:OFFS?']};")
                elif shape:
                    # Two writes, not one compound message. After
                    # `FUNC:SHAP PULSE;` the next mnemonic continues at the same
                    # subsystem level, so `FUNC:PULS:HOLD` is parsed as
                    # `FUNC:FUNC:PULS:HOLD` -- the rig answered
                    # -113,"Undefined header" and discarded the rest of the
                    # message. `agilent33220a.set_pulse` splits them for exactly
                    # this reason; the restore path had quietly recombined them.
                    led_res.write("FUNC:SHAP PULSE;")
                    led_res.write("FUNC:PULS:HOLD DCYC;")
                    for q, cmd in ((":FREQ?", ":FREQ {};"),
                                   (":VOLT:HIGH?", ":VOLT:HIGH {};"),
                                   (":VOLT:LOW?", ":VOLT:LOW {};"),
                                   ("FUNC:PULS:DCYC?", "FUNC:PULS:DCYC {};")):
                        if q in saved_led:
                            led_res.write(cmd.format(saved_led[q]))
                if ":OUTP?" in saved_led:
                    led_res.write(f":OUTP {saved_led[':OUTP?']};")
                led_res.close()
            except Exception:
                pass


# -- the stage -----------------------------------------------------------
def stage_sync(report, rm, rig_config, run_config, *,
               led_drive: tuple[float, float] = (1.020, 0.4),
               drive: bool = False, confirm=None,
               window_s: float = 2.5e-3, points: int = 500_000,
               timeout_s: float = 30.0) -> None:
    """One wide single-shot record of the whole LED cycle, plus the three
    instrument states the run depends on and never sets."""
    from bace.drivers.infiniium import Infiniium

    from .checks import Check, _wrap

    def state(c: Check) -> None:
        led_res = _wrap(c, rm, rig_config.led_address, ":SYST:ERR?")
        if led_res is not None:
            try:
                pol = str(led_res.query(":OUTP:POL?")).strip()
                c.data["33220A :OUTP:POL?"] = pol
                if not pol.upper().startswith("INV"):
                    c.warn(
                        "the 33220A output is NORM. Its Sync is not inverted with "
                        "the waveform, so with NORM a rising Sync edge means the "
                        "LED just turned ON — and the 81150A arms on the rising "
                        "edge. Confirm against the phase check below")
            finally:
                try:
                    led_res.close()
                except Exception:
                    pass

        bias_res = _wrap(c, rm, rig_config.bias_address, ":SYST:ERR?")
        if bias_res is None:
            return
        try:
            for q in (":ARM:SOUR1?", ":ARM:SENS1?", ":OUTP1:POL?",
                      # The delay and width in force. Without them the gap
                      # between the 81150A Sync and its output cannot be read:
                      # a fixed instrument latency and a programmed delay look
                      # identical in one record. `trigger_offset_s = 47e-9`
                      # claims the gap is fixed; :PULS:DEL1 is what decides
                      # whether a measurement of it means anything.
                      ":PULS:DEL1?", ":FUNC1:PULS:WIDT?", ":FREQ1?",
                      ":VOLT1:HIGH?", ":VOLT1:LOW?",
                      # The driver writes `:ARM:SLOP POS` with no channel index
                      # while every neighbouring ARM command carries one. Ask
                      # both spellings: if only the indexed form answers, the
                      # slope has never actually been set and the instrument has
                      # been running on its own default.
                      ":ARM:SLOP?", ":ARM:SLOP1?"):
                try:
                    c.data["81150A " + q] = str(bias_res.query(q)).strip()
                except Exception as exc:
                    c.data["81150A " + q] = f"no answer ({type(exc).__name__})"
        finally:
            try:
                bias_res.close()
            except Exception:
                pass

    report.run("trigger chain: instrument state", "sync", state)

    def phase(c: Check) -> None:
        res = _wrap(c, rm, rig_config.scope_address, ":SYST:ERR?")
        if res is None:
            c.skip("scope not reachable")
            return

        arm_source = rig_config.trigger_source          # CHAN3, the 81150A sync
        saved: dict[str, str] = {}
        to_save = list(_SCOPE_STATE) + list(_channel_state(LED_CHANNEL)) \
            + list(_channel_state(BIAS_CHANNEL))
        for query, _ in to_save:
            try:
                saved[query] = str(res.query(query)).strip()
            except Exception:
                pass
        c.data["scope settings saved"] = len(saved)

        try:
            s = Infiniium(res, sense_resistor_ohm=rig_config.sense_resistor_ohm,
                          probe_attenuation=rig_config.probe_attenuation)
            s.default_setup()

            # A window wide enough for one whole 2 ms LED period, with the
            # trigger 50 us in so the edge that starts it is visible.
            res.write(f":TIM:RANG {window_s:g};")
            res.write(f":TIM:POS {window_s / 2.0 - 5e-5:g};")
            # Deep, because the sample rate follows from memory over window and
            # `:ACQ:MODE RTIM` decimates by dropping samples, not by averaging
            # them. 100k over 2.5 ms is 40 ns a point, and the rig showed what
            # that costs: the 81150A Sync came back as 2-87 mV of nothing while
            # the same channel showed a clean pulse the moment the timebase was
            # made faster. A pulse narrower than one sample interval is simply
            # not there. 500k is 5 ns a point over the same window, which also
            # makes the arm-to-output delay measurable instead of a rounding.
            res.write(f":ACQ:POIN {int(points):d};")
            res.write(":ACQ:MODE RTIM;:ACQ:AVER OFF;")

            for ch in (LED_CHANNEL, BIAS_CHANNEL):
                res.write(f":CHAN{ch}:DISP ON;")
                # 1 Mohm. A 50 ohm input would load the generator output this is
                # teed off, which is the one way this read-only stage could
                # change the measurement it is checking.
                res.write(f":CHAN{ch}:INP DC;")
            res.write(f":CHAN{LED_CHANNEL}:RANG 1.5;OFFS 0.7;")
            res.write(f":CHAN{BIAS_CHANNEL}:RANG 6;OFFS 0;")

            # Trigger on the LED drive, not on the 81150A sync: the LED cycle is
            # the clock everything else is judged against.
            res.write(":TRIG:MODE EDGE;")
            res.write(f":TRIG:EDGE:SOUR CHAN{LED_CHANNEL};")
            res.write(":TRIG:EDGE:SLOP POS;")
            res.write(f":TRIG:LEV CHAN{LED_CHANNEL},0.71;")
            # TRIG, not AUTO: a missing trigger has to become a timeout. AUTO
            # would sweep anyway and hand back a free-running picture that looks
            # like a measurement.
            res.write(":TRIG:SWE TRIG;")

            # The generators have to be running for there to be a cycle to
            # look at, and nothing else in the harness leaves them that way.
            # Opt-in, because enabling the 81150A is the one part of this stage
            # that can reach the sample.
            gens = (drive_generators(c, rm, rig_config, run_config,
                                     led_drive, confirm)
                    if drive else contextlib.nullcontext(True))
            with gens as ok:
                if ok is False:
                    return
                s.single_acquisition(timeout_s)
                led = s.fetch_volts(f"CHAN{LED_CHANNEL}")
                arm = s.fetch_volts(arm_source)
                try:
                    bias = s.fetch_volts(f"CHAN{BIAS_CHANNEL}")
                except Exception:
                    bias = None

            c.data["points requested / delivered"] = f"{int(points)} / {led.n}"
            c.data["points / dt (ns)"] = f"{led.n} / {led.dt * 1e9:g}"
            c.data["record t0 (us)"] = round(led.t0 * 1e6, 3)
            c.data["channels"] = (f"LED=CHAN{LED_CHANNEL}, arm={arm_source}, "
                                  f"bias=CHAN{BIAS_CHANNEL}")

            result = sync_phase(led.y, arm.y, led.dt, led.t0,
                                led_threshold_v=rig_config.led_threshold_v,
                                bias=None if bias is None else bias.y)
            if "marker note" in result and led.dt > 2e-8:
                result["marker note"] += (
                    f". At {led.dt * 1e9:g} ns a point, anything narrower than "
                    "that is dropped outright — `:ACQ:MODE RTIM` decimates by "
                    "throwing samples away. Raise --sync-points before "
                    "concluding the channel is unconnected")
            verdict = result.pop("verdict", "?")
            explanation = result.pop("explanation", "")
            c.data.update(result)
            c.data["verdict"] = verdict

            step = max(1, led.n // 600)
            for name, tr in (("led", led), ("arm", arm), ("bias", bias)):
                if tr is not None:
                    c.data[f"{name}[::{step}] (V)"] = [
                        float(f"{v:.5e}") for v in tr.y[::step]]

            if verdict == "armed on light-off":
                c.data["reading"] = explanation
            else:
                c.fail(explanation) if verdict == "armed on light-on" \
                    else c.warn(explanation)
        finally:
            for query, template in to_save:
                if query in saved:
                    try:
                        res.write(template.format(saved[query]))
                    except Exception:
                        pass
            try:
                res.close()
            except Exception:
                pass

    report.run("trigger chain: where the pulse sits in the LED cycle", "sync", phase)
