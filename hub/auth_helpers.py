"""Shared authentication helpers used across multiple Flask blueprints.

Consolidates the _bearer_agent() function that was previously duplicated in
fleet_web.py, remote_web.py, bios_web.py, backups_web.py, and files_web.py.
"""
from flask import request

import fleet


def bearer_parts():
    """Split the Authorization header into its (agent_id, token) halves, or (None, None).

    Split out of `bearer_agent` so the refusal path can name WHICH agent was claimed
    without parsing the header a second time -- fleet_web.agent_auth needs the claimed id
    to answer "revoked" or "unknown". Purely a parse: it asserts nothing about whether
    either half is real.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, None
    raw = header[len("Bearer "):].strip()
    agent_id, _, token = raw.partition(":")
    if not agent_id or not token:
        return None, None
    return agent_id, token


def bearer_agent(db_path):
    """Resolve (agent_id, machine) from the Authorization header, or (None, None).

    Token format is '<agent_id>:<token>' so a single header carries both the
    identity and the secret; only the secret's hash is ever stored server-side.
    """
    agent_id, token = bearer_parts()
    if agent_id is None:
        return None, None
    machine = fleet.authenticate_agent(db_path, agent_id, token)
    if machine is None:
        return None, None
    return agent_id, machine
