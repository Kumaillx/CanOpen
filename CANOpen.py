"""
CANopen Automation Engine
=========================
Automatically commissions CANopen devices detected on the CAN bus through
a three-stage state machine:

  Stage 0 — Scan & Flash   : Detect bootloader nodes → assign Node IDs → flash to PREOPERATIONAL
  Stage 1 — Activate        : Transition all PREOPERATIONAL nodes → OPERATIONAL
  Stage 2 — Verify          : Confirm all nodes are OPERATIONAL → READY FOR PUMPING

Requirements: Python >= 3.10 (match-case), requests
"""

from __future__ import annotations

import time
import functools
import requests

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# =============================================================================
# CONFIGURATION
# =============================================================================

BASE_URL         = "http://localhost:9000"
REQUEST_TIMEOUT  = 10          # seconds — hard timeout for every HTTP call
MAX_RETRIES      = 5           # maximum retry attempts per operation
RETRY_DELAY      = 2.0         # seconds between retries
SCAN_DELAY       = 3.0         # seconds between CAN bus scans when no devices found
FLASH_SETTLE     = 1.5         # seconds to wait after a successful flash
BASE_NODE_ID     = 34          # starting node ID for assignment
NODE_ID_STEP     = 2           # increment between consecutive node IDs

_HEADERS = {"accept": "application/json"}


# =============================================================================
# CUSTOM EXCEPTION HIERARCHY
# =============================================================================

class CANOpenError(Exception):
    """Base exception for all CANopen automation errors."""

class GatewayConnectionError(CANOpenError):
    """Raised when the GSSHell gateway cannot be reached."""

class GatewayResponseError(CANOpenError):
    """Raised when the gateway returns an unexpected HTTP status code."""

class FlashError(CANOpenError):
    """Raised when all flash attempts for a device have been exhausted."""

class StartError(CANOpenError):
    """Raised when an NMT START command fails for a node."""

class NetworkScanError(CANOpenError):
    """Raised when no devices are detected after the maximum retry limit."""

class VerificationError(CANOpenError):
    """Raised when the final state verification check fails."""


# =============================================================================
# TYPED DATA MODELS
# =============================================================================

@dataclass(frozen=True)
class BootNode:
    """Represents a device currently in bootloader mode."""
    serial_number: int | str
    device_type: str

    @classmethod
    def from_dict(cls, data: dict) -> "BootNode":
        return cls(
            serial_number=data.get("serialNumber", "N/A"),
            device_type=data.get("type", "Unknown device"),
        )


@dataclass(frozen=True)
class CANopenNode:
    """Represents a configured CANopen node on the network."""
    node_id: int
    serial_number: int | str
    state: str
    device_type: str

    @classmethod
    def from_dict(cls, data: dict) -> "CANopenNode":
        return cls(
            node_id=data.get("nodeID", -1),
            serial_number=data.get("serialNumber", "N/A"),
            state=data.get("state", "UNKNOWN").upper(),
            device_type=data.get("type", "Unknown device"),
        )

    @property
    def is_operational(self) -> bool:
        return self.state == "OPERATIONAL"

    @property
    def is_preoperational(self) -> bool:
        return self.state == "PREOPERATIONAL"


@dataclass
class NetworkSnapshot:
    """Represents a live snapshot of all devices on the CAN bus."""
    boot_nodes: list[BootNode] = field(default_factory=list)
    canopen_nodes: list[CANopenNode] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "NetworkSnapshot":
        return cls(
            boot_nodes=[BootNode.from_dict(n) for n in data.get("bootNodes", [])],
            canopen_nodes=[CANopenNode.from_dict(n) for n in data.get("canOpenNodes", [])],
        )

    @property
    def has_devices(self) -> bool:
        return bool(self.boot_nodes or self.canopen_nodes)

    @property
    def all_operational(self) -> bool:
        return bool(self.canopen_nodes) and all(n.is_operational for n in self.canopen_nodes)

    @property
    def preoperational_nodes(self) -> list[CANopenNode]:
        return [n for n in self.canopen_nodes if n.is_preoperational]

    @property
    def non_operational_nodes(self) -> list[CANopenNode]:
        return [n for n in self.canopen_nodes if not n.is_operational]

    @property
    def assigned_node_ids(self) -> set[int]:
        return {n.node_id for n in self.canopen_nodes if n.node_id != -1}


# =============================================================================
# COLORED STRUCTURED LOGGER
# =============================================================================

