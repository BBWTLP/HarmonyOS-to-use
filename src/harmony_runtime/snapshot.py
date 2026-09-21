"""Device snapshot capture: one transport-merged transaction, or the legacy sequence.

Two providers produce the *same* normalized snapshot shape:

``LegacySnapshotProvider``
    The original sequence of separate device reads (screen, foreground, tree,
    display, foreground, screen). It stays in the tree on purpose: it is the
    fallback when batching is unavailable, the benchmark baseline and the
    regression oracle every batched result is compared against.

``BatchedSnapshotProvider``
    The same six samples, in the same order, executed inside ONE device-side
    transaction, so the host<->device transport boundary is crossed once per
    observation instead of once per sample.

The batching only merges the *transport*. Every safety fact the legacy path
read is still read from the live device inside the transaction:

``screen_before -> foreground_before -> display -> tree -> [image -> tree_after
-> display_after] -> foreground_after -> screen_after``

Nothing here is cached across calls and nothing here dispatches a write.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import time
import uuid

from .contracts import RuntimeFault
from .foreground import parse_foreground
from .observation import canonical, parse_bounds

#: Feature flag. ``0`` forces the legacy sequence, ``1`` (default) enables the
#: batched transaction with an automatic legacy fallback.
PROVIDER_FLAG = "HARMONY_OBSERVE_BATCHED"
#: Diagnostics only: ask the device to report per-section wall times. Adds one
#: process per boundary, so it is never enabled in normal operation.
SECTION_TIMING_FLAG = "HARMONY_OBSERVE_SECTION_TIMING"


def batched_enabled():
    value = os.environ.get(PROVIDER_FLAG)
    if value is None:
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


# -- fixed read-only command vocabulary -------------------------------------
#
# The device-side transaction never accepts caller-supplied shell text: it is
# assembled from exactly these strings plus a random marker and derived temp
# paths. A value that is not in this set cannot reach the device.

SCREEN_POWER = "hidumper -s PowerManagerService -a '-a'"
SCREEN_LOCK = "hidumper -s ScreenlockService -a -all"
FOREGROUND_WINDOW = "hidumper -s WindowManagerService -a '-a'"
FOREGROUND_MISSIONS = "hidumper -s AbilityManagerService -a '-l'"
DISPLAY_INFO = 'hidumper -s DisplayManagerService -a "-a"'

ALLOWED_COMMANDS = frozenset((
    SCREEN_POWER, SCREEN_LOCK, FOREGROUND_WINDOW, FOREGROUND_MISSIONS, DISPLAY_INFO,
))

SCREEN_BEFORE = "screen_before"
FOREGROUND_BEFORE = "foreground_before"
DISPLAY = "display"
TREE = "tree"
IMAGE = "image"
TREE_AFTER = "tree_after"
DISPLAY_AFTER = "display_after"
FOREGROUND_AFTER = "foreground_after"
SCREEN_AFTER = "screen_after"

MARKER_PREFIX = "HRSNAP"
#: Commands whose stdout is payload, not diagnostics, and must never be wrapped.
RAW_COMMANDS = frozenset(("cat", "base64"))
MAX_RAW_BYTES = 24 * 1024 * 1024
MAX_TREE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
TMP_ROOT = "/data/local/tmp"

#: One probe = a named list of device commands whose concatenated output is one
#: section of the transaction.
_FAST_PROBES = (
    (SCREEN_BEFORE, (SCREEN_POWER, SCREEN_LOCK)),
    (FOREGROUND_BEFORE, (FOREGROUND_WINDOW, FOREGROUND_MISSIONS, FOREGROUND_WINDOW)),
    (DISPLAY, (DISPLAY_INFO,)),
)
_FAST_TAIL = (
    (FOREGROUND_AFTER, (FOREGROUND_WINDOW, FOREGROUND_MISSIONS, FOREGROUND_WINDOW)),
    (SCREEN_AFTER, (SCREEN_POWER, SCREEN_LOCK)),
)

CONSISTENT = "consistent"
SCREEN_CHANGED = "screen_changed_during_capture"
FOREGROUND_CHANGED = "foreground_changed_during_capture"
TREE_UNSTABLE = "tree_changed_during_capture"
IMAGE_UNVERIFIED = "image_not_bracketed_by_tree"


def _library():
    from .device import parse_screen_state
    from devhelmkit.harmony.driver import _parse_display_info
    return parse_screen_state, _parse_display_info


def parse_image(payload):
    """Decode a base64 PNG payload into a PIL image, or None."""
    text = "".join(payload.split())
    if not text or len(text) > MAX_IMAGE_BYTES * 2:
        return None
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(base64.b64decode(text, validate=True)))
        image.load()
        return image
    except Exception:
        return None


class TransactionError(RuntimeFault):
    """A batched transaction that could not be parsed must never be guessed at."""


# -- script construction -----------------------------------------------------

def temp_dir(pid):
    return "%s/hrt_%s_%s" % (TMP_ROOT, pid, uuid.uuid4().hex[:8])


def build_script(marker, directory, include_image, section_timing=False):
    """Assemble the device-side transaction.

    Every command comes from :data:`ALLOWED_COMMANDS`; only the random marker,
    the derived temp directory and the derived file names are interpolated.

    Commands whose output must stay byte-exact (``cat``, ``base64``) are never
    wrapped; every other command runs under the shell's own ``time`` keyword,
    which reports device-side wall time without starting another process, so
    per-section device timing stays available at no measurable cost.
    """
    for name, commands in (*_FAST_PROBES, *_FAST_TAIL):
        for command in commands:
            if command not in ALLOWED_COMMANDS:
                raise RuntimeFault("internal_error", "Refused to build an unlisted device command")
    # Flat, uniquely named artefacts in the shared temp directory. The
    # screenshot service runs as a different principal and cannot write into a
    # directory this process created, so every artefact is a plain file whose
    # name carries the marker; `rm -f` at the end bounds retention, and the
    # start-of-transaction `rm -f` is idempotent for an interrupted run.
    layout = f"{directory}_layout.json"
    layout_after = f"{directory}_layout_after.json"
    # `snapshot_display` refuses any suffix other than `.jpeg`.
    shot = f"{directory}_shot.jpeg"
    lines = [
        f"M={marker}",
        f"D={directory}",
        f"rm -f {layout} {layout_after} {shot}",
    ]
    index = 0

    def emit(name, commands):
        nonlocal index
        lines.append(f"echo {marker}:{index}:b")
        for position, command in enumerate(commands):
            if command.split(" ", 1)[0] in RAW_COMMANDS:
                lines.append("{ %s; } 2>&1" % command)
            else:
                lines.append("time { %s; } 2>&1" % command)
            lines.append("rc=$?")
            lines.append(f"echo; echo {marker}:{index}:c{position}:$rc")
        lines.append(f"echo {marker}:{index}:e")
        index += 1

    for name, commands in _FAST_PROBES:
        emit(name, commands)
    emit(TREE, (f"uitest dumpLayout -p {layout}", "date +%s%N", f"cat {layout}"))
    if include_image:
        emit(IMAGE, (f"snapshot_display -f {shot}", "date +%s%N", f"base64 {shot}"))
        emit(TREE_AFTER, (f"uitest dumpLayout -p {layout_after}", f"cat {layout_after}"))
        emit(DISPLAY_AFTER, (DISPLAY_INFO,))
    for name, commands in _FAST_TAIL:
        emit(name, commands)
    lines.append(f"rm -f {layout} {layout_after} {shot}")
    lines.append(f"echo {marker}:done")
    return "\n".join(lines)


# -- transaction parsing -----------------------------------------------------

_MARKER_RE = re.compile(
    r"(?m)^(?P<marker>[A-Za-z0-9_]+):(?P<index>\d+):(?P<kind>[a-z][a-z0-9]*)(?::(?P<value>\d+))?$")


def _ordered_sequence(sections):
    sequence = []
    for index in sorted(sections):
        sequence.append((index, "b"))
        sequence.extend((index, "c%d" % position) for position in range(sections[index]))
        sequence.append((index, "e"))
    return sequence


def parse_transaction(raw, marker, sections):
    """Split one transaction's output into per-section command outputs.

    ``sections`` maps a section index to the number of commands it emitted.
    Framing is strict and order-sensitive: every marker must appear exactly
    once, in the order the script emitted it. A truncated, duplicated,
    reordered or interleaved capture is rejected rather than parsed partially,
    which is what makes "the device really ran this sequence" checkable.
    """
    if not isinstance(raw, str):
        raise TransactionError("device_unavailable", "Device transaction returned no text")
    if len(raw) > MAX_RAW_BYTES:
        raise TransactionError("device_unavailable", "Device transaction output exceeded the local limit")
    done = re.search(r"(?m)^" + re.escape(marker) + r":done$", raw)
    if done is None or raw[done.end():].strip():
        raise TransactionError("device_unavailable", "Device transaction did not complete")
    markers = {}
    order = []
    for match in _MARKER_RE.finditer(raw, 0, done.start()):
        if match.group("marker") != marker:
            raise TransactionError("device_unavailable", "Device transaction carried a foreign marker")
        key = (int(match.group("index")), match.group("kind"))
        if key in markers:
            raise TransactionError("device_unavailable", "Duplicate device transaction marker")
        markers[key] = match
        order.append(key)
    if order != _ordered_sequence(sections):
        raise TransactionError("device_unavailable", "Device transaction was not a complete ordered sequence")
    results = {}
    for index in sorted(sections):
        begin, end = markers[(index, "b")], markers[(index, "e")]
        pieces, cursor = [], begin.end()
        for position in range(sections[index]):
            marker_match = markers[(index, "c%d" % position)]
            pieces.append(raw[cursor:marker_match.start()])
            cursor = marker_match.end()
            if marker_match.group("value") != "0":
                raise TransactionError("device_unavailable", "Device transaction command reported a failure")
        if raw[cursor:end.start()].strip():
            raise TransactionError("device_unavailable", "Device transaction section has unlabelled output")
        results[index] = [piece.strip("\r\n") for piece in pieces]
    return results


# -- normalized snapshot -----------------------------------------------------

def _tree_fingerprint(tree):
    return hashlib.sha256(canonical(tree).encode()).hexdigest()


def _section_map(include_image):
    """Return {name: index} and {index: command count} for one transaction."""
    names, counts, index = [], {}, 0
    for name, commands in _FAST_PROBES:
        names.append(name)
        counts[index] = len(commands)
        index += 1
    names.append(TREE)
    counts[index] = 3
    index += 1
    if include_image:
        names.append(IMAGE)
        counts[index] = 3
        index += 1
        names.append(TREE_AFTER)
        counts[index] = 2
        index += 1
        names.append(DISPLAY_AFTER)
        counts[index] = 1
        index += 1
    for name, commands in _FAST_TAIL:
        names.append(name)
        counts[index] = len(commands)
        index += 1
    return {name: position for position, name in enumerate(names)}, counts


def _parse_tree(pieces):
    text = pieces[-1]
    if len(text) > MAX_TREE_BYTES:
        raise TransactionError("device_unavailable", "Device layout exceeded the local limit")
    try:
        tree = json.loads(text)
    except ValueError:
        raise TransactionError("device_unavailable", "Device layout was not valid JSON") from None
    if not isinstance(tree, dict) or not tree:
        raise TransactionError("device_unavailable", "Device layout was empty")
    return tree


def _validate_display(display):
    if not isinstance(display, (list, tuple)) or len(display) < 3:
        raise TransactionError("device_unavailable", "Device display geometry was invalid")
    width, height, rotation = display[0], display[1], display[2]
    if any(type(value) is not int for value in (width, height, rotation)) or width <= 0 or height <= 0:
        raise TransactionError("device_unavailable", "Device display geometry was invalid")
    return (width, height, rotation)


_TIME_RE = re.compile(r"^\s*\d+m[\d.]+s real\s+\d+m[\d.]+s user\s+\d+m[\d.]+s system\s*$")


def strip_device_timing(text):
    """Remove the shell's own timing report; the parsers must not see it."""
    if not text or "real" not in text:
        return text
    kept = [line for line in text.splitlines() if not _TIME_RE.match(line)]
    return "\n".join(kept)


