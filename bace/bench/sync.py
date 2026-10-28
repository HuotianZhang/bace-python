"""Check the bias step's phase within the LED cycle.

The 81150A must arm when the LED drive falls. BACE holds V_pre under light,
normally at V_oc, then steps to V_coll after the light goes out. Arming on the
rising edge instead extracts during generation, yet can still produce a
plausible transient and Q. Field arrival is delay + 47.1 ns after the 81150A
Sync, fitted over a delay scan (manual 2.2); at 88 ns delay it is about 135 ns.

Signal paths:

    33220A output -> LED through a non-inverting amplifier of constant gain
    33220A Sync   -> 81150A external arm (EDGE, POS, 1.0 V, 10 kΩ)
    81150A output -> ×4 bias amplifier -> device
    81150A Sync   -> scope CHAN3, the record trigger

The 33220A's inverted output polarity makes its rising Sync edge correspond
to the falling LED drive edge (manual 1.2). A transient run reads that polarity
but does not set it. The check measures the drive directly, so a polarity
setting alone cannot establish correct phase.

Connect the 33220A main output through a tee to CHAN1. A single acquisition
covers the 2 ms LED period and compares the drive levels on either side of
the arm marker. The drive edge and light-off at the device differ because of
the LED driver and fibre delay (manual 2.2).

    CHAN1  33220A main output, tee, 1 MΩ; LED drive, 0.400 / 1.020 V
    CHAN2  device current; unchanged
    CHAN3  81150A Sync; record trigger
    CHAN4  81150A main output, tee, 1 MΩ; optional bias-step marker

Use 1 MΩ inputs. A 50 Ω input loads the generator output and can change the
signal under test. Without `--sync-drive`, the stage changes only scope
settings and restores the settings it reads successfully. It sets no generator
levels, enables no outputs and moves no relay, so a device can stay connected.

`--sync-drive` also runs the generators and restores their saved state. The
81150A can put voltage on the device through the bias amplifier. The relay
must select the Keithley with its output off, or explicit confirmation must
authorize enabling the 81150A. `--yes` supplies that confirmation.
"""
from __future__ import annotations

import contextlib

import numpy as np

LED_CHANNEL = 1
BIAS_CHANNEL = 4

