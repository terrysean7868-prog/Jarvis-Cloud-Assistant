"""Shared broker layer.

This package enables multi-instance deployments by providing a cross-process
transport (Redis pub/sub) for:
- notifications events
- device job routing (server -> agent)

If no broker is configured, the system falls back to in-process behavior.
"""