def device_timing_ms(text):
    """Sum the shell-reported device-side wall time of one section."""
    total = 0.0
    found = False
    for line in (text or "").splitlines():
        if not _TIME_RE.match(line):
            continue
        match = re.search(r"(\d+)m([\d.]+)s real", line)
        if match:
            total += int(match.group(1)) * 60000 + float(match.group(2)) * 1000
            found = True
    return round(total, 3) if found else None


def _sections_to_snapshot(results, names, *, provider, round_trips, started, finished,
                          device_ms, include_image, component_errors):
    parse_screen_state, parse_display_info = _library()
    values = {name: results[index] for name, index in names.items()}
    section_ms = {}
    for name, pieces in values.items():
        total = None
        for piece in pieces:
            measured = device_timing_ms(piece)
            if measured is not None:
                total = (total or 0.0) + measured
        section_ms[name] = total
    snapshot = {
        "provider": provider,
        "version": 1,
        "round_trips": round_trips,
        "capture_started_at": started,
        "capture_finished_at": finished,
        "span_ms": round((finished - started) * 1000, 3),
        "device_ms": device_ms,
        "section_ms": section_ms,
        "component_errors": list(component_errors),
    }
    snapshot["screen_before"] = parse_screen_state(*[strip_device_timing(p)
                                                    for p in values[SCREEN_BEFORE]])
    snapshot["foreground_before"] = parse_foreground(*[strip_device_timing(p)
                                                       for p in values[FOREGROUND_BEFORE]])
    snapshot["display"] = _validate_display(parse_display_info(strip_device_timing(values[DISPLAY][0])))
    snapshot["tree"] = _parse_tree(values[TREE])
    snapshot["tree_fingerprint"] = _tree_fingerprint(snapshot["tree"])
    snapshot["foreground_after"] = parse_foreground(*[strip_device_timing(p)
                                                      for p in values[FOREGROUND_AFTER]])
    snapshot["screen_after"] = parse_screen_state(*[strip_device_timing(p)
                                                    for p in values[SCREEN_AFTER]])
    snapshot["tree_after"] = None
    snapshot["tree_after_fingerprint"] = None
    snapshot["display_after"] = None
    snapshot["image"] = None
    snapshot["image_dimensions"] = None
    snapshot["image_tree_skew_ms"] = None
    if include_image:
        image_pieces = values[IMAGE]
        snapshot["image_tree_skew_ms"] = _device_skew_ms(values[TREE][1], image_pieces[1])
        snapshot["image"] = parse_image(image_pieces[2])
        snapshot["display_after"] = _validate_display(
            parse_display_info(strip_device_timing(values[DISPLAY_AFTER][0])))
        snapshot["tree_after"] = _parse_tree(values[TREE_AFTER])
        snapshot["tree_after_fingerprint"] = _tree_fingerprint(snapshot["tree_after"])
        if snapshot["image"] is None:
            snapshot["component_errors"].append("image_capture_failed")
        if snapshot["tree_after_fingerprint"] != snapshot["tree_fingerprint"]:
            snapshot["component_errors"].append(TREE_UNSTABLE)
        snapshot["image_dimensions"] = tuple(snapshot["image"].size) if snapshot["image"] is not None else None
    snapshot["consistent"], snapshot["consistency"] = assess(snapshot, include_image)
    return snapshot


