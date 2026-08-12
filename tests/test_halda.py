"""Unit tests for the Halda (HiGHS) allocation-strategy parser."""
from __future__ import annotations

from prima_pool_client.halda import (
    build_distribution,
    parse_distribution,
    parse_window_size,
)

ALLOC_TABLE = """
----- Allocation Strategy (by HiGHS) -----

Parameters:
  - k = 3
  - W = 24

Device A:
  - Device Index   : 0
  - Assignment Set : M4
  - N Layer Window : 35
  - N GPU Layers   : 0

Device B:
  - Device Index   : 1
  - Assignment Set : M4
  - N Layer Window : 24
  - N GPU Layers   : 0

Device C:
  - Device Index   : 2
  - Assignment Set : M4
  - N Layer Window : 13
  - N GPU Layers   : 0
"""


def test_parse_distribution_rank_order():
    dist = parse_distribution(ALLOC_TABLE)
    assert dist == {"0": 35, "1": 24, "2": 13}


def test_parse_distribution_no_table_returns_none():
    assert parse_distribution("some random output\nno allocation here") is None
    assert parse_distribution("") is None


def test_parse_distribution_removed_device_zeroed():
    """A weak device removed after the table prints must be reported as 0."""
    stdout = ALLOC_TABLE + "\nRemove device Device C (rank 2) with only 1 layer assigned.\n"
    dist = parse_distribution(stdout)
    assert dist == {"0": 35, "1": 24, "2": 0}


def test_parse_distribution_uses_last_table():
    """If multiple tables print (re-solve after removal), the last wins."""
    stdout = ALLOC_TABLE + "\nReassign layers to the remaining 2 device(s).\n\n" + ALLOC_TABLE.replace("Device C", "Device C2")
    dist = parse_distribution(stdout)
    assert dist == {"0": 35, "1": 24, "2": 13}


def test_parse_window_size():
    assert parse_window_size("Using window size: 72, GPU layers: 0") == 72
    assert parse_window_size("no window line") is None


def test_build_distribution_world1_head_does_all():
    """world == 1: no Halda table; head handles all layers."""
    stdout = "Using window size: 72, GPU layers: 0\n"
    assert build_distribution(stdout, world=1) == {"0": 72}


def test_build_distribution_world_multi():
    assert build_distribution(ALLOC_TABLE, world=3) == {"0": 35, "1": 24, "2": 13}


def test_build_distribution_world_multi_parse_failure_is_unknown():
    """world > 1 with a FAILED Halda parse must NOT fall back to 'head does
    all the work' — that would misattribute all layers to the head."""
    stdout = "Using window size: 72, GPU layers: 0\n"  # window line present, no table
    assert build_distribution(stdout, world=3) is None
    assert build_distribution("garbage output", world=3) is None


def test_build_distribution_unknown_returns_none():
    assert build_distribution("garbage output", world=1) is None
    assert build_distribution("garbage output", world=3) is None
