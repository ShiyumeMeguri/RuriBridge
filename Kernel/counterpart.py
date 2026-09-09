# -*- coding: utf-8 -*-
"""The script that gets copied into an application that has no plugin folder.

Cascadeur loads commands by module path out of its own installation, and runs one
and exits -- there is no resident plugin to junction a package into. So the
attach step writes this one file into its command folder, and the file's whole
job is to put the checkout on the path and hand control to the driver that lives
there. Nothing about the bridge is duplicated inside the application: this is a
doorway, and it is regenerated whenever the checkout moves.

The checkout's location is written in at attach time by whoever is doing the
attaching, because it is the one fact the foreign application cannot work out for
itself. That is the same arrangement as the directory junction the other resident
application gets -- a pointer created once, pointing at wherever the checkout
actually is.
"""

from __future__ import annotations

#: Every counterpart carries this, so a stale one is recognised and rewritten
#: rather than left to fail somewhere further in.
CONTRACT = 1

TEMPLATE = '''# -*- coding: utf-8 -*-
"""RuriBridge doorway -- generated. Do not edit; reinstall to move it.

Cascadeur runs this command and exits, so one run is one visit to the session:
take whatever is owed, answer whatever was asked, publish if asked to, detach.
"""

import os
import sys

CONTRACT = {contract}
CHECKOUT = r"{checkout}"
SESSION = r"{session}"


def command_name():
    return "RuriBridge.Link"


def run(scene):
    parent = os.path.dirname(CHECKOUT)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    package = os.path.basename(CHECKOUT)
    module = __import__(package + ".Host.Cascadeur", fromlist=["visit"])
    module.visit(scene, session=SESSION)
'''


def render(checkout, session="default"):
    """The counterpart's text for one checkout."""
    return TEMPLATE.format(contract=CONTRACT, checkout=checkout, session=session)


def is_current(text):
    """Whether an already-installed counterpart speaks this contract."""
    return "CONTRACT = {0}\n".format(CONTRACT) in (text or "")