def _device_skew_ms(before_text, after_text):
    def stamp(text):
        found = re.findall(r"\b\d{16,20}\b", strip_device_timing(text or ""))
        return int(found[-1]) if found else None
    before, after = stamp(before_text), stamp(after_text)
    if before is None or after is None:
        return None
    return round((after - before) / 1e6, 3)


def assess(snapshot, include_image):
    """The consistency verdict, with an explicit reason.

    Mirrors the legacy checks: the foreground identity, the screen state and -
    when an image is part of the capture - the tree/display bracket around that
    image must all still agree. A capture that fails any of them is never
    actionable.
    """
    before, after = snapshot["screen_before"], snapshot["screen_after"]
    if before.get("screen_on") is not True or before.get("screen_locked") is not False:
        return False, SCREEN_CHANGED
    if after.get("screen_on") is not True or after.get("screen_locked") is not False:
        return False, SCREEN_CHANGED
    if before.get("screen_on") != after.get("screen_on") or before.get("screen_locked") != after.get("screen_locked"):
        return False, SCREEN_CHANGED
    foreground_before, foreground_after = snapshot["foreground_before"], snapshot["foreground_after"]
    if not (foreground_before is None and foreground_after is None):
        if foreground_before != foreground_after:
            return False, FOREGROUND_CHANGED
        if not isinstance(foreground_before, dict) or foreground_before.get("status") == "unstable":
            return False, FOREGROUND_CHANGED
    if not include_image:
        return True, CONSISTENT
    if snapshot["image"] is None or snapshot["display_after"] is None or snapshot["tree_after"] is None:
        return False, IMAGE_UNVERIFIED
    if snapshot["display_after"] != snapshot["display"]:
        return False, IMAGE_UNVERIFIED
    if snapshot["tree_after_fingerprint"] != snapshot["tree_fingerprint"]:
        return False, TREE_UNSTABLE
    skew = snapshot["image_tree_skew_ms"]
    if skew is None or not 0 <= skew <= 1000:
        return False, IMAGE_UNVERIFIED
    if snapshot["image_dimensions"] != (snapshot["display"][0], snapshot["display"][1]):
        return False, IMAGE_UNVERIFIED
    return True, CONSISTENT


