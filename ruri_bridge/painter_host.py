# -*- coding: utf-8 -*-
"""Finding and starting Substance 3D Painter, without anybody typing a path.

Windows already records where Painter is: the installer writes the full
executable path into the ``App Paths`` registry key, which is the mechanism the
shell itself uses to resolve a bare program name. Reading that is a fact lookup,
not a guess -- unlike walking Program Files, which misses every install that is
not there, including this machine's.

Two witnesses are consulted, App Paths first and the uninstall entry second,
because a repaired or side-by-side install can leave one of them stale. If
neither answers, the path is asked for rather than invented.
"""

from __future__ import annotations

import os
import subprocess

from .log import logger

LOG = logger("painter")

EXECUTABLE_NAME = "Adobe Substance 3D Painter.exe"
PRODUCT_NAME = "Adobe Substance 3D Painter"

_APP_PATHS_KEY = (r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{0}"
                  .format(EXECUTABLE_NAME))
_UNINSTALL_ROOTS = (r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")

_DISCOVERY_SCRIPT = """
$path = (Get-ItemProperty -LiteralPath '{app_paths}' -ErrorAction SilentlyContinue).'(default)'
if (-not $path) {{
  foreach ($root in @('{uninstall}')) {{
    Get-ChildItem -Path $root -ErrorAction SilentlyContinue | ForEach-Object {{
      $entry = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
      if ($entry.DisplayName -eq '{product}' -and $entry.InstallLocation) {{
        $path = Join-Path $entry.InstallLocation '{executable}'
      }}
    }}
  }}
}}
if ($path) {{ $path.Trim('"') }}
"""


def _run_powershell(script):
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode != 0:
        LOG.debug("registry query failed: %s", completed.stderr.strip())
        return ""
    return completed.stdout.strip()


def discover_executable():
    """Painter's executable as Windows recorded it, or None."""
    script = _DISCOVERY_SCRIPT.format(
        app_paths=_APP_PATHS_KEY, uninstall="','".join(_UNINSTALL_ROOTS),
        product=PRODUCT_NAME, executable=EXECUTABLE_NAME)
    found = _run_powershell(script)
    if found and os.path.isfile(found):
        LOG.info("found Painter at %s", found)
        return found
    LOG.info("Windows has no record of a Painter install")
    return None


def is_running():
    """Whether a Painter process exists at all, plugin or no plugin."""
    output = _run_powershell(
        "@(Get-Process -Name '{0}' -ErrorAction SilentlyContinue).Count".format(
            os.path.splitext(EXECUTABLE_NAME)[0]))
    return output.isdigit() and int(output) > 0


def launch(executable, session=None):
    """Start Painter detached, telling it which session to attach to.

    The session name travels in the environment because the plugin reads it
    there; passing it on the command line would mean Painter parsing an argument
    it knows nothing about.
    """
    if not executable or not os.path.isfile(executable):
        raise RuntimeError(
            "no Painter executable at {0!r}; set it in the add-on preferences".format(
                executable))
    environment = dict(os.environ)
    if session:
        environment["RURI_BRIDGE_SESSION"] = session
    subprocess.Popen([executable], env=environment, close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    LOG.info("started %s", executable)
    return executable
