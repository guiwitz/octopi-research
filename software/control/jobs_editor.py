"""Code editor for the Job designer: syntax colours, line numbers, error marks, completion with documentation, hover help.

CodeEditor     QPlainTextEdit with
                 - Python syntax highlighting (scope functions in a separate colour),
                 - line numbers, current line, marks for problems (from control.jobs.lint) and for the line where a test failed,
                 - completion: scope.<functions>, pt.<attributes>, channel names inside capture("..."), group names, with a short
                   description of the highlighted entry; opens after typing "scope." or with Ctrl+Space,
                 - hover help (function summary, problem text, channel exposure),
                 - Tab / Shift+Tab indent, Ctrl+/ comment, auto indent after ":".
No hardware, no dependency on the rest of the software except control.jobs.
"""
import keyword
import builtins
import html
import re

from qtpy.QtCore import Qt, QRect, QSize, QTimer, Signal, QEvent, QPoint
from qtpy.QtGui import (QColor, QFont, QFontDatabase, QPainter, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextFormat,
                        QPalette, QTextDocument)
from qtpy.QtWidgets import (QPlainTextEdit, QWidget, QTextEdit, QFrame, QVBoxLayout, QListWidget, QListWidgetItem, QLabel, QToolTip,
                            QApplication)

from control.jobs import apidoc
from control.jobs.api import resolve_channel_name, JobError

INDENT = '    '
FOCUS_MEASURES = ['GLVA', 'LAPE', 'LAPV', 'BREN', 'TENG', 'VOLL4', 'FFTI']      # common names; the Autofocus tab lists the real ones


def _is_dark(widget):
    c = widget.palette().color(QPalette.Base)
    return c.lightness() < 128


# ------------------------------------------------------------------------------------------------
# syntax highlighting
# ------------------------------------------------------------------------------------------------
def _fmt(color, bold=False, italic=False):
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Bold)
    f.setFontItalic(italic)
    return f


SCHEMES = {
    'light': dict(keyword=('#7b2fb5', True, False), builtin=('#0b7285', False, False), string=('#2b8a3e', False, False),
                  doc=('#5c940d', False, True), comment=('#868e96', False, True), number=('#d9480f', False, False),
                  defname=('#1864ab', True, False), api=('#087f5b', True, False), setting=('#a61e4d', False, False)),
    'dark': dict(keyword=('#c792ea', True, False), builtin=('#7fdbca', False, False), string=('#a5d6a7', False, False),
                 doc=('#b5cea8', False, True), comment=('#7f8a96', False, True), number=('#f78c6c', False, False),
                 defname=('#82aaff', True, False), api=('#4fd1a5', True, False), setting=('#f28fad', False, False)),
}

TOKEN = re.compile(r'''
    (?P<tq>[rRfFbBuU]{0,2}(?:\"\"\"|\'\'\'))
  | (?P<str>[rRfFbBuU]{0,2}(?:"(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?))
  | (?P<comment>\#.*)
  | (?P<num>\b\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?\b)
  | (?P<word>[A-Za-z_]\w*)
''', re.VERBOSE)
KEYWORDS = set(keyword.kwlist) | {'self'}
BUILTINS = {n for n in dir(builtins) if not n.startswith('_')}


class PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, document, dark=False):
        super().__init__(document)
        self.set_scheme(dark, rehighlight=False)
        self.api_names = set(apidoc.api_names())
        self.scope_name = 'scope'

    def set_scheme(self, dark, rehighlight=True):
        sc = SCHEMES['dark' if dark else 'light']
        self.f = {k: _fmt(*v) for k, v in sc.items()}
        if rehighlight:
            self.rehighlight()

    def highlightBlock(self, text):
        state = self.previousBlockState()
        n = len(text)
        i = 0
        if state in (1, 2):
            delim = "'''" if state == 1 else '"""'
            end = text.find(delim)
            if end == -1:
                self.setFormat(0, n, self.f['doc'])
                self.setCurrentBlockState(state)
                return
            self.setFormat(0, end + 3, self.f['doc'])
            i = end + 3
        self.setCurrentBlockState(0)
        prev_word = None
        skip_to = i
        while i < n:
            m = TOKEN.search(text, i)
            if not m:
                break
            kind = m.lastgroup
            s, e = m.start(), m.end()
            if s < skip_to:
                i = m.end() if m.end() > skip_to else skip_to
                continue
            if kind == 'tq':
                delim = m.group('tq')[-3:]
                end = text.find(delim, e)
                if end == -1:
                    self.setFormat(s, n - s, self.f['doc'])
                    self.setCurrentBlockState(1 if delim == "'''" else 2)
                    return
                self.setFormat(s, end + 3 - s, self.f['doc'])
                i = end + 3
                prev_word = None
                continue
            if kind == 'str':
                self.setFormat(s, e - s, self.f['string'])
            elif kind == 'comment':
                self.setFormat(s, e - s, self.f['comment'])
            elif kind == 'num':
                self.setFormat(s, e - s, self.f['number'])
            else:
                w = m.group()
                if w in KEYWORDS:
                    self.setFormat(s, e - s, self.f['keyword'])
                elif prev_word in ('def', 'class'):
                    self.setFormat(s, e - s, self.f['defname'])
                elif w == self.scope_name and text[e:e + 1] == '.':
                    mm = re.match(r'\.(\w+)', text[e:])
                    if mm and mm.group(1) in self.api_names:
                        a0 = e + 1
                        self.setFormat(a0, len(mm.group(1)), self.f['api'])
                        skip_to = a0 + len(mm.group(1))
                elif w in BUILTINS:
                    self.setFormat(s, e - s, self.f['builtin'])
                elif w.isupper() and len(w) > 2 and text[:s].strip() == '':
                    self.setFormat(s, e - s, self.f['setting'])
                prev_word = w
                i = e
                continue
            prev_word = None
            i = e


# ------------------------------------------------------------------------------------------------
# completion popup
# ------------------------------------------------------------------------------------------------
class CompletionPopup(QFrame):
    """List of completions + the description of the highlighted one. Never takes the keyboard focus."""
    accepted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFrameShape(QFrame.Box)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.doc = QLabel()
        self.doc.setWordWrap(True)
        self.doc.setTextFormat(Qt.RichText)
        self.doc.setMargin(4)
        self.doc.setFixedWidth(420)
        lay.addWidget(self.list)
        lay.addWidget(self.doc)
        self.list.currentRowChanged.connect(self._row_changed)
        self.list.itemClicked.connect(lambda it: self.accepted.emit(it.data(Qt.UserRole)))
        self._docs = []

    def set_items(self, items):
        """items: list of (insert text, display text, doc html)"""
        self.list.clear()
        self._docs = []
        for ins, disp, doc in items:
            it = QListWidgetItem(disp)
            it.setData(Qt.UserRole, ins)
            self.list.addItem(it)
            self._docs.append(doc)
        if items:
            self.list.setCurrentRow(0)
        rows = min(10, len(items))
        self.list.setFixedHeight(rows * self.list.sizeHintForRow(0) + 6 if items else 10)
        self.list.setFixedWidth(420)
        self.adjustSize()

    def _row_changed(self, row):
        self.doc.setText(self._docs[row] if 0 <= row < len(self._docs) else '')
        self.doc.setVisible(bool(self.doc.text()))
        self.adjustSize()

    def move_selection(self, delta):
        n = self.list.count()
        if n:
            self.list.setCurrentRow((self.list.currentRow() + delta) % n)

    def current_text(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None


# ------------------------------------------------------------------------------------------------
class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, ev):
        self.editor.paint_line_numbers(ev)

    def mousePressEvent(self, ev):
        self.editor.gutter_clicked(ev.pos())