# -- providers ---------------------------------------------------------------

class BatchedSnapshotProvider:
    """One device-side transaction per snapshot (one host<->device round trip)."""

    name = "batched"
    version = 1

    def __init__(self, device):
        self.device = device

    def capture(self, include_image=False, timing=None):
        marker = MARKER_PREFIX + uuid.uuid4().hex[:12]
        directory = temp_dir(os.getpid())
        script = build_script(marker, directory, include_image)
        names, counts = _section_map(include_image)
        started = time.monotonic()
        if timing is None:
            result = self.device.batch_probe(script)
        else:
            result = timing.call("snapshot_transaction", self.device.batch_probe, script)
        finished = time.monotonic()
        if timing is None:
            results = parse_transaction(result.get("raw"), marker, counts)
        else:
            results = timing.call("snapshot_parse", parse_transaction,
                                  result.get("raw"), marker, counts)
        return _sections_to_snapshot(
            results, names, provider=self.name, round_trips=1,
            started=started, finished=finished,
            device_ms=result.get("device_ms"), include_image=include_image,
            component_errors=[])


class LegacySnapshotProvider:
    """The original six-read sequence: fallback, benchmark baseline, oracle.

    ``ready`` is the runtime's own readiness gate. When it is supplied the two
    screen reads go through it, exactly as the pre-batching code did, so a
    locked or dark screen is still refused *before* the hierarchy is read.
    """

    name = "legacy"
    version = 1

    def __init__(self, device, supports_foreground=True, ready=None):
        self.device = device
        self.supports_foreground = supports_foreground
        self.ready = ready

    @staticmethod
    def _read(timing, name, function, *args):
        return timing.call(name, function, *args) if timing is not None else function(*args)

    def _read_screen(self, timing, name):
        if self.ready is not None:
            return self._read(timing, name, self.ready)
        return self._read(timing, name, self.device.screen_state)

    def capture(self, include_image=False, timing=None):
        device = self.device
        started = time.monotonic()
        round_trips = 0
        screen_before = self._read_screen(timing, "screen_ready_before")
        round_trips += 1
        if self.supports_foreground:
            foreground_before = self._read(timing, "foreground_before", device.foreground)
            round_trips += 1
        else:
            foreground_before = None
        tree = self._read(timing, "tree", device.tree)
        round_trips += 1
        display = self._read(timing, "display", device.display)
        round_trips += 1
        tree_after = display_after = image = None
        image_dimensions = None
        skew = None
        if include_image:
            tree_captured_at = time.monotonic()
            image = self._read(timing, "screenshot", device.screenshot)
            round_trips += 1
            image_captured_at = time.monotonic()
            tree_after = self._read(timing, "tree_after", device.tree)
            round_trips += 1
            display_after = self._read(timing, "display_after", device.display)
            round_trips += 1
            skew = round((image_captured_at - tree_captured_at) * 1000, 3)
            try:
                image_dimensions = tuple(image.size)
            except AttributeError:
                image_dimensions = None
        foreground_after = (self._read(timing, "foreground_after", device.foreground)
                            if self.supports_foreground else None)
        if self.supports_foreground:
            round_trips += 1
        screen_after = self._read_screen(timing, "screen_ready_after")
        round_trips += 1
        finished = time.monotonic()
        snapshot = {
            "provider": self.name,
            "version": self.version,
            "round_trips": round_trips,
            "capture_started_at": started,
            "capture_finished_at": finished,
            "span_ms": round((finished - started) * 1000, 3),
            "device_ms": None,
            "section_ms": {},
            "component_errors": [],
            "screen_before": _valid_screen(screen_before),
            "foreground_before": foreground_before,
            "display": _validate_display(display),
            "tree": tree,
            "tree_fingerprint": _tree_fingerprint(tree),
            "foreground_after": foreground_after,
            "screen_after": _valid_screen(screen_after),
            "tree_after": tree_after,
            "tree_after_fingerprint": _tree_fingerprint(tree_after) if tree_after is not None else None,
            "display_after": _validate_display(display_after) if display_after is not None else None,
            "image": image,
            "image_dimensions": image_dimensions,
            "image_tree_skew_ms": skew,
        }
        if include_image and image is None:
            snapshot["component_errors"].append("image_capture_failed")
        snapshot["consistent"], snapshot["consistency"] = assess(snapshot, include_image)
        return snapshot