class _ANSI:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    CYAN    = "\033[96m"
    BLUE    = "\033[94m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    BRED    = "\033[1;91m"
    WHITE   = "\033[97m"
    DIM     = "\033[2m"


_LEVEL_STYLES: dict[str, str] = {
    "STATE"   : _ANSI.CYAN,
    "ACTION"  : _ANSI.BLUE,
    "SUCCESS" : _ANSI.GREEN,
    "WARNING" : _ANSI.YELLOW,
    "ERROR"   : _ANSI.RED,
    "CRITICAL": _ANSI.BRED,
    "FAILED"  : _ANSI.BRED,
    "SYSTEM"  : _ANSI.WHITE,
    "INFO"    : _ANSI.DIM,
}


def log(level: str, message: str) -> None:
    """Print a colour-coded, timestamped log entry to stdout."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    colour    = _LEVEL_STYLES.get(level.upper(), _ANSI.WHITE)
    label     = f"{colour}[{level.upper():8s}]{_ANSI.RESET}"
    ts        = f"{_ANSI.DIM}[{timestamp}]{_ANSI.RESET}"
    print(f"{ts} {label} {message}")


# =============================================================================
# UTILITIES
# =============================================================================

def format_serial(serial: int | str | None) -> str:
    """Return a serial number formatted as a hex string (e.g. 0x16782149)."""
    if serial is None:
        return "N/A"
    if isinstance(serial, int):
        return f"0x{serial:08X}"
    if isinstance(serial, str):
        if serial.upper().startswith("0X"):
            return serial
        try:
            return f"0x{int(serial):08X}"
        except ValueError:
            return serial
    return str(serial)


def next_node_id(base: int, step: int, taken: set[int]) -> int:
    """Return the lowest available node ID starting from *base*, stepping by *step*."""
    nid = base
    while nid in taken:
        nid += step
    return nid


def retry(max_attempts: int = MAX_RETRIES, delay: float = RETRY_DELAY, exceptions=(CANOpenError,)):
    """
    Decorator that retries a function on specified exceptions.

    Usage::

        @retry(max_attempts=3, delay=1.5, exceptions=(GatewayConnectionError,))
        def my_fn(): ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        log("WARNING",
                            f"{func.__name__} failed (attempt {attempt}/{max_attempts}): "
                            f"{exc}. Retrying in {delay}s...")
                        time.sleep(delay)
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator


def print_network_status(snapshot: NetworkSnapshot) -> None:
    """Print a formatted table of all devices and their current states."""
    sep = "=" * 58
    print(f"\n{sep}")
    print(f"{'  PRESENT DEVICES & STATUS':^58}")
    print(sep)

    if not snapshot.has_devices:
        log("SYSTEM", "No devices detected on the CAN bus.")
        print(sep + "\n")
        return

    if snapshot.boot_nodes:
        print(f"  {'BOOTLOADER DEVICES':}")
        print(f"  {'-'*54}")
        for node in snapshot.boot_nodes:
            serial = format_serial(node.serial_number)
            print(f"  [{_ANSI.YELLOW}BOOT{_ANSI.RESET}]  {serial}  |  {node.device_type}")

    if snapshot.canopen_nodes:
        if snapshot.boot_nodes:
            print()
        print(f"  {'CANOPEN NODES':}")
        print(f"  {'-'*54}")
        for node in snapshot.canopen_nodes:
            serial = format_serial(node.serial_number)
            colour = _ANSI.GREEN if node.is_operational else (
                     _ANSI.CYAN  if node.is_preoperational else _ANSI.YELLOW)
            state_str = f"{colour}{node.state}{_ANSI.RESET}"
            print(f"  [Node {node.node_id:>3}]  {serial}  |  {state_str:<30}  |  {node.device_type}")

    print(sep + "\n")


# =============================================================================
# HTTP / GATEWAY LAYER
# =============================================================================

# Single shared session for connection pooling
_session = requests.Session()
_session.headers.update(_HEADERS)


def get_snapshot() -> NetworkSnapshot:
    """
    Query /what and return a typed NetworkSnapshot.

    Raises:
        GatewayConnectionError: Cannot connect to the GSSHell backend.
        GatewayResponseError:   Gateway returned a non-200 status.
    """
    url = f"{BASE_URL}/what"
    try:
        response = _session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.ConnectionError as exc:
        raise GatewayConnectionError(
            f"Cannot reach GSSHell at {BASE_URL}. Is the backend running? ({exc})"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise GatewayConnectionError(
            f"Request to {url} timed out after {REQUEST_TIMEOUT}s."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise GatewayConnectionError(f"Unexpected request error: {exc}") from exc

    if response.status_code != 200:
        raise GatewayResponseError(
            f"GET /what returned HTTP {response.status_code}: {response.text[:200]}"
        )

    return NetworkSnapshot.from_dict(response.json())


def flash_device(boot_node: BootNode, node_id: int) -> None:
    """
    Flash a bootloader device by posting to /flash/<serial>/<node_id>.
    Tries the decimal serial first, then the hex representation.

    Raises:
        FlashError: All format attempts failed.
    """
    serial = boot_node.serial_number
    hex_serial = format_serial(serial)

    # Build ordered list of serial formats to attempt
    formats: list[tuple[str, str]] = []
    if isinstance(serial, int):
        formats.append((str(serial), "decimal"))
    elif isinstance(serial, str):
        if serial.upper().startswith("0X"):
            try:
                formats.append((str(int(serial, 16)), "decimal"))
            except ValueError:
                pass
        else:
            try:
                formats.append((str(int(serial)), "decimal"))
            except ValueError:
                pass
    formats.append((hex_serial, "hex"))

    last_status: Optional[int] = None
    for serial_val, fmt in formats:
        url = f"{BASE_URL}/flash/{serial_val}/{node_id}"
        try:
            log("ACTION",
                f"Flashing {hex_serial} ({boot_node.device_type}) "
                f"[{fmt}={serial_val}] → Node ID {node_id}")
            response = _session.post(url, timeout=REQUEST_TIMEOUT)
            if response.status_code in (200, 201):
                log("SUCCESS",
                    f"Node {node_id} identity stored "
                    f"[{fmt} serial {serial_val}].")
                return  # success
            last_status = response.status_code
            log("WARNING",
                f"Flash attempt [{fmt}] returned HTTP {response.status_code}.")
        except requests.exceptions.RequestException as exc:
            log("WARNING", f"Flash attempt [{fmt}] raised: {exc}")

    raise FlashError(
        f"All flash attempts exhausted for {hex_serial}. "
        f"Last HTTP status: {last_status}. Formats tried: {[f[1] for f in formats]}"
    )


def start_node(node: CANopenNode) -> None:
    """
    Send NMT START to transition a node from PREOPERATIONAL → OPERATIONAL.

    Raises:
        StartError: The gateway rejected or could not process the command.
    """
    url = f"{BASE_URL}/start/{node.node_id}"
    try:
        log("ACTION",
            f"Sending NMT START → Node {node.node_id} "
            f"(s/n: {format_serial(node.serial_number)}, {node.device_type})")
        response = _session.post(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            msg = response.json().get("message", "Node in OPERATIONAL")
            log("SUCCESS", f"Node {node.node_id} → {msg}")
            return
        raise StartError(
            f"NMT START for Node {node.node_id} returned HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )
    except requests.exceptions.RequestException as exc:
        raise StartError(
            f"Request error during NMT START for Node {node.node_id}: {exc}"
        ) from exc


# =============================================================================
# STATE MACHINE STAGES
# =============================================================================

def _stage_0_flash(flashed_ids: set[int]) -> int:
    """
    Stage 0: Detect the first bootloader node and flash it.

    Returns the newly assigned node ID on success.

    Raises:
        NetworkScanError:      No devices found after MAX_RETRIES.
        GatewayConnectionError / GatewayResponseError: Network errors.
        FlashError:            Flash failed for the target device.
    """
    log("STATE", "─── Stage 0 : Scan & Flash (bootmode → preoperational) ───")
    no_device_retries = 0

    while True:
        snapshot = get_snapshot()
        print_network_status(snapshot)

        # No devices detected at all
        if not snapshot.has_devices:
            no_device_retries += 1
            if no_device_retries >= MAX_RETRIES:
                raise NetworkScanError(
                    f"No devices detected after {MAX_RETRIES} scan attempts."
                )
            log("SYSTEM",
                f"No devices found. Retrying scan in {SCAN_DELAY}s "
                f"(attempt {no_device_retries}/{MAX_RETRIES})...")
            time.sleep(SCAN_DELAY)
            continue

        # All devices already have node IDs → advance
        if not snapshot.boot_nodes:
            log("SUCCESS",
                f"All {len(snapshot.canopen_nodes)} device(s) cleared from bootmode.")
            return -1  # sentinel: nothing to flash, move on

        # Flash only the first pending bootloader node (snapshot is refreshed each loop)
        boot_node = snapshot.boot_nodes[0]
        serial_str = format_serial(boot_node.serial_number)
        log("INFO",
            f"Bootloader device detected — Serial: {serial_str}, "
            f"Type: {boot_node.device_type}")

        # Determine a unique node ID
        taken = snapshot.assigned_node_ids | flashed_ids
        assigned_id = next_node_id(BASE_NODE_ID, NODE_ID_STEP, taken)

        flash_device(boot_node, assigned_id)
        flashed_ids.add(assigned_id)

        log("SUCCESS",
            f"Device {serial_str} → preoperational, Node ID: {assigned_id}.")
        time.sleep(FLASH_SETTLE)
        # Loop back to fetch fresh snapshot


def _stage_1_activate(snapshot: NetworkSnapshot) -> None:
    """
    Stage 1: Transition all PREOPERATIONAL nodes to OPERATIONAL.

    Raises:
        StartError: An NMT START command failed for one or more nodes.
    """
    log("STATE", "─── Stage 1 : Activate (preoperational → operational) ────")

    # Self-healing: unexpected bootmode nodes → caller should re-enter stage 0
    if snapshot.boot_nodes:
        raise CANOpenError(
            f"{len(snapshot.boot_nodes)} device(s) still in bootmode — "
            "reverting to Stage 0."
        )

    preop = snapshot.preoperational_nodes
    if not preop:
        log("SUCCESS", "All configured nodes are already OPERATIONAL.")
        return

    log("SYSTEM", f"Transitioning {len(preop)} preoperational node(s) → OPERATIONAL.")
    print_network_status(snapshot)
    time.sleep(RETRY_DELAY)

    failed: list[str] = []
    for node in preop:
        try:
            start_node(node)
        except StartError as exc:
            log("ERROR", str(exc))
            failed.append(str(node.node_id))

    if failed:
        raise StartError(
            f"NMT START failed for node(s): {', '.join(failed)}"
        )


def _stage_2_verify(snapshot: NetworkSnapshot) -> None:
    """
    Stage 2: Confirm no bootmode nodes remain and all CANopen nodes are OPERATIONAL.

    Raises:
        VerificationError: Network is not in a ready state after inspection.
    """
    log("STATE", "─── Stage 2 : Verify & Ready ─────────────────────────────")

    if snapshot.boot_nodes:
        raise VerificationError(
            f"{len(snapshot.boot_nodes)} device(s) still in bootmode."
        )

    not_ready = snapshot.non_operational_nodes
    if not_ready:
        states = ", ".join(f"Node {n.node_id}={n.state}" for n in not_ready)
        raise VerificationError(
            f"{len(not_ready)} node(s) are not OPERATIONAL: {states}"
        )


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main() -> None:
    start_time = time.monotonic()

    print(f"\n{_ANSI.BOLD}{'═' * 60}{_ANSI.RESET}")
    print(f"{_ANSI.BOLD}{'  CANOPEN AUTOMATION ENGINE  ':^60}{_ANSI.RESET}")
    print(f"{_ANSI.BOLD}{'═' * 60}{_ANSI.RESET}\n")

    max_retries  = MAX_RETRIES
    retry_count  = 0
    flashed_ids: set[int] = set()
    devices_flashed = 0
    case = 0

    try:
        while True:
            match case:
                case 0:
                    # ── Stage 0: Flash all bootloader nodes one-by-one ──────
                    try:
                        result = _stage_0_flash(flashed_ids)
                    except GatewayConnectionError as exc:
                        retry_count += 1
                        if retry_count >= max_retries:
                            raise
                        log("ERROR",
                            f"Gateway unreachable: {exc}. "
                            f"Retrying in {RETRY_DELAY}s "
                            f"(attempt {retry_count}/{max_retries})...")
                        time.sleep(RETRY_DELAY)
                        continue
                    except FlashError as exc:
                        retry_count += 1
                        if retry_count >= max_retries:
                            raise
                        log("ERROR",
                            f"Flash error: {exc}. "
                            f"Retrying in {RETRY_DELAY}s "
                            f"(attempt {retry_count}/{max_retries})...")
                        time.sleep(RETRY_DELAY)
                        continue

                    # _stage_0_flash returns -1 when bootNodes is empty
                    if result == -1:
                        devices_flashed = len(flashed_ids)
                        retry_count = 0
                        case = 1
                    # Otherwise it flashed one device; loop back to check again
                    continue

                case 1:
                    # ── Stage 1: Activate all preoperational nodes ──────────
                    try:
                        snapshot = get_snapshot()
                    except GatewayConnectionError as exc:
                        retry_count += 1
                        if retry_count >= max_retries:
                            raise
                        log("ERROR",
                            f"Gateway unreachable: {exc}. "
                            f"Retrying in {RETRY_DELAY}s "
                            f"(attempt {retry_count}/{max_retries})...")
                        time.sleep(RETRY_DELAY)
                        continue

                    try:
                        _stage_1_activate(snapshot)
                    except CANOpenError as exc:
                        # Includes self-healing: bootmode nodes found → back to stage 0
                        log("WARNING", str(exc))
                        retry_count += 1
                        if retry_count >= max_retries:
                            raise
                        case = 0
                        time.sleep(RETRY_DELAY)
                        continue

                    # Show updated status after activation and check for
                    # newly-appeared bootloader devices. Some hardware only
                    # joins the CAN bus after another node has been started
                    # (sequential boot behaviour). If new boot nodes are found,
                    # revert to Stage 0 to flash them before verifying.
                    log("SYSTEM", "Updated network status after activation:")
                    try:
                        post_snapshot = get_snapshot()
                        print_network_status(post_snapshot)

                        if post_snapshot.boot_nodes:
                            log("WARNING",
                                f"{len(post_snapshot.boot_nodes)} new bootloader "
                                "device(s) appeared after activation "
                                "(sequential hardware boot detected). "
                                "Reverting to Stage 0 to flash them...")
                            retry_count = 0
                            case = 0
                            continue

                    except CANOpenError:
                        pass  # non-critical display step

                    retry_count = 0
                    case = 2
                    continue

                case 2:
                    # ── Stage 2: Final verification ─────────────────────────
                    try:
                        snapshot = get_snapshot()
                    except GatewayConnectionError as exc:
                        retry_count += 1
                        if retry_count >= max_retries:
                            raise
                        log("ERROR",
                            f"Gateway unreachable: {exc}. "
                            f"Retrying in {RETRY_DELAY}s "
                            f"(attempt {retry_count}/{max_retries})...")
                        time.sleep(RETRY_DELAY)
                        continue

                    try:
                        _stage_2_verify(snapshot)
                    except VerificationError as exc:
                        retry_count += 1
                        if retry_count >= max_retries:
                            raise
                        log("WARNING",
                            f"Verification failed: {exc}. "
                            f"Retrying in {RETRY_DELAY}s "
                            f"(attempt {retry_count}/{max_retries})...")
                        time.sleep(RETRY_DELAY)
                        continue

                    # ── All checks passed ────────────────────────────────────
                    print(f"\n{_ANSI.GREEN}{_ANSI.BOLD}{'*' * 50}{_ANSI.RESET}")
                    print(f"{_ANSI.GREEN}{_ANSI.BOLD}{'  READY FOR PUMPING  ':^50}{_ANSI.RESET}")
                    print(f"{_ANSI.GREEN}{_ANSI.BOLD}{'*' * 50}{_ANSI.RESET}\n")
                    break

    except NetworkScanError as exc:
        log("CRITICAL", f"Network scan exhausted: {exc}")
    except GatewayConnectionError as exc:
        log("CRITICAL", f"Gateway connection failed: {exc}")
    except GatewayResponseError as exc:
        log("CRITICAL", f"Unexpected gateway response: {exc}")
    except FlashError as exc:
        log("CRITICAL", f"Device flash failed: {exc}")
    except StartError as exc:
        log("CRITICAL", f"NMT start failed: {exc}")
    except VerificationError as exc:
        log("CRITICAL", f"Final verification failed: {exc}")
    except KeyboardInterrupt:
        log("WARNING", "Interrupted by user (Ctrl+C). Shutting down...")
    except Exception as exc:  # noqa: BLE001
        log("CRITICAL", f"Unexpected error: {type(exc).__name__}: {exc}")
        raise
    finally:
        elapsed = time.monotonic() - start_time
        print(f"\n{'─' * 60}")
        print(f"  Devices flashed  : {devices_flashed}")
        print(f"  Elapsed time     : {elapsed:.1f}s")
        print(f"  Finished at      : {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'─' * 60}\n")


if __name__ == "__main__":
    main()