class CodeEditor(QPlainTextEdit):
    cursorLineChanged = Signal(int, int)       # line, column (1-based)
    helpRequested = Signal(str)                # a function name (F1)

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setPointSize(max(font.pointSize(), 10))
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * 4)
        self.dark = _is_dark(self)
        self.highlighter = PythonHighlighter(self.document(), self.dark)
        self.gutter = LineNumberArea(self)
        self.issues = {}                 # line -> [Issue]
        self.issue_list = []
        self.error_line = None
        self.flash = None
        self.find_matches = []
        self.channel_names = []
        self.channel_infos = {}
        self.group_names = []
        self.popup = CompletionPopup(self)
        self.popup.accepted.connect(self._accept_completion)
        self.popup.hide()
        self._popup_ctx = None
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._clear_flash)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._cursor_moved)
        self._update_gutter_width()
        self._refresh_selections()

    # --- gutter ------------------------------------------------------------------------------
    def line_number_width(self):
        digits = max(3, len(str(self.blockCount())))
        return 26 + self.fontMetrics().horizontalAdvance('9') * digits

    def _update_gutter_width(self, *_):
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def paint_line_numbers(self, ev):
        p = QPainter(self.gutter)
        bg = self.palette().color(QPalette.Window)
        p.fillRect(ev.rect(), bg)
        fg = QColor(self.palette().color(QPalette.WindowText))
        fg.setAlpha(130)
        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        cur = self.textCursor().blockNumber()
        while block.isValid() and top <= ev.rect().bottom():
            if block.isVisible() and bottom >= ev.rect().top():
                line = num + 1
                p.setPen(fg if num != cur else self.palette().color(QPalette.WindowText))
                p.drawText(0, top, self.gutter.width() - 6, self.fontMetrics().height(), Qt.AlignRight, str(line))
                sev = self._line_severity(line)
                if sev:
                    col = {'error': '#e03131', 'warning': '#f08c00', 'info': '#1c7ed6'}[sev]
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(col))
                    h = self.fontMetrics().height()
                    p.drawEllipse(5, top + h // 2 - 4, 8, 8)
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            num += 1

    def _line_severity(self, line):
        if self.error_line == line:
            return 'error'
        iss = self.issues.get(line)
        if not iss:
            return None
        for s in ('error', 'warning', 'info'):
            if any(i.severity == s for i in iss):
                return s
        return None

    def gutter_clicked(self, pos):
        cursor = self.cursorForPosition(QPoint(0, pos.y()))
        line = cursor.blockNumber() + 1
        iss = self.issues.get(line)
        if iss:
            QToolTip.showText(self.gutter.mapToGlobal(pos), '<br>'.join(html.escape(i.message) for i in iss), self.gutter)

    # --- marks -------------------------------------------------------------------------------
    def set_issues(self, issues):
        self.issue_list = list(issues)
        self.issues = {}
        for i in issues:
            self.issues.setdefault(i.line, []).append(i)
        self._refresh_selections()
        self.gutter.update()

    def set_error_line(self, line):
        self.error_line = line
        self._refresh_selections()
        self.gutter.update()

    def set_find_matches(self, cursors):
        self.find_matches = cursors
        self._refresh_selections()

    def _line_selection(self, line, color, alpha):
        sel = QTextEdit.ExtraSelection()
        c = QColor(color)
        c.setAlpha(alpha)
        sel.format.setBackground(c)
        sel.format.setProperty(QTextFormat.FullWidthSelection, True)
        block = self.document().findBlockByNumber(line - 1)
        if not block.isValid():
            return None
        sel.cursor = QTextCursor(block)
        sel.cursor.clearSelection()
        return sel

    def _refresh_selections(self):
        sels = []
        cur = QTextEdit.ExtraSelection()
        c = QColor(self.palette().color(QPalette.Highlight))
        c.setAlpha(28)
        cur.format.setBackground(c)
        cur.format.setProperty(QTextFormat.FullWidthSelection, True)
        cur.cursor = self.textCursor()
        cur.cursor.clearSelection()
        sels.append(cur)
        if self.error_line:
            s = self._line_selection(self.error_line, '#e03131', 45)
            if s:
                sels.append(s)
        if self.flash:
            s = self._line_selection(self.flash, '#f1c40f', 110)
            if s:
                sels.append(s)
        colors = {'error': '#e03131', 'warning': '#f08c00', 'info': '#1c7ed6'}
        for iss in self.issue_list:
            block = self.document().findBlockByNumber(iss.line - 1)
            if not block.isValid():
                continue
            sel = QTextEdit.ExtraSelection()
            sel.format.setUnderlineStyle(QTextCharFormat.WaveUnderline)
            sel.format.setUnderlineColor(QColor(colors[iss.severity]))
            cursor = QTextCursor(block)
            start = block.position() + min(iss.col, max(0, block.length() - 1))
            end = block.position() + min(max(iss.end_col, iss.col + 1), max(0, block.length() - 1))
            cursor.setPosition(start)
            cursor.setPosition(max(end, start), QTextCursor.KeepAnchor)
            sel.cursor = cursor
            sels.append(sel)
        for cursor in self.find_matches:
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(241, 196, 15, 120))
            sel.cursor = cursor
            sels.append(sel)
        self.setExtraSelections(sels)

    def goto_line(self, line, flash=True, column=0):
        block = self.document().findBlockByNumber(max(0, line - 1))
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + min(column, max(0, block.length() - 1)))
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()
        if flash:
            self.flash = line
            self._refresh_selections()
            self._flash_timer.start(1600)

    def _clear_flash(self):
        self.flash = None
        self._refresh_selections()

    def _cursor_moved(self):
        self._refresh_selections()
        c = self.textCursor()
        self.cursorLineChanged.emit(c.blockNumber() + 1, c.positionInBlock() + 1)
        self.gutter.update()

    # --- keys --------------------------------------------------------------------------------
    def keyPressEvent(self, ev):
        key, mod = ev.key(), ev.modifiers()
        if self.popup.isVisible():
            if key in (Qt.Key_Down, Qt.Key_Up):
                self.popup.move_selection(1 if key == Qt.Key_Down else -1)
                return
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                t = self.popup.current_text()
                if t is not None:
                    self._accept_completion(t)
                    return
            if key == Qt.Key_Escape:
                self.popup.hide()
                return
        if key == Qt.Key_Space and mod & Qt.ControlModifier:
            self.show_completion(manual=True)
            return
        if key == Qt.Key_Slash and mod & Qt.ControlModifier:
            self.toggle_comment()
            return
        if key == Qt.Key_F1:
            self.helpRequested.emit(self.word_at_cursor()[0] or '')
            return
        if key in (Qt.Key_Tab, Qt.Key_Backtab) and not (mod & Qt.ControlModifier):
            c = self.textCursor()
            if c.hasSelection() or key == Qt.Key_Backtab or (mod & Qt.ShiftModifier):
                self._indent_selection(dedent=(key == Qt.Key_Backtab or bool(mod & Qt.ShiftModifier)))
            else:
                n = len(INDENT) - (c.positionInBlock() % len(INDENT))
                c.insertText(' ' * n)
            return
        if key in (Qt.Key_Return, Qt.Key_Enter) and not (mod & Qt.ShiftModifier):
            c = self.textCursor()
            line = c.block().text()[:c.positionInBlock()]
            indent = re.match(r'\s*', line).group()
            extra = INDENT if line.rstrip().endswith(':') else ''
            c.insertText('\n' + indent + extra)
            self.ensureCursorVisible()
            return
        if key == Qt.Key_Backspace and not self.textCursor().hasSelection():
            c = self.textCursor()
            before = c.block().text()[:c.positionInBlock()]
            if before and before.strip() == '' and len(before) % len(INDENT) == 0 and not (mod & Qt.ControlModifier):
                for _ in range(len(INDENT)):
                    c.deletePreviousChar()
                return
        super().keyPressEvent(ev)
        text = ev.text()
        if self.popup.isVisible():
            self.show_completion(manual=False, keep=True)
        elif text == '.' or (text in ('"', "'")):
            QTimer.singleShot(0, lambda: self.show_completion(manual=False))

    def _indent_selection(self, dedent):
        c = self.textCursor()
        doc = self.document()
        start, end = c.selectionStart(), c.selectionEnd()
        b0 = doc.findBlock(start)
        b1 = doc.findBlock(end)
        if end > start and b1.position() == end:
            b1 = b1.previous()
        c.beginEditBlock()
        b = b0
        while b.isValid() and b.blockNumber() <= b1.blockNumber():
            cur = QTextCursor(b)
            if dedent:
                t = b.text()
                n = min(len(INDENT), len(t) - len(t.lstrip(' ')))
                cur.movePosition(QTextCursor.StartOfBlock)
                for _ in range(n):
                    cur.deleteChar()
            else:
                cur.movePosition(QTextCursor.StartOfBlock)
                cur.insertText(INDENT)
            b = b.next()
        c.endEditBlock()

    def toggle_comment(self):
        c = self.textCursor()
        doc = self.document()
        b0 = doc.findBlock(c.selectionStart())
        b1 = doc.findBlock(c.selectionEnd())
        if c.hasSelection() and b1.position() == c.selectionEnd():
            b1 = b1.previous()
        blocks = []
        b = b0
        while b.isValid() and b.blockNumber() <= b1.blockNumber():
            blocks.append(b)
            b = b.next()
        nonempty = [b for b in blocks if b.text().strip()]
        uncomment = bool(nonempty) and all(b.text().lstrip().startswith('#') for b in nonempty)
        c.beginEditBlock()
        for b in blocks:
            t = b.text()
            if not t.strip():
                continue
            cur = QTextCursor(b)
            pos = b.position() + (len(t) - len(t.lstrip()))
            cur.setPosition(pos)
            if uncomment:
                n = 2 if t.lstrip().startswith('# ') else 1
                cur.setPosition(pos + n, QTextCursor.KeepAnchor)
                cur.removeSelectedText()
            else:
                cur.insertText('# ')
        c.endEditBlock()

    def insert_snippet(self, text):
        """insert (multi-line) code at the cursor, indented like the current line"""
        c = self.textCursor()
        line = c.block().text()
        base = re.match(r'\s*', line).group()
        lines = text.rstrip('\n').split('\n')
        out = lines[0] + ''.join('\n' + (base + l if l.strip() else l) for l in lines[1:])
        if len(lines) > 1:
            out += '\n' + base
        c.insertText(out)
        self.setTextCursor(c)
        self.setFocus()

    # --- context ----------------------------------------------------------------------------
    def scope_name(self):
        m = re.search(r'def\s+run_job\s*\(\s*(\w+)', self.toPlainText())
        return m.group(1) if m else 'scope'

    def _loop_names(self):
        txt = self.toPlainText()
        sc = re.escape(self.scope_name())
        pts = set(re.findall(r'for\s+(\w+)\s+in\s+' + sc + r'\.(?:points|all_points)', txt))
        tps = set(re.findall(r'for\s+(\w+)\s+in\s+' + sc + r'\.timepoints', txt))
        return (pts or {'pt', 'point', 'p'}), (tps or {'t', 'tp'})

    def word_at_cursor(self, cursor=None):
        """(word, base) at the cursor: base is the name before a '.', e.g. ('capture', 'scope') in scope.capture"""
        c = QTextCursor(cursor or self.textCursor())
        line = c.block().text()
        col = c.positionInBlock()
        s = col
        while s > 0 and (line[s - 1].isalnum() or line[s - 1] == '_'):
            s -= 1
        e = col
        while e < len(line) and (line[e].isalnum() or line[e] == '_'):
            e += 1
        word = line[s:e]
        base = None
        if s > 0 and line[s - 1] == '.':
            m = re.search(r'(\w+)\.$', line[:s])
            base = m.group(1) if m else None
        return word, base

    def _api_items(self, prefix):
        items = []
        for e in apidoc.API:
            if e['name'].lower().startswith(prefix.lower()):
                args = e['signature'].split(e['name'], 1)[1] if e['kind'] == 'method' else ''
                items.append((e['name'], f'{e["name"]}{args}' + ('' if e['kind'] == 'method' else '   (value)'),
                              f'<b>{html.escape(e["signature"])}</b><br>{e["summary"]}'))
        return items

    def _attr_items(self, kind, prefix):
        obj = apidoc.OBJECTS[0 if kind == 'Point' else 1]
        return [(a, f'{a}   ({t})', html.escape(d)) for a, t, d in obj['attrs'] if a.lower().startswith(prefix.lower())]

    def completion_context(self, manual=False):
        """-> (replace_start_column, items) or None"""
        c = self.textCursor()
        line = c.block().text()
        col = c.positionInBlock()
        before = line[:col]
        # strings: channels, groups, focus measures
        pats = [
            (r'''(?:capture|autofocus)\(\s*(?:channel\s*=\s*)?(["'])([^"']*)$''', 'channel'),
            (r'''(?:zstack|movie)\(\s*(?:channels\s*=\s*)?(["'])([^"']*)$''', 'channel'),
            (r'''(?:capture_channels|zstack|movie)\(\s*(?:channels\s*=\s*)?[\[(][^\])]*?(["'])([^"']*)$''', 'channel'),
            (r'''\bchannel\s*=\s*(["'])([^"']*)$''', 'channel'),
            (r'''^\s*[A-Za-z_]*CHANNEL[A-Za-z_]*\s*=.*?(["'])([^"']*)$''', 'channel'),
            (r'''\.points\([^)]*?(["'])([^"']*)$''', 'group'),
            (r'''focus_measure\s*=\s*(["'])([^"']*)$''', 'focus'),
        ]
        for pat, kind in pats:
            m = re.search(pat, before, re.I if kind == 'channel' and 'CHANNEL' in pat else 0)
            if m:
                frag = m.group(2)
                names = {'channel': self.channel_names, 'group': self.group_names, 'focus': FOCUS_MEASURES}[kind]
                items = []
                for n in names:
                    if frag.lower() in n.lower():
                        doc = ''
                        if kind == 'channel' and n in self.channel_infos:
                            i = self.channel_infos[n]
                            doc = f'exposure {i.get("exposure_ms", "?")} ms, gain {i.get("gain", "?")}, intensity {i.get("intensity", "?")}'
                        elif kind == 'group':
                            doc = 'group of positions'
                        items.append((n, n, doc))
                return (col - len(frag), items) if items else None
        m = re.search(r'([A-Za-z_]\w*)\.(\w*)$', before)
        if m:
            base, prefix = m.group(1), m.group(2)
            pts, tps = self._loop_names()
            if base == self.scope_name():
                items = self._api_items(prefix)
            elif base in pts:
                items = self._attr_items('Point', prefix)
            elif base in tps:
                items = self._attr_items('TimePoint', prefix)
            else:
                return None
            return (col - len(prefix), items) if items else None
        if manual:
            m = re.search(r'(\w*)$', before)
            prefix = m.group(1)
            words = [self.scope_name()] + [k for k in ('for', 'if', 'elif', 'else', 'while', 'in', 'range', 'len', 'print', 'True', 'False', 'None', 'import', 'def', 'return', 'break', 'continue', 'and', 'or', 'not', 'pass')]
            words += sorted(set(re.findall(r'^([A-Z][A-Z0-9_]+)\s*=', self.toPlainText(), re.M)))
            words += sorted(self._loop_names()[0] & set(re.findall(r'\b\w+\b', self.toPlainText())))
            seen, items = set(), []
            for w in words:
                if w not in seen and w.lower().startswith(prefix.lower()) and (prefix or w == self.scope_name()):
                    seen.add(w)
                    items.append((w, w, ''))
            return (col - len(prefix), items) if items else None
        return None

    def show_completion(self, manual=False, keep=False):
        ctx = self.completion_context(manual)
        if not ctx or not ctx[1]:
            self.popup.hide()
            return
        start, items = ctx
        self._popup_ctx = start
        self.popup.set_items(items)
        cr = self.cursorRect()
        shift = (self.textCursor().positionInBlock() - start) * self.fontMetrics().horizontalAdvance(' ')
        pos = self.viewport().mapToGlobal(QPoint(max(0, cr.left() - shift), cr.bottom() + 2))
        self.popup.move(pos)
        self.popup.show()
        self.popup.raise_()

    def _accept_completion(self, text):
        ctx = self.completion_context(manual=True) if self._popup_ctx is None else None
        start = self._popup_ctx if self._popup_ctx is not None else (ctx[0] if ctx else self.textCursor().positionInBlock())
        c = self.textCursor()
        c.setPosition(c.block().position() + start, QTextCursor.MoveAnchor)
        c.setPosition(self.textCursor().position(), QTextCursor.KeepAnchor)
        c.insertText(text)
        self.setTextCursor(c)
        self.popup.hide()
        self._popup_ctx = None
        self.setFocus()

    def focusOutEvent(self, ev):
        if not self.popup.underMouse():
            self.popup.hide()
        super().focusOutEvent(ev)

    def mousePressEvent(self, ev):
        self.popup.hide()
        super().mousePressEvent(ev)

    # --- hover help -------------------------------------------------------------------------
    def event(self, ev):
        if ev.type() == QEvent.ToolTip:
            pos = ev.pos()
            cursor = self.cursorForPosition(pos)
            tip = self._tooltip_at(cursor)
            if tip:
                QToolTip.showText(ev.globalPos(), tip, self)
            else:
                QToolTip.hideText()
                ev.ignore()
            return True
        return super().event(ev)

    def _tooltip_at(self, cursor):
        line_no = cursor.blockNumber() + 1
        col = cursor.positionInBlock()
        for iss in self.issues.get(line_no, []):
            if iss.col <= col <= iss.end_col:
                color = {'error': '#e03131', 'warning': '#e67700', 'info': '#1c7ed6'}[iss.severity]
                return f'<span style="color:{color}"><b>{iss.severity}</b></span>: {html.escape(iss.message)}'
        if self.error_line == line_no:
            return 'The test stopped at this line.'
        line = cursor.block().text()
        # a string that names a channel
        for m in re.finditer(r'''(["'])([^"'\n]*)\1''', line):
            if m.start() < col < m.end() and self.channel_names:
                try:
                    full = resolve_channel_name(m.group(2), self.channel_names)
                except JobError:
                    return None
                i = self.channel_infos.get(full, {})
                return f'<b>channel</b> {html.escape(full)}<br>exposure {i.get("exposure_ms", "?")} ms, gain {i.get("gain", "?")}, intensity {i.get("intensity", "?")}'
        word, base = self.word_at_cursor(cursor)
        if not word:
            return None
        if base == self.scope_name():
            return apidoc.tooltip_for(word)
        pts, tps = self._loop_names()
        if base in pts or base in tps:
            obj = apidoc.OBJECTS[0 if base in pts else 1]
            for a, t, d in obj['attrs']:
                if a == word:
                    return f'<b>{base}.{a}</b> ({t})<br>{html.escape(d)}'
        if word == 'run_job':
            return '<b>run_job(scope)</b><br>The job starts here. Everything the microscope does goes through <code>scope</code>.'
        return None
