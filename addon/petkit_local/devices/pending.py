"""Durable, latest-value-only delivery of panel property settings."""
from __future__ import annotations

from copy import deepcopy

KEY = "pending_settings"


def remember(device, params):
    pending = device.config.setdefault(KEY, {})
    pending.update(deepcopy(params))
    retained = []
    for command in device.command_queue:
        if (isinstance(command, dict)
                and command.get("method") == "thing.service.property.set"):
            old = command.get("params", {})
            command = {**command, "params": {k: v for k, v in old.items() if k not in params}}
            if not command["params"]:
                continue
        retained.append(command)
    device.command_queue[:] = retained


def completed(device, sent):
    pending = device.config.get(KEY, {})
    for key, value in sent.items():
        if pending.get(key) == value:
            pending.pop(key, None)
    # Remove only matching property writes. Never replay or discard actions.
    retained = []
    for command in device.command_queue:
        if (isinstance(command, dict)
                and command.get("method") == "thing.service.property.set"):
            params = command.get("params", {})
            if params and all(k in sent and sent[k] == v for k, v in params.items()):
                continue
        retained.append(command)
    device.command_queue[:] = retained
