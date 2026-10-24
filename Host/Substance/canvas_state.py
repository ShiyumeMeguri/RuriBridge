# -*- coding: utf-8 -*-
"""The canvases of the open project: Texture Sets that exist for the paint of others (``Spp.retarget.canvas``).

A canvas is a Texture Set no modelling application paints into: it keeps the paint of a material on the surface that paint was
made on, and the material that replaced it reads it. So it is nothing the bridge shows, publishes, pushes, binds or counts as a
Texture Set a surface has left layers in, and every surface that goes into the project carries the canvases' own surfaces with it
(``paint_repair``), or Painter drops them as it drops every Texture Set the surface does not paint into.

Which Texture Sets are canvases is a fact of the project (``project_facts``): the names of the ones the bridge made.

A canvas's surface lies where the material's surface lay, so in the viewports both would show. Painter's Python interface has no
call that hides the surface of a Texture Set -- only the eye beside each in the Texture Set list does -- so the canvases are hidden by
pressing that eye on the list, until it shows them hidden: a reload of the mesh, and a project that was opened, show every Texture Set
again. Nothing but the view depends on it.
"""

from __future__ import annotations

import shiboken6
import substance_painter.textureset
from PySide6 import QtCore, QtGui, QtWidgets

from ...Kernel.log import logger

from . import project_facts

LOG = logger("painter.canvas")

#: The key of the fact.
FACT = "canvases"
#: The name of the frame that holds the Texture Set list, and where its eye lies in a row, measured in a row 24 pixels high.
LIST_FRAME = "TextureSetsView"
ROW_HEIGHT = 24.0
EYE_CENTRE = (10, 12)
EYE_CLICK = (12, 12)
#: The eye shows a pupil, a pixel brighter than this, when the Texture Set is shown.
PUPIL_BRIGHTNESS = 150
#: How many ticks hiding is tried for after something showed the canvases again.
CONCEAL_TICKS = 100

_pending = [0]


def names():
    """The canvases of the open project that are still Texture Sets of it, by name."""
    present = {one.name for one in substance_painter.textureset.all_texture_sets()}
    return sorted(name for name in (project_facts.read(FACT) or []) if name in present)


def remember(made):
    """State that the Texture Sets ``made`` are canvases, with the ones the project already has."""
    project_facts.write(FACT, sorted(set(project_facts.read(FACT) or []) | set(made)))
    conceal_soon()


def held():
    """Every Texture Set of the open project that is not a canvas: the ones a modelling application's surface paints into."""
    canvases = set(project_facts.read(FACT) or [])
    return [one for one in substance_painter.textureset.all_texture_sets() if one.name not in canvases]


def conceal_soon():
    """Something showed the canvases in the viewports: they are hidden again on the next ticks."""
    _pending[0] = CONCEAL_TICKS


def concealing():
    """Whether hiding the canvases is still to be tried."""
    return _pending[0] > 0


def settle_concealment():
    """One try at hiding the canvases, for a tick; the tries end when it worked, and when they run out."""
    _pending[0] -= 1
    try:
        done = conceal()
    except Exception as error:
        _pending[0] = 0
        LOG.warning("the canvases could not be hidden in the viewports: %s", error)
        return
    if done:
        _pending[0] = 0
    elif _pending[0] == 0:
        LOG.warning("the canvases could not be hidden in the viewports: the Texture Set list did not show them")


def _frame():
    for widget in QtWidgets.QApplication.allWidgets():
        if shiboken6.isValid(widget) and widget.objectName() == LIST_FRAME and isinstance(widget, QtWidgets.QFrame) and widget.isVisible():
            return widget
    return None


def _shown(view, row):
    """Whether the Texture Set in this row of the list is shown: its eye has a pupil."""
    rect = view.visualRect(view.model().index(row, 0))
    scale = rect.height() / ROW_HEIGHT
    size = max(1, int(round(3 * scale)))
    left, top = int(round(rect.x() + (EYE_CENTRE[0] - 1) * scale)), int(round(rect.y() + (EYE_CENTRE[1] - 1) * scale))
    image = view.viewport().grab(QtCore.QRect(left, top, size, size)).toImage()
    return max(QtGui.qGray(image.pixel(x, y)) for x in range(image.width()) for y in range(image.height())) > PUPIL_BRIGHTNESS


def _press_eye(view, row):
    viewport = view.viewport()
    rect = view.visualRect(view.model().index(row, 0))
    scale = rect.height() / ROW_HEIGHT
    position = QtCore.QPoint(int(round(rect.x() + EYE_CLICK[0] * scale)), int(round(rect.y() + EYE_CLICK[1] * scale)))
    for kind in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease):
        pressed = kind == QtCore.QEvent.MouseButtonPress
        event = QtGui.QMouseEvent(kind, QtCore.QPointF(position), QtCore.QPointF(viewport.mapToGlobal(position)), QtCore.Qt.LeftButton,
                                  QtCore.Qt.LeftButton if pressed else QtCore.Qt.NoButton, QtCore.Qt.NoModifier)
        QtWidgets.QApplication.sendEvent(viewport, event)
        QtCore.QCoreApplication.processEvents()


def conceal():
    """Hide the canvases of the open project in the viewports, by their eyes in the Texture Set list. Returns True when every canvas
    the list has is hidden, False when the list does not show them yet.

    The list is looked for again after each eye: Painter may build it anew when one is pressed, and a view found through a frame
    is only good while the frame it was found through is held."""
    wanted = set(names())
    if not wanted:
        return True
    for _round in range(len(wanted) + 1):
        frame = _frame()
        if frame is None:
            return False
        trees = frame.findChildren(QtWidgets.QTreeView)
        if not trees or trees[0].model() is None:
            return False
        view = trees[0]
        model = view.model()
        rows = {model.data(model.index(row, 0), QtCore.Qt.DisplayRole): row for row in range(model.rowCount())}
        if not wanted <= set(rows):
            return False
        shown = [name for name in sorted(wanted) if _shown(view, rows[name])]
        if not shown:
            return True
        _press_eye(view, rows[shown[0]])
    return False
