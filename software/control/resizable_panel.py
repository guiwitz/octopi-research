"""The control panel (right side of the main window) is a fixed-width widget; this wrapper lets the divider between the
image display (napari) and the controls be dragged in both directions: if the panel is made narrower/shorter than its
content, scroll bars appear instead of the divider being blocked."""
from qtpy.QtCore import QSize
from qtpy.QtWidgets import QScrollArea, QFrame, QComboBox


def make_combo_compact(combo, chars=16, popup_width=None):
    """A QComboBox is as wide as its longest item, which forces wide control panels. This lets it shrink to about `chars`
    characters (long items are shortened in the box); the drop-down list still shows the items in full (popup_width, in pixels)."""
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(chars)
    if popup_width:
        combo.view().setMinimumWidth(popup_width)


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
        width = hint.width()
        if w is not None and 0 < w.minimumWidth() == w.maximumWidth():
            width = w.minimumWidth()      # the content has a fixed width (set to what it needs): the panel needs exactly that
        extra = self.verticalScrollBar().sizeHint().width() + 2 * self.frameWidth()
        return QSize(width + extra, min(hint.height(), 2000))
