# ZAIA PDF - PDF annotator (PySide6 + PyMuPDF). Adds ink/highlight/text on top of pages.
import sys, os, re, json, time, shutil, hashlib, threading, urllib.request, urllib.parse
from pathlib import Path
import pymupdf as fitz
from PySide6.QtCore import (Qt, QPointF, QRectF, QSizeF, QMarginsF, QTimer, QBuffer, QIODevice,
                            Signal, QObject, QSettings)
from PySide6.QtGui import (QColor, QFont, QImage, QPainter, QPainterPath, QPalette,
                           QPen, QPixmap, QPageSize, QPageLayout, QPdfWriter, QIcon, QKeySequence,
                           QTransform, QTextCursor, QTextBlockFormat)
from PySide6.QtWidgets import (QApplication, QMainWindow, QGraphicsView, QGraphicsScene, QGraphicsItem,
                               QGraphicsRectItem, QGraphicsPixmapItem, QGraphicsPathItem,
                               QGraphicsTextItem, QPushButton, QColorDialog, QSpinBox,
                               QFontComboBox, QLabel, QFileDialog, QMessageBox, QInputDialog,
                               QLineEdit, QToolBar, QToolButton, QFrame, QVBoxLayout, QHBoxLayout,
                               QWidget, QStackedWidget, QButtonGroup, QComboBox)
from PySide6.QtPrintSupport import QPrinter, QPrintDialog, QAbstractPrintDialog

APP_VER = "0.9.0"
APP_NAME = "ZAIA PDF"
UPDATE_URL = "https://raw.githubusercontent.com/YOUR_USER/zaia-pdf/main/update.json"
APP_DIR = Path(__file__).resolve().parent
GAP = 16


# ---------------------------------------------------------------- update (no Qt, testable)
def vt(s):
    return tuple(int(x) for x in str(s).split("."))


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def fetch(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "ZAIA-PDF/" + APP_VER})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def local_rev():
    try:
        return int((APP_DIR / "setup_rev.txt").read_text().strip())
    except Exception:
        return 1  # shortcut: missing file = dev run, assume current


def safe_rel(rel):
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-/]+", rel)) and not rel.startswith("/") \
        and ".." not in rel.split("/")


def get_manifest(url):
    m = json.loads(fetch(url).decode("utf-8"))
    vt(m["version"])
    files = m["files"]
    for rel, h in files.items():
        if not safe_rel(rel) or not re.fullmatch(r"[0-9a-fA-F]{64}", str(h)):
            raise ValueError("update.json ไม่ถูกต้อง: " + rel)
    return m


def diff_files(files):
    return [f for f, h in files.items()
            if not (APP_DIR / f).exists() or sha(APP_DIR / f).lower() != h.lower()]


def _retry(fn, n=4):
    for i in range(n):
        try:
            return fn()
        except OSError:
            if i == n - 1:
                raise
            time.sleep(0.5)  # locked file (antivirus/explorer) usually frees up


