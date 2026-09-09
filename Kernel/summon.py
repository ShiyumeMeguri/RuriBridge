# -*- coding: utf-8 -*-
"""Making an application that is not watching come and look.

Two of the three keep a plugin running and watch the session themselves, so
publishing is the whole of telling them. The third runs a command and exits, so
something has to knock -- and that is the only difference between them, stated
once in the roster (``peers.Peer.resident``) and acted on here.

Knocking is not a transport. The payload is already in the session before anybody
is summoned, so what crosses the process boundary is one argument list and
nothing else: no socket, no temporary file, no protocol to keep in step. If the
application is already open, its own launcher routes the command into it; if it
is not, it starts and does the same thing. Either way the visit is the same visit.

Where the application lives is asked of the operating system's own registration
rather than guessed by walking the usual folders -- an install outside the
default location is normal, and a walk that misses it reports "not installed"
about software that is right there.
"""

from __future__ import annotations

import os
import subprocess

from . import peers as peers_module
from .log import logger

LOG = logger("summon")

#: Where Windows records the applications that registered themselves.
APP_PATHS = r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"


class SummonError(RuntimeError):
    """A peer that cannot be reached, and why."""


def _registered_path(executable_name):
    """The install location Windows itself recorded, or an empty string."""
    script = (
        "$key = Join-Path '{0}' '{1}'; "
        "if (Test-Path $key) {{ (Get-ItemProperty $key).'(default)' }}"
    ).format(APP_PATHS, executable_name)
    try:
        finished = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        LOG.debug("could not ask Windows about %s: %s", executable_name, error)
        return ""
    found = (finished.stdout or "").strip().strip('"')
    return found if found and os.path.isfile(found) else ""


def locate(peer, hint=""):
    """The executable for that application.

    A hint wins when it is a real file: somebody who typed a path knows something
    the registry does not, and an install that never registered itself would
    otherwise be unreachable.
    """
    if hint:
        candidate = hint
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, peer.executable_name)
        if os.path.isfile(candidate):
            return candidate
    return _registered_path(peer.executable_name)


def summon(peer_name, command="link", hint=""):
    """Make that application visit the session once.

    Refuses for an application that is already watching: summoning one would be
    describing a limit it does not have, and starting a second copy of something
    that is already attached is the worst of the answers available.
    """
    peer = peers_module.by_name(peer_name)
    if peer.resident:
        raise SummonError(
            "{0} keeps a plugin running and watches the session itself; "
            "publishing is the whole of telling it".format(peer.name))
    executable = locate(peer, hint)
    if not executable:
        raise SummonError(
            "cannot find {0}: Windows has no registration for {1} and no path "
            "was given".format(peer.label, peer.executable_name))
    arguments = [executable] + peer.summons(command)
    LOG.info("summoning %s: %s", peer.name, " ".join(arguments))
    subprocess.Popen(arguments)
    return arguments
