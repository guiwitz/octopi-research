"""Input lock while an acquisition runs.

While an acquisition (multipoint, flexible multipoint or a JOB) runs, every mouse click, mouse wheel and key press
on the main window and on the Positions window is ignored, except for:
  * the Stop / Abort / Pause buttons,
  * the image display (looking at images: zoom, pan, contrast; click-to-move is switched off during acquisitions anyway),
  * scroll bars and read-only text (e.g. the JOBs log),
  * dialogs and message boxes (e.g. the abort confirmation).
Keyboard shortcuts (e.g. Alt Gr + arrow for next/previous position) are blocked too.
While a JOB is paused the lock is lifted, so positions and the stage can be adjusted.

The lock is one event filter on the QApplication: nothing in the existing widgets had to change, and anything added
to the GUI later is locked automatically. Set SQUID_NO_INPUT_LOCK=1 to disable it (e.g. for debugging).
"""
import os
import time

from qtpy.QtCore import QObject, QEvent
from qtpy.QtWidgets import (QApplication, QWidget, QDialog, QScrollBar, QAbstractScrollArea, QPlainTextEdit,
                            QTextEdit, QMenu)

BLOCKED_EVENTS = {QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick, QEvent.Wheel,
                  QEvent.KeyPress, QEvent.KeyRelease, QEvent.ShortcutOverride, QEvent.Shortcut, QEvent.ContextMenu,
                  QEvent.DragEnter, QEvent.Drop, QEvent.TabletPress}

MESSAGE = 'Acquisition running: the controls are locked (Stop / Pause are available)'


class InputLock(QObject):

    def __init__(self, locked_windows, allowed_widgets, status=None, parent=None):
        """locked_windows: callable -> list of top-level windows whose input is locked (main window, Positions window)
        allowed_widgets: callable -> list of widgets that stay usable (with their children)
        status: callable(text) to show a short message when an input was ignored"""
        super().__init__(parent)
        self._locked_windows = locked_windows
        self._allowed = allowed_widgets
        self._status = status
        self.active = False
        self.paused = False
        self.blocked_count = 0
        self._last_msg = 0.0
        self.enabled = not os.environ.get('SQUID_NO_INPUT_LOCK')
        QApplication.instance().installEventFilter(self)

    # --- state
    def acquisition_started(self, *a):
        self.active = True
        self.paused = False

    def acquisition_finished(self, *a):
        self.active = False
        self.paused = False

    def set_paused(self, paused):
        self.paused = bool(paused)

    @property
    def locking(self):
        return self.enabled and self.active and not self.paused

    # --- filter
    def _is_allowed(self, w):
        if isinstance(w, (QScrollBar, QMenu)):
            return True
        win = w.window()
        if isinstance(win, QDialog) or (win not in self._locked_windows() and win is not None):
            return True                         # message boxes, confirmation dialogs, the job plan window, ...
        for a in self._allowed():
            if a is not None and (w is a or a.isAncestorOf(w)):
                return True
        p = w
        while p is not None:                    # read-only text (logs) can be scrolled and selected
            if isinstance(p, (QPlainTextEdit, QTextEdit)) and p.isReadOnly():
                return True
            if isinstance(p, QAbstractScrollArea) and w is p.verticalScrollBar():
                return True
            p = p.parentWidget()
        return False

    def eventFilter(self, obj, ev):
        if not self.locking:
            return False
        t = ev.type()
        if t not in BLOCKED_EVENTS:
            return False
        if t == QEvent.Shortcut:
            if isinstance(QApplication.activeWindow(), QDialog):
                return False                    # mnemonics in a dialog (e.g. &Yes of the abort question)
            self._blocked()
            return True                         # no shortcut (e.g. Alt Gr + arrow) while acquiring
        if not isinstance(obj, QWidget):
            return False
        if self._is_allowed(obj):
            return False
        if t in (QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.Wheel):
            self._blocked()
        return True

    def _blocked(self):
        self.blocked_count += 1
        now = time.time()
        if self._status is not None and now - self._last_msg > 1.5:
            self._last_msg = now
            try:
                self._status(MESSAGE)
            except Exception:
                pass