def _valid_screen(state):
    if not isinstance(state, dict):
        raise RuntimeFault("device_unavailable", "Device screen state was invalid")
    return state


class ProviderState:
    """Bounded, observable degradation from batched to legacy capture."""

    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self):
        self.consecutive_failures = 0
        self.degraded = False
        self.fallback_reason = None
        self.counts = {"batched": 0, "legacy": 0, "legacy_fallback": 0}

    def record_success(self, provider):
        self.counts[provider] = self.counts.get(provider, 0) + 1
        if provider == "batched":
            self.consecutive_failures = 0

    def record_failure(self, reason):
        self.consecutive_failures += 1
        self.fallback_reason = reason
        if self.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            self.degraded = True
        return self.degraded

    def as_dict(self):
        return {"counts": dict(self.counts), "degraded": self.degraded,
                "consecutive_failures": self.consecutive_failures,
                "last_fallback_reason": self.fallback_reason}


def capture_snapshot(device, *, include_image=False, supports_foreground=True, state=None,
                     timing=None, ready=None):
    """Capture one live snapshot, preferring the merged transport.

    The caller always receives the same normalized shape. A framing or parsing
    failure of the batched transaction is answered by one legacy capture, which
    is recorded (never hidden) and bounded: after
    :data:`ProviderState.MAX_CONSECUTIVE_FAILURES` consecutive failures the
    provider degrades to the legacy sequence for the remaining process.
    """
    state = state if state is not None else _MODULE_STATE
    legacy = LegacySnapshotProvider(device, supports_foreground, ready)
    reason = None
    if state.degraded:
        reason = "degraded"
    elif not batched_enabled():
        reason = "flag_off"
    elif not hasattr(device, "batch_probe"):
        reason = "device_without_batched_probe"
    elif not supports_foreground:
        reason = "no_foreground_support"
    if reason is None:
        try:
            snapshot = BatchedSnapshotProvider(device).capture(include_image, timing)
            state.record_success("batched")
            return snapshot
        except TransactionError as error:
            degraded = state.record_failure(str(error))
            snapshot = legacy.capture(include_image, timing)
            snapshot["provider"] = "legacy_fallback"
            snapshot["fallback_reason"] = str(error)
            snapshot["provider_degraded"] = degraded
            state.record_success("legacy_fallback")
            return snapshot
    snapshot = legacy.capture(include_image, timing)
    snapshot["fallback_reason"] = reason
    state.record_success("legacy")
    return snapshot


_MODULE_STATE = ProviderState()