def apply_update(base, files, changed):
    """download all -> verify SHA-256 -> only then write (with .bak, rollback on failure)"""
    tmp = APP_DIR / ".update_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    done = []
    try:
        for f in changed:
            data = fetch(base + urllib.parse.quote(f), 30)
            if hashlib.sha256(data).hexdigest().lower() != files[f].lower():
                raise RuntimeError("Hash ไม่ตรง ไม่เขียนไฟล์: " + f)
            t = tmp / f
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(data)
        try:
            for f in changed:
                dst = APP_DIR / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                bak = dst.with_name(dst.name + ".bak")
                had = dst.exists()
                if had:
                    _retry(lambda: shutil.copy2(dst, bak))
                done.append((dst, bak, had))
                _retry(lambda: os.replace(tmp / f, dst))
        except Exception as e:
            for dst, bak, had in reversed(done):
                try:
                    if had and bak.exists():
                        os.replace(bak, dst)
                    elif not had and dst.exists():
                        dst.unlink()
                except OSError:
                    pass
            raise RuntimeError("เขียนไฟล์ไม่สำเร็จ (ไฟล์อาจถูกล็อก) ย้อนกลับแล้ว: %s: %s" % (type(e).__name__, e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- canvas items
class PageItem(QGraphicsRectItem):
    def __init__(self, idx, w, h, x, y):
        super().__init__(0, 0, w, h)
        self.idx = idx
        self.setPos(x, y)
        self.setBrush(Qt.white)
        self.setPen(QPen(Qt.NoPen))
        self.pix = QGraphicsPixmapItem(self)
        self.pix.setTransformationMode(Qt.SmoothTransformation)
        self.scale_key = 0

    def render(self, doc, s):
        if self.scale_key == s:
            return
        pm = doc[self.idx].get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
        img = QImage(pm.samples, pm.width, pm.height, pm.stride, QImage.Format_RGB888).copy()
        self.pix.setPixmap(QPixmap.fromImage(img))
        self.pix.setScale(1 / s)
        self.scale_key = s

    def clear(self):
        if self.scale_key:
            self.pix.setPixmap(QPixmap())
            self.scale_key = 0


class Grip(QGraphicsRectItem):
    """green handle on the right edge of a selected text box: drag = set wrap width, double-click = auto"""
    def __init__(self, parent):
        super().__init__(-5, -12, 10, 24, parent)
        self.setBrush(QColor("#22C55E"))
        self.setPen(QPen(QColor("#0b0d0e"), 1))
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        self.setCursor(Qt.SizeHorCursor)
        self.setZValue(20)
        self.hide()

    def mousePressEvent(self, e):
        e.accept()

    def mouseMoveEvent(self, e):
        p = self.parentItem()
        p.set_width(max(20, p.mapFromScene(e.scenePos()).x()))

    def mouseReleaseEvent(self, e):
        self.parentItem().canvas.changed.emit()

    def mouseDoubleClickEvent(self, e):
        self.parentItem().set_width(-1)


class ZText(QGraphicsTextItem):
    def __init__(self, canvas, page):
        super().__init__()
        self.canvas = canvas
        self.pending = True
        self.sx = self.lh = 100
        self.setData(0, "ann")
        self.setData(1, page)
        self.setFlags(QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemIsMovable)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.grip = Grip(self)  # child item: hidden unless selected, never exported
        self.document().contentsChanged.connect(self.place_grip)

    def style(self, font, color, sx, lh):
        """font (weight/spacing/size...), colour, horizontal scale %, line height %"""
        self.setFont(font)
        self.setDefaultTextColor(color)
        self.sx, self.lh = sx, lh
        self.setTransform(QTransform.fromScale(sx / 100, 1))  # compress/expand around top-left
        c = QTextCursor(self.document())
        c.select(QTextCursor.SelectionType.Document)
        f = QTextBlockFormat()
        f.setLineHeight(lh, 1)  # 1 = ProportionalHeight
        c.mergeBlockFormat(f)
        self.place_grip()

    def set_width(self, w):
        self.setTextWidth(w)
        self.place_grip()

    def place_grip(self):
        r = self.boundingRect()
        w = self.textWidth() if self.textWidth() > 0 else r.width()
        self.grip.setPos(w, r.height() / 2)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self.grip.setVisible(bool(value))
        return super().itemChange(change, value)

    def edit(self):
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setFocus(Qt.MouseFocusReason)

    def mouseDoubleClickEvent(self, e):
        if self.textInteractionFlags() == Qt.NoTextInteraction:
            self.edit()
        super().mouseDoubleClickEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        c = self.textCursor()
        c.clearSelection()
        self.setTextCursor(c)
        self.canvas.text_done(self)


class Canvas(QGraphicsView):
    changed = Signal()
    pageInfo = Signal(int, int)

    def __init__(self):
        sc = QGraphicsScene()
        super().__init__(sc)
        self.sc = sc  # keep python ref or the scene is garbage collected
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor("#2b2d30"))  # view bg, so sc.render() stays transparent
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.doc = None
        self.data = b""
        self.password = ""
        self.pages = []
        self.zoom = 1.0
        self.tool = "select"
        self.pen_color = QColor("#22C55E")
        self.hl_color = QColor("#FFEB3B")
        self.width = 3
        self.font = QFont("Tahoma", 16)
        self.tx_stretch = self.tx_line = 100
        self.undo, self.redo = [], []
        self.cur = self.start = self.pg = self.erasing = None
        self.timer = QTimer(self, singleShot=True, interval=40)
        self.timer.timeout.connect(self.refresh)
        self.verticalScrollBar().valueChanged.connect(lambda *_: self.timer.start())
        self.set_tool("select")

    # ---- document
    def set_doc(self, doc, data, password=""):
        self.sc.clear()
        self.doc, self.data, self.password = doc, data, password
        self.pages, self.undo, self.redo = [], [], []
        rects = [p.rect for p in doc]
        mw = max(r.width for r in rects)
        y = 0
        for i, r in enumerate(rects):
            it = PageItem(i, r.width, r.height, (mw - r.width) / 2, y)
            self.sc.addItem(it)
            self.pages.append(it)
            y += r.height + GAP
        self.sc.setSceneRect(-GAP, -GAP, mw + 2 * GAP, y + GAP)
        self.resetTransform()
        self.zoom = 1.0
        QTimer.singleShot(0, self.first_view)

    def set_zoom(self, z):
        z = max(0.2, min(z, 8))
        f = z / self.zoom
        self.zoom = z
        self.scale(f, f)
        self.timer.start()

    def first_view(self):
        self.fit_width()
        self.verticalScrollBar().setValue(0)  # AnchorUnderMouse scrolls mid-document while scaling

    def fit_width(self):
        if self.pages:
            self.set_zoom((self.viewport().width() - 2) / self.sc.sceneRect().width())

    def refresh(self):
        if not self.doc:
            return
        vr = self.mapToScene(self.viewport().rect()).boundingRect()
        ext = vr.adjusted(0, -vr.height(), 0, vr.height())
        s = self.zoom * self.devicePixelRatioF()
        for p in self.pages:
            if p.sceneBoundingRect().intersects(ext):
                p.render(self.doc, s)
            else:
                p.clear()
        c = vr.center().y()
        n = min(self.pages, key=lambda p: abs(p.sceneBoundingRect().center().y() - c))
        self.pageInfo.emit(n.idx + 1, len(self.pages))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.timer.start()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.set_zoom(self.zoom * (1.15 if e.angleDelta().y() > 0 else 1 / 1.15))
            e.accept()
        else:
            super().wheelEvent(e)

    # ---- tools
    def set_tool(self, t):
        self.tool = t
        self.sc.clearSelection()
        self.sc.clearFocus()
        if t == "select":
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self.viewport().unsetCursor()
        else:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.IBeamCursor if t == "text" else Qt.CrossCursor)

    def page_at(self, pt):
        for p in self.pages:
            if p.sceneBoundingRect().contains(pt):
                return p

    def _clamp(self, pt):
        r = self.pg.sceneBoundingRect()
        return QPointF(min(max(pt.x(), r.left()), r.right()), min(max(pt.y(), r.top()), r.bottom()))

    def _push(self, op):
        self.undo.append(op)
        self.redo.clear()
        self.changed.emit()

    def _apply(self, op, undo):
        kind, items = op
        add = (kind == "add") != undo
        for it in items:
            if add and it.scene() is None:
                self.sc.addItem(it)
            elif not add and it.scene() is self.sc:
                self.sc.removeItem(it)

    def do_undo(self):
        self.sc.clearFocus()
        if self.undo:
            op = self.undo.pop()
            self._apply(op, True)
            self.redo.append(op)
            self.changed.emit()

    def do_redo(self):
        if self.redo:
            op = self.redo.pop()
            self._apply(op, False)
            self.undo.append(op)
            self.changed.emit()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self.doc or self.tool == "select":
            return super().mousePressEvent(e)
        pos = self.mapToScene(e.position().toPoint())
        pg = self.page_at(pos)
        if self.tool == "eraser":
            self.erasing = []
            return self._erase(pos)
        if self.tool == "text":
            if isinstance(self.itemAt(e.position().toPoint()), (ZText, Grip)):
                return super().mousePressEvent(e)
            if pg:
                t = ZText(self, pg.idx)
                t.style(self.font, self.pen_color, self.tx_stretch, self.tx_line)
                t.setPos(pos)
                t.setZValue(10)
                self.sc.addItem(t)
                t.setSelected(True)
                t.edit()
            return
        if not pg:
            return
        self.pg, self.start = pg, pos
        if self.tool == "pen":
            path = QPainterPath(pos)
            path.lineTo(pos + QPointF(0.01, 0))
            it = QGraphicsPathItem(path)
            it.setPen(QPen(self.pen_color, self.width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        else:
            it = QGraphicsRectItem(QRectF(pos, pos))
            c = QColor(self.hl_color)
            c.setAlpha(105)
            it.setBrush(c)
            it.setPen(QPen(Qt.NoPen))
        it.setData(0, "ann")
        it.setData(1, pg.idx)
        it.setFlags(QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemIsMovable)
        it.setZValue(10)
        self.sc.addItem(it)
        self.cur = it

    def mouseMoveEvent(self, e):
        left = e.buttons() & Qt.LeftButton
        if self.cur is not None and left:
            pos = self._clamp(self.mapToScene(e.position().toPoint()))
            if self.tool == "pen":
                p = self.cur.path()
                p.lineTo(pos)
                self.cur.setPath(p)
            else:
                self.cur.setRect(QRectF(self.start, pos).normalized())
        elif self.erasing is not None and left:
            self._erase(self.mapToScene(e.position().toPoint()))
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.cur is not None:
            it, self.cur = self.cur, None
            if self.tool == "highlight" and (it.rect().width() < 2 or it.rect().height() < 2):
                self.sc.removeItem(it)
            else:
                self._push(("add", [it]))
        elif self.erasing is not None:
            items, self.erasing = self.erasing, None
            if items:
                self._push(("del", items))
        else:
            super().mouseReleaseEvent(e)

    def _erase(self, pos):
        r = 6 / self.zoom
        for it in self.sc.items(QRectF(pos.x() - r, pos.y() - r, 2 * r, 2 * r)):
            if it.data(0) == "ann":
                self.sc.removeItem(it)
                self.erasing.append(it)
                break  # topmost only

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Delete:
            fi = self.sc.focusItem()
            editing = isinstance(fi, ZText) and fi.textInteractionFlags() != Qt.NoTextInteraction
            items = [i for i in self.sc.selectedItems() if i.data(0) == "ann"]
            if not editing and items:
                for i in items:
                    self.sc.removeItem(i)
                self._push(("del", items))
                return
        super().keyPressEvent(e)

    def text_done(self, it):
        def fin():  # deferred: never delete an item inside its own focus event
            if it.scene() is None:
                return
            if not it.toPlainText().strip():
                self.sc.removeItem(it)
                if not it.pending:
                    self._push(("del", [it]))
            elif it.pending:
                self._push(("add", [it]))
            else:
                self.changed.emit()
            it.pending = False
        QTimer.singleShot(0, fin)

    def apply_text_style(self):
        fi = self.sc.focusItem()
        for it in self.sc.selectedItems() + ([fi] if fi else []):
            if isinstance(it, ZText):
                it.style(self.font, self.pen_color, self.tx_stretch, self.tx_line)

    # ---- export
    def ann_page(self, it):
        p = self.page_at(it.sceneBoundingRect().center())
        return p.idx if p else it.data(1)

    def overlay_pdf(self):
        self.sc.clearSelection()
        self.sc.clearFocus()
        idxs = sorted({self.ann_page(i) for i in self.sc.items() if i.data(0) == "ann"})
        if not idxs:
            return None, []
        for p in self.pages:
            p.setVisible(False)
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        w = QPdfWriter(buf)
        w.setResolution(72)  # 1 unit = 1 pt = scene unit
        w.setPageMargins(QMarginsF(0, 0, 0, 0))
        painter = None
        try:
            for n, i in enumerate(idxs):
                r = self.pages[i].sceneBoundingRect()
                w.setPageSize(QPageSize(QSizeF(r.width(), r.height()), QPageSize.Point))
                if n == 0:
                    painter = QPainter(w)
                    painter.setRenderHint(QPainter.Antialiasing)
                else:
                    w.newPage()
                self.sc.render(painter, QRectF(0, 0, r.width(), r.height()), r)
        finally:
            if painter:
                painter.end()
            for p in self.pages:
                p.setVisible(True)
        return bytes(buf.data()), idxs

    def build(self):
        ov, idxs = self.overlay_pdf()
        out = fitz.open("pdf", self.data)
        if out.needs_pass:
            out.authenticate(self.password)
        if ov:
            o = fitz.open("pdf", ov)
            for n, i in enumerate(idxs):
                pg = out[i]
                if pg.rotation:
                    pg.show_pdf_page(pg.rect * pg.derotation_matrix, o, n, rotate=pg.rotation)
                else:
                    pg.show_pdf_page(pg.rect, o, n)
        return out

    def save(self, path):
        out = self.build()
        tmp = path + ".tmp"
        out.save(tmp, garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        out.close()
        os.replace(tmp, path)

    def print_to(self, printer, a, b):
        d = self.build()
        s = min(printer.resolution(), 300) / 72  # cap dpi: 1200dpi page = hundreds of MB
        painter = None
        for n, i in enumerate(range(a - 1, b)):
            pg = d[i]
            printer.setPageOrientation(QPageLayout.Landscape if pg.rect.width > pg.rect.height
                                       else QPageLayout.Portrait)
            if n == 0:
                painter = QPainter(printer)
            else:
                printer.newPage()
            pm = pg.get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
            img = QImage(pm.samples, pm.width, pm.height, pm.stride, QImage.Format_RGB888).copy()
            pr = printer.pageRect(QPrinter.DevicePixel)
            sz = img.size().scaled(int(pr.width()), int(pr.height()), Qt.KeepAspectRatio)
            painter.drawImage(QRectF((pr.width() - sz.width()) / 2, 0, sz.width(), sz.height()), img)
        if painter:
            painter.end()
        d.close()


# ---------------------------------------------------------------- window
class Bus(QObject):
    found = Signal(str, bool)


class ColorBtn(QPushButton):
    changed = Signal(QColor)

    def __init__(self, color, tip):
        super().__init__()
        self.c = QColor(color)
        self.setFixedSize(30, 26)
        self.setToolTip(tip)
        self._paint()
        self.clicked.connect(self._pick)

    def set_color(self, c):
        self.c = QColor(c)
        self._paint()

    def _paint(self):
        self.setStyleSheet("background:%s;border:2px solid #9ca3af;border-radius:4px;" % self.c.name())

    def _pick(self):
        c = QColorDialog.getColor(self.c, self, "เลือกสี")
        if c.isValid():
            self.c = c
            self._paint()
            self.changed.emit(c)


QSS = """
QMainWindow{background:#1b1d1f}
QToolBar{background:#16181a;border:0;border-bottom:2px solid #22C55E;padding:3px}
QFrame#grp{border-right:1px solid #3a3f45}
QLabel{color:#9ca3af}
QLabel#cap{color:#22C55E;font-size:10px;font-weight:bold}
QToolButton{color:#e5e7eb;background:#2b2f33;border:1px solid #3a3f45;border-radius:6px;
  min-width:54px;min-height:46px;padding:2px 6px;font-size:12px}
QToolButton:hover{background:#383d42}
QToolButton:checked{background:#22C55E;color:#0b0d0e;font-weight:bold;border-color:#22C55E}
QToolButton#mini{min-width:28px;min-height:26px;padding:0 4px;font-size:13px}
QSpinBox,QFontComboBox{background:#232629;color:#e5e7eb;border:1px solid #4b5563;border-radius:4px;padding:2px;min-height:24px}
QStatusBar{background:#16181a;color:#9ca3af}
QPushButton#upd{background:#22C55E;color:#000;border:0;border-radius:4px;padding:2px 10px;font-weight:bold}
QToolTip{background:#232629;color:#e5e7eb;border:1px solid #22C55E}
"""


class Group(QFrame):
    """one titled toolbar category"""
    def __init__(self, title, *widgets):
        super().__init__()
        self.setObjectName("grp")
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 2, 8, 2)
        v.setSpacing(2)
        row = QHBoxLayout()
        row.setSpacing(4)
        v.addLayout(row)
        for w in widgets:
            row.addWidget(w)
        cap = QLabel(title)
        cap.setObjectName("cap")
        cap.setAlignment(Qt.AlignCenter)
        v.addWidget(cap)


def hbox(*widgets):
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        h.addWidget(x)
    h.addStretch()
    return w


class Main(QMainWindow):
    TOOLS = ["select", "pen", "highlight", "eraser", "text"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 800)
        self.setAcceptDrops(True)
        self.setStyleSheet(QSS)
        self.cfg = QSettings("ZAIA", "ZAIA PDF")
        self.path = ""
        self.dirty = False
        self.cv = Canvas()
        self.setCentralWidget(self.cv)
        self.cv.changed.connect(lambda: setattr(self, "dirty", True))
        self.bus = Bus()
        self.bus.found.connect(self.on_found)

        def btn(text, fn, key=None, checkable=False, mini=False):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(text.replace("\n", " ") + (" (%s)" % key if key else ""))
            b.setCheckable(checkable)
            if mini:
                b.setObjectName("mini")
            if key:
                b.setShortcut(QKeySequence(key))
            b.clicked.connect(fn)
            return b

        # tools (exclusive, active one turns green)
        grp = QButtonGroup(self)
        tools = []
        for i, (k, txt) in enumerate([("select", "↖\nเลือก/ย้าย"), ("pen", "✎\nปากกา"),
                                      ("highlight", "▰\nไฮไลท์"), ("eraser", "⌫\nยางลบ"),
                                      ("text", "T\nข้อความ")]):
            b = btn(txt, lambda _=False, k=k: self.pick(k), "Ctrl+%d" % (i + 1), True)
            grp.addButton(b)
            tools.append(b)
        tools[0].setChecked(True)
        self.tool_btns = tools

        # per-tool options
        self.cb_pen = ColorBtn(self.cv.pen_color, "สีปากกา")
        self.cb_txt = ColorBtn(self.cv.pen_color, "สีข้อความ")
        self.cb_hl = ColorBtn(self.cv.hl_color, "สีไฮไลท์")
        self.cb_pen.changed.connect(self.set_pen_color)
        self.cb_txt.changed.connect(self.set_pen_color)
        self.cb_hl.changed.connect(lambda c: setattr(self.cv, "hl_color", c))
        sw = QSpinBox()
        sw.setRange(1, 100)
        sw.setValue(self.cv.width)
        sw.valueChanged.connect(lambda v: setattr(self.cv, "width", v))
        self.fc = QFontComboBox()
        self.fc.setCurrentFont(QFont("Tahoma"))
        self.fc.currentFontChanged.connect(self.font_changed)
        self.fs = QSpinBox()
        self.fs.setRange(4, 400)
        self.fs.setValue(16)
        self.fs.valueChanged.connect(self.font_changed)
        self.fw = QComboBox()
        for label, w in [("บางมาก", QFont.Thin), ("บาง", QFont.Light), ("ปกติ", QFont.Normal),
                         ("กลาง", QFont.Medium), ("กึ่งหนา", QFont.DemiBold), ("หนา", QFont.Bold),
                         ("หนามาก", QFont.Black)]:
            self.fw.addItem(label, w)
        self.fw.setCurrentIndex(2)
        self.fw.currentIndexChanged.connect(self.font_changed)
        self.fi = btn("I", self.font_changed, None, True, True)
        self.fu = btn("U", self.font_changed, None, True, True)

        def pct(v, lo, hi):
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setValue(v)
            sp.setSuffix(" %")
            sp.valueChanged.connect(self.font_changed)
            return sp
        self.fls = pct(100, 20, 500)   # letter spacing
        self.fx = pct(100, 30, 300)    # horizontal scale (compress / expand)
        self.fl = pct(100, 50, 400)    # line height
        self.fls.setToolTip("ระยะห่างตัวอักษร (100% = ปกติ)")
        self.fx.setToolTip("บีบ/ขยายความกว้างตัวอักษร (100% = ปกติ)")
        self.fl.setToolTip("ระยะห่างระหว่างบรรทัด (100% = ปกติ)")
        self.stack = QStackedWidget()  # order = TOOLS
        for ws in ([QLabel("คลิกเลือก • ลากย้าย • Delete = ลบ • ดับเบิลคลิกข้อความ = แก้")],
                   [QLabel("สี"), self.cb_pen, QLabel("ความหนา"), sw],
                   [QLabel("สีไฮไลท์"), self.cb_hl, QLabel("ลากเป็นกรอบทับข้อความ")],
                   [QLabel("คลิก/ลากทับสิ่งที่เพิ่มไว้เพื่อลบ (ไม่แตะเนื้อหา PDF เดิม)")],
                   None):
            if ws is None:  # text page: 2 rows
                pg = QWidget()
                v = QVBoxLayout(pg)
                v.setContentsMargins(0, 0, 0, 0)
                v.setSpacing(3)
                v.addWidget(hbox(QLabel("สี"), self.cb_txt, QLabel("ฟอนต์"), self.fc, self.fs,
                                 QLabel("น้ำหนัก"), self.fw, self.fi, self.fu))
                v.addWidget(hbox(QLabel("ห่างอักษร"), self.fls, QLabel("กว้างอักษร"), self.fx,
                                 QLabel("ระยะบรรทัด"), self.fl, QLabel("ลากจุดเขียวขวากล่อง = กำหนดความกว้าง/ตัดบรรทัด (ดับเบิลคลิก = อัตโนมัติ)")))
                self.stack.addWidget(pg)
            else:
                self.stack.addWidget(hbox(*ws))
        self.cv.sc.selectionChanged.connect(self.update_opts)

        ribbon = QWidget()
        h = QHBoxLayout(ribbon)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        for g in (Group("ไฟล์", btn("📂\nเปิด", self.open_dialog, "Ctrl+O"),
                        btn("💾\nบันทึกเป็น", self.save_as, "Ctrl+S"),
                        btn("🖨\nพิมพ์", self.print_pdf, "Ctrl+P")),
                  Group("แก้ไข", btn("↶\nย้อนกลับ", self.cv.do_undo, "Ctrl+Z"),
                        btn("↷\nทำซ้ำ", self.cv.do_redo, "Ctrl+Y")),
                  Group("เครื่องมือ", *tools),
                  Group("ตั้งค่าเครื่องมือ", self.stack),
                  Group("มุมมอง", btn("−\nย่อ", lambda: self.cv.set_zoom(self.cv.zoom / 1.2), "Ctrl+-"),
                        btn("+\nขยาย", lambda: self.cv.set_zoom(self.cv.zoom * 1.2), "Ctrl+="),
                        btn("⤢\nพอดีกว้าง", self.cv.fit_width, "Ctrl+0")),
                  Group("ช่วยเหลือ", btn("⬆\nอัปเดต", self.check_update),
                        btn("ℹ\nเกี่ยวกับ", lambda: QMessageBox.information(
                            self, APP_NAME, "%s %s (Beta)\nเพิ่มปากกา/ไฮไลท์/ข้อความทับบน PDF\nไม่แก้ข้อความเดิมใน PDF" % (APP_NAME, APP_VER))))):
            h.addWidget(g)
        h.addStretch()
        bar = QToolBar()
        bar.setMovable(False)
        bar.addWidget(ribbon)
        self.addToolBar(bar)

        self.pl = QLabel("")
        self.statusBar().addPermanentWidget(self.pl)
        self.cv.pageInfo.connect(lambda a, n: self.pl.setText("หน้า %d / %d  |  %d%%" % (a, n, self.cv.zoom * 100)))
        self.upd = QPushButton()
        self.upd.setObjectName("upd")
        self.upd.hide()
        self.upd.clicked.connect(self.check_update)
        self.statusBar().addWidget(self.upd)
        QTimer.singleShot(1500, self.silent_check)

    def pick(self, k):
        self.tool_btns[self.TOOLS.index(k)].setChecked(True)
        self.cv.set_tool(k)
        self.update_opts()

    def update_opts(self):
        t = self.cv.tool
        sel = [i for i in self.cv.sc.selectedItems() if isinstance(i, ZText)]
        if t == "select" and sel:
            t = "text"  # let user restyle a selected text box
        if len(sel) == 1:
            self.load_text_style(sel[0])
        self.stack.setCurrentIndex(self.TOOLS.index(t))

    # ---- font / color
    def set_pen_color(self, c):
        self.cv.pen_color = c
        self.cb_pen.set_color(c)
        self.cb_txt.set_color(c)
        self.cv.apply_text_style()

    def font_changed(self, *_):
        f = QFont(self.fc.currentFont())
        f.setPointSize(self.fs.value())
        f.setWeight(self.fw.currentData())
        f.setItalic(self.fi.isChecked())
        f.setUnderline(self.fu.isChecked())
        f.setLetterSpacing(QFont.PercentageSpacing, float(self.fls.value()))  # % of glyph width: same on screen and PDF
        self.cv.font = f
        self.cv.tx_stretch, self.cv.tx_line = self.fx.value(), self.fl.value()
        self.cv.apply_text_style()

    def load_text_style(self, it):
        """show the selected text box's own style in the controls (without applying anything)"""
        f = it.font()
        ws = [self.fw.itemData(i) for i in range(self.fw.count())]
        wv = lambda w: getattr(w, "value", w)
        ctrls = (self.fc, self.fs, self.fw, self.fi, self.fu, self.fls, self.fx, self.fl)
        for c in ctrls:
            c.blockSignals(True)
        self.fc.setCurrentFont(f)
        self.fs.setValue(max(4, f.pointSize()))
        self.fw.setCurrentIndex(min(range(len(ws)), key=lambda i: abs(wv(ws[i]) - wv(f.weight()))))
        self.fi.setChecked(f.italic())
        self.fu.setChecked(f.underline())
        self.fls.setValue(int(f.letterSpacing()) if f.letterSpacingType() == QFont.PercentageSpacing else 100)
        self.fx.setValue(it.sx)
        self.fl.setValue(it.lh)
        for c in ctrls:
            c.blockSignals(False)
        col = it.defaultTextColor()
        self.cb_pen.set_color(col)
        self.cb_txt.set_color(col)
        self.cv.font, self.cv.pen_color = QFont(f), col
        self.cv.tx_stretch, self.cv.tx_line = it.sx, it.lh

    # ---- file
    def confirm_discard(self):
        if self.dirty and self.cv.doc:
            return QMessageBox.question(self, APP_NAME, "มีการแก้ไขที่ยังไม่บันทึก ทิ้งหรือไม่?",
                                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes
        return True

    def open_dialog(self):
        f, _ = QFileDialog.getOpenFileName(self, "เปิด PDF", "", "PDF (*.pdf)")
        if f:
            self.open_path(f)

    def open_path(self, f):
        if not self.confirm_discard():
            return
        try:
            data = Path(f).read_bytes()  # read once: original file is never locked or modified
            doc = fitz.open("pdf", data)
            pw = ""
            while doc.needs_pass:  # needs_pass can stay set after success, so break on authenticate()
                pw, ok = QInputDialog.getText(self, "PDF มีรหัสผ่าน", "รหัสผ่าน:", QLineEdit.Password)
                if not ok:
                    return
                if doc.authenticate(pw):
                    break
                QMessageBox.warning(self, APP_NAME, "รหัสผ่านไม่ถูกต้อง")
            self.cv.set_doc(doc, data, pw)
        except Exception as e:
            return QMessageBox.critical(self, APP_NAME, "เปิดไฟล์ไม่ได้:\n%s: %s" % (type(e).__name__, e))
        self.path, self.dirty = f, False
        self.setWindowTitle("%s - %s" % (Path(f).name, APP_NAME))

    def save_as(self):
        if not self.cv.doc:
            return
        p = Path(self.path)
        f, _ = QFileDialog.getSaveFileName(self, "บันทึกเป็น", str(p.with_name(p.stem + "_edited.pdf")), "PDF (*.pdf)")
        if not f:
            return
        if not f.lower().endswith(".pdf"):
            f += ".pdf"
        if Path(f).resolve() == p.resolve() and QMessageBox.question(
                self, APP_NAME, "เขียนทับไฟล์ต้นฉบับ? ย้อนกลับไม่ได้",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            self.cv.save(f)
        except Exception as e:
            return QMessageBox.critical(self, APP_NAME, "บันทึกไม่สำเร็จ:\n%s: %s" % (type(e).__name__, e))
        self.dirty = False
        self.statusBar().showMessage("บันทึกแล้ว: " + f, 6000)

    def print_pdf(self):
        if not self.cv.doc:
            return
        n = len(self.cv.pages)
        pr = QPrinter(QPrinter.HighResolution)
        dlg = QPrintDialog(pr, self)
        dlg.setOption(QAbstractPrintDialog.PrintPageRange, True)
        dlg.setMinMax(1, n)
        if dlg.exec() != QPrintDialog.Accepted:
            return
        a, b = (pr.fromPage(), pr.toPage()) if pr.printRange() == QPrinter.PageRange else (1, n)
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            self.cv.print_to(pr, max(1, a), min(n, b or n))
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, "พิมพ์ไม่สำเร็จ:\n%s: %s" % (type(e).__name__, e))
        finally:
            QApplication.restoreOverrideCursor()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.toLocalFile().lower().endswith(".pdf"):
                return self.open_path(u.toLocalFile())

    def closeEvent(self, e):
        e.accept() if self.confirm_discard() else e.ignore()

    # ---- update
    def silent_check(self):
        url = self.cfg.value("update_url", UPDATE_URL)
        if "YOUR_USER" in url:
            return

        def run():  # silent: notify only, never download, ignore all errors
            try:
                m = get_manifest(url)
                if vt(m["version"]) > vt(APP_VER):
                    self.bus.found.emit(str(m["version"]), int(m.get("setup_rev", 1)) > local_rev())
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def on_found(self, ver, full):
        self.upd.setText("⬆ มีรุ่นใหม่ %s%s" % (ver, " (ต้องติดตั้งตัวเต็ม)" if full else ""))
        self.upd.show()

    def check_update(self):
        url = self.cfg.value("update_url", UPDATE_URL)
        while True:
            try:
                self._update_flow(url)
                self.cfg.setValue("update_url", url)
                return
            except Exception as e:
                txt, ok = QInputDialog.getText(
                    self, "อัปเดตผิดพลาด",
                    "Error: %s: %s\n\nใส่ลิงก์ update.json ใหม่ แล้วกด OK เพื่อลองใหม่ (Cancel = เลิก):" % (type(e).__name__, e),
                    text=url)
                if not ok or not txt.strip():
                    return
                url = txt.strip()

    def _update_flow(self, url):
        m = get_manifest(url)
        ver, files = str(m["version"]), m["files"]
        if vt(ver) <= vt(APP_VER):
            return QMessageBox.information(self, APP_NAME, "เป็นรุ่นล่าสุดแล้ว (%s)" % APP_VER)
        if int(m.get("setup_rev", 1)) > local_rev():
            return QMessageBox.warning(self, APP_NAME, "รุ่น %s ต้องติดตั้งตัวเต็มใหม่ (setup_rev เปลี่ยน)\n%s" % (
                ver, m.get("setup_url", "ดาวน์โหลดตัวติดตั้งจากผู้พัฒนา")))
        changed = diff_files(files)
        if QMessageBox.question(self, APP_NAME, "อัปเดต %s → %s\nไฟล์ที่จะเขียน (%d):\n%s\n\n(สำรองเป็น .bak ให้)" % (
                APP_VER, ver, len(changed), "\n".join(changed) or "-"),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            apply_update(url.rsplit("/", 1)[0] + "/", files, changed)
        finally:
            QApplication.restoreOverrideCursor()
        QMessageBox.information(self, APP_NAME, "อัปเดตเป็น %s แล้ว ปิดแล้วเปิดโปรแกรมใหม่" % ver)


def dark_theme(app):
    app.setStyle("Fusion")
    p = QPalette()
    for role, c in [(QPalette.Window, "#1b1d1f"), (QPalette.WindowText, "#e5e7eb"), (QPalette.Base, "#232629"),
                    (QPalette.AlternateBase, "#2b2f33"), (QPalette.ToolTipBase, "#232629"),
                    (QPalette.ToolTipText, "#e5e7eb"), (QPalette.Text, "#e5e7eb"), (QPalette.Button, "#2b2f33"),
                    (QPalette.ButtonText, "#e5e7eb"), (QPalette.BrightText, "#ffffff"),
                    (QPalette.Highlight, "#22C55E"), (QPalette.HighlightedText, "#0b0d0e"),
                    (QPalette.Link, "#22C55E"), (QPalette.PlaceholderText, "#6b7280")]:
        p.setColor(role, QColor(c))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor("#6b7280"))
    app.setPalette(p)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    dark_theme(app)

    def hook(t, v, tb):  # pythonw has no console: keep errors visible
        import traceback
        msg = "".join(traceback.format_exception(t, v, tb))
        try:
            (APP_DIR / "crash.log").write_text(msg, encoding="utf-8")
        except OSError:
            pass
        QMessageBox.critical(None, APP_NAME, "เกิดข้อผิดพลาด (บันทึกใน crash.log):\n" + msg[-700:])
    sys.excepthook = hook
    app.setWindowIcon(QIcon(str(APP_DIR / "zaia.ico")))
    w = Main()
    w.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        w.open_path(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
