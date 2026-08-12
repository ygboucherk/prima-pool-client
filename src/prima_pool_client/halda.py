"""Halda (HiGHS) allocation-strategy parsing.

prima.cpp (rank 0 only) prints an "Allocation Strategy (by HiGHS)" table to
stdout once the model is loaded and layers are assigned. Each node is listed
in ring order (Device Index == rank) with its layer window and GPU layer count:

    ----- Allocation Strategy (by HiGHS) -----
    Parameters:
      - k = 3
      - W = 24
    ...
    <device name>:
      - Device Index   : 0
      - Assignment Set : M4
      - N Layer Window : 35
      - N GPU Layers   : 0

Caveats handled here:
  - Weak devices may be REMOVED after the table prints ("Remove device ... with
    only N layer assigned") — those ranks end up with 0 layers (forwarders).
  - world == 1: Halda never runs and no table is printed; the sole node
    handles every layer (window == total model layers).

The distribution is keyed by RANK (Device Index), which is the ring position.
The server maps rank -> worker_id using the cluster's member order, so the
client never needs to know other workers' IDs.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# "----- Allocation Strategy (by HiGHS) -----"
_ALLOC_HEADER = re.compile(r"Allocation Strategy \(by HiGHS\)")
# "  - Device Index   : 0"
_DEVICE_INDEX = re.compile(r"-\s*Device Index\s*:\s*(\d+)")
# "  - N Layer Window : 35"
_LAYER_WINDOW = re.compile(r"-\s*N Layer Window\s*:\s*(\d+)")
# "Remove device <name> (rank 3) with only 1 layer assigned."
_REMOVE_DEVICE = re.compile(r"Remove device .* \(rank (\d+)\) with only (\d+) layer")
# "Using window size: %d, GPU layers: %d" — printed on EVERY node (incl. world==1)
_WINDOW_SIZE = re.compile(r"Using window size:\s*(\d+),\s*GPU layers:\s*(\d+)")


def parse_distribution(stdout: str) -> dict[str, int] | None:
    """Extract per-rank layer windows from prima.cpp's head stdout.

    Returns a dict {rank: window} for EVERY rank in the ring (removed ranks
    become 0), or None if no allocation strategy table was found (Halda did
    not run, e.g. world == 1).

    The table is the PRE-pruning solution; ranks removed by the weak-device
    pruning are zeroed out to reflect the authoritative final mapping.
    """
    if not stdout:
        return None
    # Find the LAST allocation table (a cluster may print several as it
    # re-solves after removing devices; the final one is authoritative).
    matches = list(_ALLOC_HEADER.finditer(stdout))
    if not matches:
        return None
    table_start = matches[-1].start()
    table = stdout[table_start:]

    # Parse each device block: a "Device Index : N" line followed by its
    # "N Layer Window : M" line. We scope the window search to the block
    # (up to the next Device Index) so a malformed block can't steal the
    # next device's window.
    windows: dict[str, int] = {}
    idx_matches = list(_DEVICE_INDEX.finditer(table))
    for i, m in enumerate(idx_matches):
        idx = m.group(1)
        block_end = idx_matches[i + 1].start() if i + 1 < len(idx_matches) else len(table)
        block = table[m.end():block_end]
        win_m = _LAYER_WINDOW.search(block)
        if win_m:
            windows[idx] = int(win_m.group(1))
    if not windows:
        return None

    # Reconcile with the weak-device removal: ranks pruned after the table
    # (they logged "Remove device ... (rank N) with only X layer assigned")
    # must be reported as 0 (they became forwarders / exited).
    for m in _REMOVE_DEVICE.finditer(stdout):
        rank = m.group(1)
        if rank in windows:
            windows[rank] = 0
    return windows


def parse_window_size(stdout: str) -> int | None:
    """Extract the head's window size ("Using window size: N, GPU layers: M").

    For world == 1 the window equals the total model layers — the head handles
    100% of the work. Returns None if the line is missing.
    """
    if not stdout:
        return None
    m = _WINDOW_SIZE.search(stdout)
    return int(m.group(1)) if m else None


def build_distribution(stdout: str) -> dict[str, int] | None:
    """Return the rank-keyed layer distribution for reporting.

    - world > 1, Halda table present -> {rank: window} (pruned ranks -> 0).
    - world == 1 (no table) -> {"0": total_layers} (head does all the work),
      using the "Using window size" line for the total.
    - Nothing could be parsed -> None (caller sends an explicit "unknown",
      so the cluster can still go live with the field recorded as unknown).
    """
    dist = parse_distribution(stdout)
    if dist is not None:
        return dist
    total = parse_window_size(stdout)
    if total is not None and total > 0:
        return {"0": total}
    return None
