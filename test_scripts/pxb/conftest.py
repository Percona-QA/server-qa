"""Pytest configuration file for docker_backup_tests."""
import pytest


def pytest_addoption(parser):
    """Add command-line options for pytest."""
    parser.addoption(
        "--repo-name",
        action="store",
        default=None,
        help="Repo name: pxb-24, pxb-80, pxb-8x-innovation, pxb-84-lts, pxb-9x-innovation"
    )
    parser.addoption(
        "--repo-type",
        action="store",
        default=None,
        help="Repo type: release, testing, experimental"
    )
    parser.addoption(
        "--server",
        action="store",
        default=None,
        help="Server: ps, ms"
    )
    parser.addoption(
        "--innovation",
        action="store",
        default="",
        help="Innovation version: 8.1, 8.2, 8.3, 8.4, 9.1"
    )