# Query and command template for restoring the read-back value.
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
    """Test whether a trace contains a two-level logic signal.

    An unterminated input can couple to an LED edge and give a plausible timing
    marker. Noise with an outlier can put the trace midpoint below the noise
    band, leaving a single apparent rising edge. Noise of 2.3 mV and a false
    marker 320 ns from the LED edge are measured examples on this bench.

    The swing must reach `min_swing_v`: a logic signal spans volts. Samples
    must also cluster near two levels. Noise spreads through the range; a
    pulse train spends most of the record near one of its two levels.
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
    """Determine whether the 81150A marker falls at light-on or light-off.

    `led` is the 33220A output in V, `arm` is the 81150A Sync in V, and `bias`
    is the optional 81150A output in V. `dt` and `t0` are the record's sample
    interval and origin, in s.

    Each trace's midpoint locates its edges. For drive levels 0.400 and
    1.020 V and a 1.0 V LED threshold, threshold crossings have only 20 mV
    margin. The midpoint has about 300 mV margin from either level.
    `led_threshold_v` instead determines whether a level lights the LED.

    `guard_s` separates LED level samples from the marker on either side.
    For a 1 ms half-period, a 20 µs guard avoids transition ringing.
    """
    led = np.asarray(led, dtype=float)
    arm = np.asarray(arm, dtype=float)
    out: dict = {}

    lo, hi = float(led.min()), float(led.max())
    mid = (hi + lo) / 2.0
    out["led low / high (V)"] = f"{lo:.4f} / {hi:.4f}"
    # The LED requires a drive level reaching its turn-on threshold.
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

    # -- bias-step marker ------------------------------------------------
    # An unconnected Sync input cannot establish phase. If it lacks a logic
    # signal, the 81150A output can supply the marker: it carries the bias
    # step directly rather than a separate Sync pulse.
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


# -- generator drive for the phase check ----------------------------------
@contextlib.contextmanager
def drive_generators(c, rm, rig_config, run_config, led_drive, confirm=None):
    """Drive the generators for the phase check and restore their saved state.

    A J–V scan leaves the 33220A in DC mode. `--configure` selects pulse mode
    with output off; `--outputs` enables it only briefly; `--measure` does
    not drive it. The phase check needs a running LED cycle.

    The 81150A drives the device through the bias amplifier when the relay
    selects that path. The safe phase-check position selects the Keithley
    with its output off, disconnecting the amplifier from the device. An
    enabled Keithley blocks this operation. If the relay position is unsafe
    or unreadable, enabling an inactive 81150A requires explicit confirmation;
    without it, only the 33220A runs. An enabled 81150A stays enabled. The
    read-back includes device voltage levels so the confirmation states the
    voltage at risk. `--yes` supplies confirmation through the caller.
    """
    from bace.drivers.agilent33220a import Agilent33220A
    from bace.drivers.routing import TO_AMPLIFIER, TO_SOURCEMETER

    from .checks import _wrap

    # -- relay read-back --------------------------------------------------
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

    # -- Keithley output interlock ----------------------------------------
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

    # -- device voltage before output enable ------------------------------
    # Confirmation requires the actual levels: the generator retains settings
    # between sessions. Free-running output of +5.06 V at the device is
    # measured on this bench.
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
            # The 33220A Sync arms the 81150A, so the generator must use the
            # run's frequency rather than a driver default.
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
                    # Use separate writes. Within a compound message,
                    # `FUNC:SHAP PULSE;FUNC:PULS:HOLD DCYC;` makes the second header
                    # relative to `FUNC`, giving `FUNC:FUNC:PULS:HOLD`. It
                    # returns -113, "Undefined header", and discards the remaining
                    # commands. `Agilent33220A.set_pulse` also separates the
                    # writes; restoration must preserve that command boundary.
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
    """Record a complete LED cycle and read back the trigger-chain state.

    `RunConfig.external_trigger` controls 81150A arming during a run. The run
    reads the 33220A polarity without setting it, so this stage checks whether
    that retained setting produces the required phase.
    """
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
                      # Read delay and width to separate programmed delay from
                      # latency in the Sync-to-output interval. One record cannot
                      # distinguish them without `:PULS:DEL1?`. The 47.1 ns
                      # `trigger_offset_s` locates field arrival at the device,
                      # rather than the generator output (manual 2.2).
                      ":PULS:DEL1?", ":FUNC1:PULS:WIDT?", ":FREQ1?",
                      ":VOLT1:HIGH?", ":VOLT1:LOW?",
                      # The driver uses unindexed `:ARM:SLOP POS` while other arming
                      # commands carry a channel index. Query both forms to test
                      # whether the unindexed command applies the slope or leaves
                      # the instrument at its default.
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

        arm_source = rig_config.trigger_source          # CHAN3, the 81150A Sync
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
                          current_sign=rig_config.current_sign,
                          probe_attenuation=rig_config.probe_attenuation)
            s.default_setup()

            # A 2.5 ms record covers the 2 ms LED period. The trigger sits
            # 50 µs into the record so its starting edge remains visible.
            res.write(f":TIM:RANG {window_s:g};")
            res.write(f":TIM:POS {window_s / 2.0 - 5e-5:g};")
            # Memory depth sets the sample interval over the record.
            # `:ACQ:MODE RTIM` drops samples rather than averaging them. At
            # 100000 samples over 2.5 ms, the interval is 25 ns; narrow Sync
            # pulses can disappear, including at a 40 ns sample interval.
            # Sync amplitudes of 2–87 mV at low sample rates, with a clean
            # pulse at faster rates, are measured on this bench. At 500000
            # samples the interval is 5 ns, resolving both
            # short pulses and the arm-to-output delay.
            res.write(f":ACQ:POIN {int(points):d};")
            res.write(":ACQ:MODE RTIM;:ACQ:AVER OFF;")

            for ch in (LED_CHANNEL, BIAS_CHANNEL):
                res.write(f":CHAN{ch}:DISP ON;")
                # Use 1 MΩ input impedance. A 50 Ω input loads the generator
                # output at the tee and can change the signal under test.
                res.write(f":CHAN{ch}:INP DC;")
            res.write(f":CHAN{LED_CHANNEL}:RANG 1.5;OFFS 0.7;")
            res.write(f":CHAN{BIAS_CHANNEL}:RANG 6;OFFS 0;")

            # Trigger on the LED drive because its cycle defines the phase
            # reference for the 81150A marker.
            res.write(":TRIG:MODE EDGE;")
            res.write(f":TRIG:EDGE:SOUR CHAN{LED_CHANNEL};")
            res.write(":TRIG:EDGE:SLOP POS;")
            res.write(f":TRIG:LEV CHAN{LED_CHANNEL},0.71;")
            # `TRIG` makes a missing trigger time out. `AUTO` can return a
            # free-running record that looks like a triggered measurement.
            res.write(":TRIG:SWE TRIG;")

            # A phase record requires running generators. Generator drive is
            # opt-in because enabling the 81150A can put voltage on the device.
            # The other stages do not leave both generators running.
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
