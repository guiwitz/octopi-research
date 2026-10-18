"""The control panel (right side of the main window) is a fixed-width widget; this wrapper lets the divider between the
image display (napari) and the controls be dragged in both directions: if the panel is made narrower/shorter than its
content, scroll bars appear instead of the divider being blocked."""
from qtpy.QtCore import QSize
from qtpy.QtWidgets import QScrollArea, QFrame


class ResizableControlPanel(QScrollArea):
    def __init__(self, content, min_width=220, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setWidgetResizable(True)
        self.setWidget(content)
        self.setMinimumWidth(min_width)

    def sizeHint(self):
        """Natural size = the content (QScrollArea would cap it at a few hundred pixels)."""
        w = self.widget()
        hint = w.sizeHint() if w is not None else QSize(300, 300)
        extra = self.verticalScrollBar().sizeHint().width() + 2 * self.frameWidth()
        return QSize(hint.width() + extra, min(hint.height(), 2000))
