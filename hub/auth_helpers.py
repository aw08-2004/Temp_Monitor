"""Shared authentication helpers used across multiple Flask blueprints.

Consolidates the _bearer_agent() function that was previously duplicated in
fleet_web.py, remote_web.py, bios_web.py, backups_web.py, and files_web.py.
"""
from flask import request

import fleet


def bearer_agent(db_path):
    """Resolve (agent_id, machine) from the Authorization header, or (None, None).

    Token format is '<agent_id>:<token>' so a single header carries both the
    identity and the secret; only the secret's hash is ever stored server-side.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, None
    raw = header[len("Bearer "):].strip()
    agent_id, _, token = raw.partition(":")
    if not agent_id or not token:
        return None, None
    machine = fleet.authenticate_agent(db_path, agent_id, token)
    if machine is None:
        return None, None
    return agent_id, machine
