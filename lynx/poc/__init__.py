"""Cheap two-operator proof-of-concept launcher.

This is not the Phase 4 field kit. It starts today's ``lynx-relay`` + two
``lynx-headset`` clients with the laptop-only or bench-camera layout used to
film IFF, shared pings, and night/edge mode.
"""

from .launch import build_plan, main

__all__ = ["build_plan", "main"]
