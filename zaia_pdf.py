# ZAIA PDF - PDF annotator (PySide6 + PyMuPDF). Adds ink/highlight/text on top of pages.
import sys, os, re, json, math, time, shutil, hashlib, tempfile, threading, urllib.request, urllib.parse
from pathlib import Path
import pymupdf as fitz
from PySide6.QtCore import (Qt, QPointF, QRectF, QSizeF, QSize, QMarginsF, QTimer, QBuffer, QIODevice,
                            Signal, QObject, QSettings)
from PySide6.QtGui import (QColor, QFont, QImage, QPainter, QPainterPath, QPalette,
                           QPen, QPixmap, QPageSize, QPageLayout, QPdfWriter, QIcon, QKeySequence,
                           QTransform, QTextCursor, QTextBlockFormat, QPolygonF, QIntValidator, QFontDatabase)
from PySide6.QtWidgets import (QApplication, QMainWindow, QGraphicsView, QGraphicsScene, QGraphicsItem,
                               QGraphicsRectItem, QGraphicsPixmapItem, QGraphicsPathItem,
                               QGraphicsTextItem, QPushButton, QColorDialog,
                               QFontComboBox, QLabel, QFileDialog, QMessageBox, QInputDialog,
                               QLineEdit, QToolButton, QFrame, QVBoxLayout, QHBoxLayout,
                               QWidget, QStackedWidget, QButtonGroup, QComboBox, QTabBar, QMenu, QGridLayout,
                               QWidgetAction, QSlider, QDialog, QPlainTextEdit)
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

APP_VER = "1.1.0"
APP_NAME = "ZAIA PDF"
UPDATE_URL = "https://raw.githubusercontent.com/Minos117/-/main/update.json"
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

    def geom(self):
        """the page rectangle in scene units (sceneBoundingRect() is inflated by the green frame's pen)"""
        return QRectF(self.pos(), self.rect().size())

    def mark(self, on):
        """green frame = the page the rotate buttons act on"""
        pen = QPen(QColor("#16A34A"), 3)
        pen.setCosmetic(True)
        self.setPen(pen if on else QPen(Qt.NoPen))


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
        self.sx = self.lh = self.wt = 100
        self.bold_px = 0.0
        self._outline = None
        self.setData(0, "ann")
        self.setData(1, page)
        self.setFlags(QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemIsMovable)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.grip = Grip(self)  # child item: hidden unless selected, never exported
        self.document().contentsChanged.connect(self.place_grip)
        self.document().contentsChanged.connect(self._dirty_outline)

    def _dirty_outline(self):
        self._outline = None

    def style(self, font, color, sx, lh, wt=100):
        """font (spacing/size/italic...), colour, width %, line height %, weight % (>100 = faux-bold stroke)"""
        self.prepareGeometryChange()
        self.setFont(font)
        self.setDefaultTextColor(color)
        self.sx, self.lh, self.wt = sx, lh, wt
        self.bold_px = font.pointSizeF() * 0.03 * (wt - 100) / 50 if wt > 100 else 0.0
        self._outline = None
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

    def boundingRect(self):
        r = super().boundingRect()
        b = self.bold_px
        return r.adjusted(-b, -b, b, b) if b > 0 else r

    def _glyph_outline(self):
        """vector outline of the shaped text (Thai marks included), used for the faux-bold stroke"""
        if self._outline is None:
            path = QPainterPath()
            self.document().size()  # forces layout; glyph runs are empty before it
            blk = self.document().begin()
            while blk.isValid():
                lay = blk.layout()
                for i in range(lay.lineCount()):  # per line: QTextLayout.glyphRuns() is empty for document layouts
                    for run in lay.lineAt(i).glyphRuns():
                        rf = run.rawFont()
                        for idx, pos in zip(run.glyphIndexes(), run.positions()):
                            g = rf.pathForGlyph(idx)
                            g.translate(pos + lay.position())
                            path.addPath(g)
                blk = blk.next()
            if path.isEmpty():
                return path  # not cached: try again on the next paint
            self._outline = path
        return self._outline

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if self.bold_px > 0:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(self.defaultTextColor(), self.bold_px, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(self._glyph_outline())
            painter.restore()

    def place_grip(self):
        r = super().boundingRect()
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


class ImgGrip(QGraphicsRectItem):
    """green square at the bottom-right corner of a selected image: drag = resize (keeps proportions)"""
    def __init__(self, parent):
        super().__init__(-7, -7, 14, 14, parent)
        self.setBrush(QColor("#16A34A"))
        self.setPen(QPen(QColor("#ffffff"), 1.5))
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        self.setCursor(Qt.SizeFDiagCursor)
        self.setZValue(20)
        self.hide()

    def mousePressEvent(self, e):
        e.accept()

    def mouseMoveEvent(self, e):
        p = self.parentItem()
        v = e.scenePos() - p.pos()
        a = math.radians(-p.rotation())  # into the image's own axes
        vx, vy = v.x() * math.cos(a) - v.y() * math.sin(a), v.x() * math.sin(a) + v.y() * math.cos(a)
        w0, h0 = p.pixmap().width(), p.pixmap().height()
        p.setScale(max(0.02, (vx * w0 + vy * h0) / (w0 * w0 + h0 * h0)))

    def mouseReleaseEvent(self, e):
        self.parentItem().canvas.changed.emit()


class ZImage(QGraphicsPixmapItem):
    def __init__(self, canvas, page, pixmap, scale):
        super().__init__(pixmap)
        self.canvas = canvas
        self.setTransformationMode(Qt.SmoothTransformation)
        self.setData(0, "ann")
        self.setData(1, page)
        self.setFlags(QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemIsMovable)
        self.setScale(scale)
        self.grip = ImgGrip(self)
        self.grip.setPos(pixmap.width(), pixmap.height())

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self.grip.setVisible(bool(value))
        return super().itemChange(change, value)


FORM_FONTS = ["TH SarabunPSK", "TH Sarabun New", "TH SarabunIT\u0e59", "Tahoma", "Leelawadee UI", "Segoe UI"]


def form_font():
    have = set(QFontDatabase.families())
    return next((f for f in FORM_FONTS if f in have), QApplication.font().family())


def field_png(wpt, hpt, text, size, color, align, multiline, fill, border, bwidth):
    """400 dpi picture of a filled-in field (Thai shaped by Qt, independent of the PDF renderer's fonts)"""
    S = 400 / 72
    img = QImage(max(1, math.ceil(wpt * S)), max(1, math.ceil(hpt * S)), QImage.Format_ARGB32)
    img.setDotsPerMeterX(2835)  # 72 dpi: 1 pt = 1 px before the scale below
    img.setDotsPerMeterY(2835)
    img.fill(QColor.fromRgbF(*fill) if fill else Qt.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
    p.scale(S, S)
    if border and bwidth:
        p.setPen(QPen(QColor.fromRgbF(*border), bwidth))
        p.drawRect(QRectF(bwidth / 2, bwidth / 2, wpt - bwidth, hpt - bwidth))
    f = QFont(form_font())
    f.setPointSizeF(size)
    p.setFont(f)
    p.setPen(QColor.fromRgbF(*color))
    flags = {1: Qt.AlignHCenter, 2: Qt.AlignRight}.get(align, Qt.AlignLeft)
    flags |= (Qt.AlignTop | Qt.TextWordWrap) if multiline else Qt.AlignVCenter
    area = QRectF(2, 1, max(1, wpt - 4), max(1, hpt - 2))
    p.setClipRect(area)
    p.drawText(area, int(flags), text)
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def apply_value(doc, page_no, xref, value):
    """set one form field's value (clearing needs reset(): update() ignores an empty value). For text-like fields
    the appearance is replaced by our own picture, so Thai always shows fully whatever PyMuPDF/MuPDF version runs."""
    page = doc[page_no]
    w = next((x for x in page.widgets() if x.xref == xref), None)
    if w is None:
        return
    ftype = w.field_type
    empty = value in ("", None)
    if empty:
        w.reset()
    else:
        w.field_value = value
        w.update()
    if empty or ftype not in (fitz.PDF_WIDGET_TYPE_TEXT, fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
        return
    r = w.rect
    wpt, hpt = r.width, r.height
    multi = bool(w.field_flags & fitz.PDF_TX_FIELD_IS_MULTILINE) and ftype == fitz.PDF_WIDGET_TYPE_TEXT
    size = w.text_fontsize if w.text_fontsize and w.text_fontsize > 0 else (12 if multi else min(max(hpt * 0.62, 6), 16))
    q = doc.xref_get_key(xref, "Q")
    png = field_png(wpt, hpt, str(value), size, w.text_color or (0, 0, 0), int(q[1]) if q[0] == "int" else 0,
                    multi, w.fill_color, w.border_color, w.border_width or 0)
    mini = fitz.open()
    mini.new_page(width=wpt, height=hpt).insert_image(fitz.Rect(0, 0, wpt, hpt), stream=png)
    tmp = doc.new_page(-1, width=wpt, height=hpt)  # scratch page: turns the picture into a form XObject
    form = tmp.show_pdf_page(tmp.rect, mini, 0)
    doc.delete_page(len(doc) - 1)
    doc.xref_set_key(xref, "AP", "<</N %d 0 R>>" % form)


class FormHit(QGraphicsRectItem):
    """clickable area over one PDF form field (only visible/active in the form tool)"""
    def __init__(self, parent, rect, info):
        super().__init__(rect, parent)
        self.info = info  # {xref, page, kind}
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setZValue(5)
        self._style(False)

    def _style(self, hover):
        pen = QPen(QColor("#2563eb"), 1.2 if hover else 0.8)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QColor(37, 99, 235, 70 if hover else 32))

    def hoverEnterEvent(self, e):
        self._style(True)

    def hoverLeaveEvent(self, e):
        self._style(False)


class _EditMixin:
    done = Signal(str, int)  # text, 0 = commit / 1 = next / -1 = previous / 9 = cancel

    def _finish(self, move):
        if not getattr(self, "_fin", False):
            self._fin = True
            self.done.emit(self.value_text(), move)

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Return, Qt.Key_Enter) and (not self.multiline or e.modifiers() & Qt.ControlModifier):
            self._finish(0)
        elif k == Qt.Key_Escape:
            self._finish(9)
        elif k == Qt.Key_Tab:
            self._finish(1)
        elif k == Qt.Key_Backtab:
            self._finish(-1)
        else:
            super().keyPressEvent(e)

    def focusNextPrevChild(self, nxt):
        return False  # let Tab reach keyPressEvent

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self._finish(0)


class FieldEdit(_EditMixin, QLineEdit):
    done = Signal(str, int)
    multiline = False

    def value_text(self):
        return self.text()


class FieldEditML(_EditMixin, QPlainTextEdit):
    done = Signal(str, int)
    multiline = True

    def value_text(self):
        return self.toPlainText()


class Canvas(QGraphicsView):
    changed = Signal()
    pageInfo = Signal(int, int)

    def __init__(self):
        sc = QGraphicsScene()
        super().__init__(sc)
        self.sc = sc  # keep python ref or the scene is garbage collected
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor("#cfd4da"))  # view bg, so sc.render() stays transparent
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
        self.tx_stretch = self.tx_line = self.tx_weight = 100
        self.undo, self.redo = [], []
        self.notes = []
        self.rot = {}       # extra page rotation (deg, clockwise) chosen in the app
        self.form = {}      # xref -> (page, value) of form fields filled in the app (re-applied when saving)
        self.form_items = []
        self._ed = None
        self._wpage = None
        self.cur_page = 0   # page the rotate buttons act on (the one at the centre of the view)
        self.cur = self.start = self.pg = self.erasing = None
        self.timer = QTimer(self, singleShot=True, interval=40)
        self.timer.timeout.connect(self.refresh)
        self.verticalScrollBar().valueChanged.connect(lambda *_: self.timer.start())
        self.set_tool("select")

    # ---- document
    def set_doc(self, doc, data, password="", keep=None):
        """keep=(zoom, vscroll, hscroll) restores the view (used after overwrite-save)"""
        self.sc.clear()
        self.doc, self.data, self.password = doc, data, password
        self.pages, self.undo, self.redo, self.rot, self.cur_page = [], [], [], {}, 0
        self.form, self.form_items, self._ed = {}, [], None
        self._layout()
        self._build_form()
        self.resetTransform()
        self.zoom = 1.0
        if keep:
            self.set_zoom(keep[0])
            self.verticalScrollBar().setValue(keep[1])
            self.horizontalScrollBar().setValue(keep[2])
        else:
            QTimer.singleShot(0, self.first_view)

    def _layout(self):
        rects = [p.rect for p in self.doc]
        mw = max(r.width for r in rects)
        y = 0
        for i, r in enumerate(rects):
            if i < len(self.pages):
                it = self.pages[i]
                it.setRect(0, 0, r.width, r.height)
                it.setPos((mw - r.width) / 2, y)
                it.scale_key = 0
                it.pix.setPixmap(QPixmap())
            else:
                it = PageItem(i, r.width, r.height, (mw - r.width) / 2, y)
                self.sc.addItem(it)
                self.pages.append(it)
            y += r.height + GAP
        self.sc.setSceneRect(-GAP, -GAP, mw + 2 * GAP, y + GAP)

    def rotate_page(self, i, deg):
        """rotate page i by deg (90/180/270, clockwise); everything added on it turns with it"""
        deg %= 360
        if not self.doc or not deg:
            return
        self.sc.clearSelection()
        self.sc.clearFocus()
        old_o = [p.geom().topLeft() for p in self.pages]
        old_wh = [(p.rect().width(), p.rect().height()) for p in self.pages]
        groups = {}
        for it in self.sc.items():
            if it.data(0) == "ann":
                groups.setdefault(self.ann_page(it), []).append(it)
        pg = self.doc[i]
        pg.set_rotation((pg.rotation + deg) % 360)
        self.rot[i] = (self.rot.get(i, 0) + deg) % 360
        self._layout()
        for j, items in groups.items():
            new_o = self.pages[j].geom().topLeft()
            W, H = old_wh[j]
            m = QTransform.fromTranslate(-old_o[j].x(), -old_o[j].y())
            if j == i:
                m = m * {90: QTransform(0, 1, -1, 0, H, 0), 180: QTransform(-1, 0, 0, -1, W, H),
                         270: QTransform(0, -1, 1, 0, 0, W)}[deg]
            m = m * QTransform.fromTranslate(new_o.x(), new_o.y())
            for it in items:
                if isinstance(it, QGraphicsPathItem):  # ink: geometry lives in scene coordinates
                    path = m.map(it.mapToScene(it.path()))
                    it.setPos(0, 0)
                    it.setPath(path)
                elif isinstance(it, QGraphicsRectItem):  # highlight
                    r = m.map(it.mapToScene(it.rect())).boundingRect()
                    it.setPos(0, 0)
                    it.setRect(r)
                else:  # text box / image: origin point + rotation
                    it.setPos(m.map(it.pos()))
                    if j == i:
                        it.setRotation((it.rotation() + deg) % 360)
        self.undo, self.redo = [], []  # earlier steps no longer match the new page geometry
        self._build_form()
        self.changed.emit()
        self.timer.start()
        self.ensureVisible(self.pages[i], 20, 20)

    def add_image(self, path):
        img = QImage(path)
        if img.isNull():
            raise ValueError("เปิดไฟล์รูปไม่ได้: " + path)
        if max(img.width(), img.height()) > 2400:  # keep memory and PDF size sane
            img = img.scaled(2400, 2400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pg = self.pages[self.cur_page]
        r = pg.geom()
        vis = self.mapToScene(self.viewport().rect()).boundingRect().intersected(r)
        c = vis.center() if not vis.isEmpty() else r.center()
        s = min(1.0, r.width() * 0.4 / img.width(), r.height() * 0.6 / img.height())
        it = ZImage(self, pg.idx, QPixmap.fromImage(img), s)
        it.setPos(c - QPointF(img.width() * s / 2, img.height() * s / 2))
        it.setZValue(10)
        self.sc.addItem(it)
        it.setSelected(True)
        self._push(("add", [it]))
        return it

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
            if p.geom().intersects(ext):
                p.render(self.doc, s)
            else:
                p.clear()
        c = vr.center().y()
        n = min(self.pages, key=lambda p: abs(p.geom().center().y() - c))
        self.cur_page = n.idx
        for p in self.pages:
            p.mark(p is n and len(self.pages) > 1)
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
        self._close_editor()
        for h in self.form_items:
            h.setVisible(t == "form")
        if t in ("select", "form"):
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self.viewport().unsetCursor()
        else:
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.IBeamCursor if t == "text" else Qt.CrossCursor)

    def page_at(self, pt):
        for p in self.pages:
            if p.geom().contains(pt):
                return p

    def _clamp(self, pt):
        r = self.pg.geom()
        return QPointF(min(max(pt.x(), r.left()), r.right()), min(max(pt.y(), r.top()), r.bottom()))

    def _push(self, op):
        self.undo.append(op)
        self.redo.clear()
        self.changed.emit()

    def _apply(self, op, undo):
        kind, items = op
        if kind == "form":
            page, before, after = items
            self._set_states(page, before if undo else after)
            return
        add = (kind == "add") != undo
        for it in items:
            if add and it.scene() is None:
                self.sc.addItem(it)
            elif not add and it.scene() is self.sc:
                self.sc.removeItem(it)

    # ---- PDF form fields (text, check boxes, radio buttons, drop-down lists)
    KINDS = {fitz.PDF_WIDGET_TYPE_TEXT: "text", fitz.PDF_WIDGET_TYPE_CHECKBOX: "check",
             fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio", fitz.PDF_WIDGET_TYPE_COMBOBOX: "combo",
             fitz.PDF_WIDGET_TYPE_LISTBOX: "list"}

    def _build_form(self):
        for h in self.form_items:
            if h.scene() is not None:
                self.sc.removeItem(h)
        self.form_items = []
        for i, page in enumerate(self.doc):
            for w in page.widgets():
                kind = self.KINDS.get(w.field_type)
                if not kind or w.field_flags & fitz.PDF_FIELD_IS_READ_ONLY:
                    continue
                r = fitz.Rect(w.rect * page.rotation_matrix)  # widget rects are in unrotated page space
                r.normalize()
                h = FormHit(self.pages[i], QRectF(r.x0, r.y0, r.width, r.height), {"xref": w.xref, "page": i, "kind": kind})
                h.setVisible(self.tool == "form")
                self.form_items.append(h)

    def _widget(self, info):
        # a widget is only usable while its page object is alive: keep that page referenced
        self._wpage = page = self.doc[info["page"]]
        for w in page.widgets():
            if w.xref == info["xref"]:
                return w

    @staticmethod
    def _on_state(w):
        try:
            return w.on_state() or "Yes"
        except Exception:
            return "Yes"

    def _states(self, w, page):
        """{xref: value} of this field (a whole group for radio buttons) - what undo/redo/save restore"""
        if w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
            return {x.xref: x.field_value for x in self.doc[page].widgets() if x.field_name == w.field_name}
        return {w.xref: w.field_value}

    def _set_states(self, page, states):
        cur = {w.xref: w.field_value for w in self.doc[page].widgets() if w.xref in states}
        for xref, val in states.items():
            if cur.get(xref) != val:
                apply_value(self.doc, page, xref, val)
            self.form[xref] = (page, val)
        self.pages[page].scale_key = 0  # re-render that page
        self.timer.start()
        self.changed.emit()

    def set_field(self, info, value):
        w = self._widget(info)
        if w is None or w.field_value == value:
            return
        before = self._states(w, info["page"])
        apply_value(self.doc, info["page"], info["xref"], value)
        after = self._states(self._widget(info), info["page"])
        for xref, val in after.items():
            self.form[xref] = (info["page"], val)
        self.pages[info["page"]].scale_key = 0
        self.timer.start()
        self._push(("form", (info["page"], before, after)))

    def edit_field(self, hit):
        info = hit.info
        w = self._widget(info)
        if w is None:
            return
        self._close_editor()
        k = info["kind"]
        if k == "check":
            on = str(w.field_value) not in ("Off", "False", "")
            self.set_field(info, "Off" if on else self._on_state(w))
        elif k == "radio":
            self.set_field(info, self._on_state(w))
        elif k in ("combo", "list"):
            self._choice_menu(hit, w)
        else:
            self._text_editor(hit, w)

    def _choice_menu(self, hit, w):
        cur = str(w.field_value or "")
        m = QMenu(self)
        acts = {}
        for c in (w.choice_values or []):
            exp, label = (c[0], c[-1]) if isinstance(c, (list, tuple)) else (c, c)
            a = m.addAction(("✓  " if str(exp) == cur or label == cur else "     ") + str(label))
            acts[a] = str(exp)
        custom = None
        if w.field_flags & fitz.PDF_CH_FIELD_IS_EDIT:
            m.addSeparator()
            custom = m.addAction("พิมพ์ค่าเอง…")
        r = self.mapFromScene(hit.mapToScene(hit.rect())).boundingRect()
        m.setMinimumWidth(max(120, r.width()))
        chosen = []
        m.triggered.connect(chosen.append)
        a = m.exec(self.viewport().mapToGlobal(r.bottomLeft())) or (chosen[0] if chosen else None)
        if a is None:
            return
        if a is custom:
            txt, ok = QInputDialog.getText(self, "กรอกค่า", "ค่า:", text=cur)
            if ok:
                self.set_field(hit.info, txt)
        else:
            self.set_field(hit.info, acts[a])

    def _text_editor(self, hit, w):
        r = self.mapFromScene(hit.mapToScene(hit.rect())).boundingRect()
        multi = bool(w.field_flags & fitz.PDF_TX_FIELD_IS_MULTILINE)
        ed = FieldEditML(self.viewport()) if multi else FieldEdit(self.viewport())
        f = QFont("Tahoma")
        f.setPixelSize(max(9, int((w.text_fontsize * self.zoom) if w.text_fontsize and w.text_fontsize > 0
                                  else (r.height() * 0.62 if not multi else 14 * self.zoom))))
        ed.setFont(f)
        ed.setStyleSheet("background:#ffffff;color:#111827;border:2px solid #16A34A;border-radius:2px;padding:0 3px")
        if multi:
            ed.setPlainText(str(w.field_value or ""))
        else:
            ed.setText(str(w.field_value or ""))
            if w.text_maxlen and w.text_maxlen > 0:
                ed.setMaxLength(w.text_maxlen)
            ed.selectAll()
        ed.setGeometry(r.adjusted(-1, -1, 1, 1))
        ed.done.connect(lambda text, move, hit=hit: self._editor_done(hit, text, move))
        ed.show()
        ed.setFocus()
        self._ed = ed

    def _close_editor(self):
        if self._ed is not None:
            ed, self._ed = self._ed, None
            try:
                ed.done.disconnect()
            except (TypeError, RuntimeError):
                pass
            ed.hide()
            ed.deleteLater()

    def _editor_done(self, hit, text, move):
        ed, self._ed = self._ed, None
        if ed is not None:
            try:
                ed.done.disconnect()
            except (TypeError, RuntimeError):
                pass
            QTimer.singleShot(0, ed.deleteLater)
            ed.hide()
        if move != 9:
            w = self._widget(hit.info)
            if w is not None and w.text_maxlen and w.text_maxlen > 0:
                text = text[:w.text_maxlen]
            self.set_field(hit.info, text)
        if move in (1, -1):
            i = self.form_items.index(hit)
            seq = (self.form_items[i + 1:] if move == 1 else self.form_items[:i][::-1])
            nxt = next((h for h in seq if h.info["kind"] == "text"), None)
            if nxt is not None:
                self.ensureVisible(nxt)
                QTimer.singleShot(0, lambda: self.edit_field(nxt))

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
        if self.tool == "form" and e.button() == Qt.LeftButton and self.doc:
            it = self.itemAt(e.position().toPoint())
            if isinstance(it, FormHit):
                return self.edit_field(it)
            return super().mousePressEvent(e)
        if e.button() != Qt.LeftButton or not self.doc or self.tool == "select":
            return super().mousePressEvent(e)
        pos = self.mapToScene(e.position().toPoint())
        pg = self.page_at(pos)
        if self.tool == "eraser":
            self.erasing = []
            return self._erase(pos)
        if self.tool == "text":
            if isinstance(self.itemAt(e.position().toPoint()), (ZText, Grip, ImgGrip)):
                return super().mousePressEvent(e)
            if pg:
                t = ZText(self, pg.idx)
                t.style(self.font, self.pen_color, self.tx_stretch, self.tx_line, self.tx_weight)
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
                it.style(self.font, self.pen_color, self.tx_stretch, self.tx_line, self.tx_weight)

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
                r = self.pages[i].geom()
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

    def form_hits(self):
        """number of form fields (date pickers, dropdowns, text boxes...) that added items sit on top of"""
        self.sc.clearSelection()
        n = 0
        rects = {}
        for it in self.sc.items():
            if it.data(0) == "ann":
                i = self.ann_page(it)
                o = self.pages[i].geom().topLeft()
                r = it.sceneBoundingRect().translated(-o)
                rects.setdefault(i, []).append(fitz.Rect(r.left(), r.top(), r.right(), r.bottom()))
        for i, rs in rects.items():
            for w in self.doc[i].widgets():
                wr = fitz.Rect(w.rect * self.doc[i].rotation_matrix)
                if any(wr.intersects(r) for r in rs):
                    n += 1
        return n

    def _base(self, flatten=False):
        """original pages + the rotations chosen in the app (+ flattened form fields), no overlay yet"""
        out = fitz.open("pdf", self.data)
        if out.needs_pass:
            out.authenticate(self.password)
        for xref, (pno, val) in self.form.items():  # values typed/chosen in the app
            cur = next((x.field_value for x in out[pno].widgets() if x.xref == xref), None)
            if cur is not None and cur != val:
                apply_value(out, pno, xref, val)
        for i, deg in self.rot.items():
            out[i].set_rotation((out[i].rotation + deg) % 360)
        notes = []
        if flatten and any(True for pg in out if pg.first_widget):
            out.bake(annots=False, widgets=True)
            notes.append("ทำฟอร์มเป็นข้อความถาวรแล้ว: ช่องกรอก/ปฏิทิน/ดรอปลิสต์ในไฟล์ใหม่จะกรอกต่อไม่ได้")
        return out, notes

    def _compose(self, flatten, ov, idxs, modes):
        """modes[page] = 'vector' (real text/fonts, default) or 'raster' (300 dpi image of the added items)"""
        out, notes = self._base(flatten)
        o = fitz.open("pdf", ov) if ov else None
        for n, i in enumerate(idxs):
            pg = out[i]
            rect = pg.rect * pg.derotation_matrix if pg.rotation else pg.rect
            if modes.get(i) == "raster":
                pg.insert_image(rect, stream=self._overlay_png(i), rotate=pg.rotation, overlay=True)
            else:
                pg.show_pdf_page(rect, o, n, rotate=pg.rotation)
        if self._strip_xfa(out):
            notes.append("ลบ XFA (ฟอร์มไดนามิกของ Adobe) แล้ว: สคริปต์ XFA (เช่น ปฏิทินแบบไดนามิก) อาจใช้ไม่ได้")
        return out, notes

    def _overlay_image(self, i, scale):
        """transparent QImage of everything added on page i (what the screen shows, without the PDF page)"""
        self.sc.clearSelection()
        self.sc.clearFocus()
        r = self.pages[i].geom()
        img = QImage(max(1, math.ceil(r.width() * scale)), max(1, math.ceil(r.height() * scale)), QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        for p in self.pages:
            p.setVisible(False)
        try:
            pt = QPainter(img)
            pt.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
            self.sc.render(pt, QRectF(0, 0, img.width(), img.height()), r)
            pt.end()
        finally:
            for p in self.pages:
                p.setVisible(True)
        return img

    def _overlay_png(self, i):
        r = self.pages[i].geom()
        img = self._overlay_image(i, min(300 / 72, 4096 / max(r.width(), r.height())))
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        return bytes(buf.data())

    def _verify(self, data, idxs, flatten):
        """re-open the PDF we are about to write and check that the added items are really visible in it
        (a viewer-independent renderer, like the one used for reloading and printing). Returns the bad pages."""
        bad = []
        doc = fitz.open("pdf", data)
        if doc.needs_pass:
            doc.authenticate(self.password)
        base, _ = self._base(flatten)
        S = 1.5
        for i in idxs:
            ov = self._overlay_image(i, S)
            w_, h_, bpl = ov.width(), ov.height(), ov.bytesPerLine()
            px = bytes(ov.constBits())
            a = doc[i].get_pixmap(matrix=fitz.Matrix(S, S), alpha=False)
            b = base[i].get_pixmap(matrix=fitz.Matrix(S, S), alpha=False)
            if abs(a.width - w_) > 3 or abs(a.height - h_) > 3 or (a.width, a.height) != (b.width, b.height):
                bad.append(i)  # the saved page does not even have the expected size
                continue
            w_, h_ = min(w_, a.width), min(h_, a.height)  # renderers round sizes differently by a pixel
            sa, sb = a.samples, b.samples
            seen = lost = 0
            for y in range(0, h_, 2):
                for x in range(0, w_, 2):
                    o = y * bpl + x * 4
                    if px[o + 3] < 200:
                        continue
                    k = y * a.stride + x * 3
                    ink = (px[o + 2], px[o + 1], px[o])  # ARGB32 little endian = B,G,R,A
                    if sum(abs(ink[c] - sb[k + c]) for c in range(3)) < 90:
                        continue  # item colour ~ page colour here: nothing to compare
                    seen += 1
                    if sum(abs(sa[k + c] - sb[k + c]) for c in range(3)) < 45:
                        lost += 1
            if seen >= 20 and lost / seen > 0.8:
                bad.append(i)
        return bad

    def _log(self, msg):
        try:
            with open(APP_DIR / "zaia.log", "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except OSError:
            pass

    def make_pdf(self, flatten=False):
        """bytes of the finished PDF. Added items are first embedded as real vector text; we then verify the result and
        switch the pages where they are not visible to a high-resolution image, so nothing is silently lost."""
        ov, idxs = self.overlay_pdf()
        modes = {}
        if idxs and getattr(self, "force_raster", False):
            modes = {i: "raster" for i in idxs}
        still_bad = []
        for attempt in (1, 2):
            out, notes = self._compose(flatten, ov, idxs, modes)
            data = out.tobytes(garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_KEEP)
            bad = self._verify(data, idxs, flatten) if idxs else []
            self._log("compose %d: pages %s modes %s -> not visible on %s" % (attempt, [i + 1 for i in idxs], modes or "vector", [i + 1 for i in bad] or "none"))
            if not bad:
                still_bad = []
                break
            still_bad = bad
            if attempt == 1:
                modes.update({i: "raster" for i in bad})
        self.notes = notes
        if still_bad:
            self.notes.append("คำเตือน: ตรวจแล้วยังไม่พบสิ่งที่เพิ่มบนหน้า %s ในไฟล์ที่บันทึก (ดูรายละเอียดใน zaia.log)" % ", ".join(str(i + 1) for i in still_bad))
            try:
                if ov:
                    (APP_DIR / "last_overlay.pdf").write_bytes(ov)
            except OSError:
                pass
        elif any(m == "raster" for m in modes.values()):
            pages = ", ".join(str(i + 1) for i, m in sorted(modes.items()) if m == "raster")
            self.notes.append("หน้า %s: ข้อความ/รูปที่เพิ่มถูกบันทึกเป็นภาพความละเอียดสูง (300 dpi) เพราะแบบตัวอักษรจริงแสดงไม่ขึ้นในไฟล์นี้ ภาพจะค้นหา/คัดลอกข้อความไม่ได้" % pages)
        return data

    @staticmethod
    def _strip_xfa(out):
        """Adobe renders dynamic XFA forms from XFA data and ignores page content, so added text would never show"""
        cat = out.pdf_catalog()
        t, v = out.xref_get_key(cat, "AcroForm")
        if t == "xref":
            af = int(v.split()[0])
            if out.xref_get_key(af, "XFA")[0] != "null":
                out.xref_set_key(af, "XFA", "null")
                out.xref_set_key(cat, "NeedsRendering", "null")
                return True
        elif t == "dict" and "/XFA" in v:
            out.xref_set_key(cat, "AcroForm/XFA", "null")
            out.xref_set_key(cat, "NeedsRendering", "null")
            return True
        return False

    def save(self, path, flatten=False):
        data = self.make_pdf(flatten)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def print_doc(self):
        """the finished PDF (items verified visible, form fields flattened) as a document to print"""
        d = fitz.open("pdf", self.make_pdf(True))
        if d.needs_pass:
            d.authenticate(self.password)
        return d

    def print_pages(self, printer, doc, pages, fit=True, orient="auto", gray=False):
        """pages = 0-based indexes; fit=True scales to the printable area, False prints at 100 %;
        orient = auto (by page shape) / portrait / landscape. The page is rasterised at <=300 dpi."""
        s = min(printer.resolution(), 300) / 72  # 1200 dpi pages would be hundreds of MB
        dpp = printer.resolution() / 72           # printer pixels per point
        painter = None
        for n, i in enumerate(pages):
            pg = doc[i]
            pw, ph = pg.rect.width, pg.rect.height
            land = orient == "landscape" or (orient == "auto" and pw > ph)
            printer.setPageOrientation(QPageLayout.Landscape if land else QPageLayout.Portrait)
            if n == 0:
                painter = QPainter(printer)
                painter.setRenderHint(QPainter.SmoothPixmapTransform)
            else:
                printer.newPage()
            pm = pg.get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
            img = QImage(pm.samples, pm.width, pm.height, pm.stride, QImage.Format_RGB888).copy()
            if gray:  # do not rely on the driver's colour setting
                img = img.convertToFormat(QImage.Format_Grayscale8)
            pr = printer.pageRect(QPrinter.DevicePixel)
            painter.drawImage(place(pr.width(), pr.height(), pw * dpp, ph * dpp, fit), img)
        if painter:
            painter.end()

    def print_to(self, printer, a, b):  # simple helper (pages a..b, 1-based)
        d = self.print_doc()
        self.print_pages(printer, d, list(range(a - 1, b)))
        d.close()


def place(avail_w, avail_h, pw, ph, fit):
    """rectangle for a page of size pw x ph centred on an area avail_w x avail_h (same units)"""
    k = min(avail_w / pw, avail_h / ph) if fit else 1.0
    return QRectF((avail_w - pw * k) / 2, (avail_h - ph * k) / 2, pw * k, ph * k)


def parse_pages(text, n):
    """'1-3, 5' -> [0, 1, 2, 4]; ValueError on anything invalid"""
    out = []
    for part in re.split(r"[,\s;]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(part)
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if a < 1 or b < a or b > n:
            raise ValueError(part)
        out.extend(range(a - 1, b))
    if not out:
        raise ValueError("empty")
    seen = set()
    return [i for i in out if not (i in seen or seen.add(i))]


# ---------------------------------------------------------------- window
class Bus(QObject):
    found = Signal(str, bool)


# ---------------------------------------------------------------- Office-style UI
def _poly(p, pts, close=True, fill=None):
    pg = QPolygonF([QPointF(x, y) for x, y in pts])
    if fill is not None:
        p.setBrush(fill)
    p.drawPolygon(pg) if close else p.drawPolyline(pg)
    p.setBrush(Qt.NoBrush)


def _undo(p, c):
    _poly(p, [(13, 6), (7, 12), (13, 18)], False)
    path = QPainterPath(QPointF(7, 12))
    path.lineTo(19, 12)
    path.cubicTo(27, 12, 27, 26, 19, 26)
    path.lineTo(11, 26)
    p.drawPath(path)


def _redo(p, c):
    p.save()
    p.translate(32, 0)
    p.scale(-1, 1)
    _undo(p, c)
    p.restore()


def _zoom(plus):
    def f(p, c):
        p.drawEllipse(QPointF(14, 14), 9, 9)
        p.drawLine(21, 21, 28, 28)
        p.drawLine(10, 14, 18, 14)
        if plus:
            p.drawLine(14, 10, 14, 18)
    return f


def _rot(cw):
    def f(p, c):
        if not cw:
            p.translate(32, 0)
            p.scale(-1, 1)
        p.drawArc(QRectF(7, 8, 18, 18), 90 * 16, -270 * 16)  # clockwise 3/4 circle from the top
        _poly(p, [(3, 21), (7, 17), (11, 21)], False)       # arrow head at its end (left, pointing up)
    return f


def _saveas(p, c):
    ICONS["save"](p, c)
    p.drawLine(24, 22, 30, 22)
    p.drawLine(27, 19, 27, 25)


ICONS = {
    "open": lambda p, c: _poly(p, [(3, 8), (12, 8), (15, 11), (28, 11), (28, 26), (3, 26)]),
    "save": lambda p, c: (p.drawRoundedRect(QRectF(5, 5, 22, 22), 2, 2), p.drawRect(QRectF(10, 5, 11, 7)),
                          p.drawRect(QRectF(9, 18, 14, 9))),
    "print": lambda p, c: (p.drawRect(QRectF(4, 11, 24, 12)), p.drawRect(QRectF(9, 4, 14, 7)),
                           p.drawRect(QRectF(9, 18, 14, 9))),
    "undo": _undo, "redo": _redo,
    "rotr": _rot(True), "rotl": _rot(False), "saveas": _saveas,
    "form": lambda p, c: (p.drawRoundedRect(QRectF(5, 4, 22, 24), 2, 2), p.drawRect(QRectF(9, 9, 5, 5)),
                          p.drawLine(17, 11, 24, 11), p.drawRect(QRectF(9, 18, 5, 5)), p.drawLine(17, 20, 24, 20)),
    "image": lambda p, c: (p.drawRoundedRect(QRectF(4, 6, 24, 20), 2, 2), p.drawEllipse(QPointF(11, 13), 2.2, 2.2),
                           _poly(p, [(5, 25), (13, 17), (18, 22), (22, 18), (27, 25)], False)),
    "select": lambda p, c: _poly(p, [(8, 4), (8, 25), (13, 20), (17, 28), (20, 27), (16, 19), (23, 19)], True, c),
    "pen": lambda p, c: (_poly(p, [(5, 27), (7, 19), (21, 5), (27, 11), (13, 25)]), p.drawLine(19, 7, 25, 13)),
    "marker": lambda p, c: (_poly(p, [(10, 17), (20, 6), (27, 13), (17, 24)]), p.drawLine(4, 28, 15, 28)),
    "eraser": lambda p, c: (_poly(p, [(5, 19), (17, 6), (26, 15), (14, 28)]), p.drawLine(11, 12, 20, 21),
                            p.drawLine(14, 28, 28, 28)),
    "text": lambda p, c: (p.drawLine(7, 7, 25, 7), p.drawLine(16, 7, 16, 26), p.drawLine(12, 26, 20, 26)),
    "zoomout": _zoom(False), "zoomin": _zoom(True),
    "fit": lambda p, c: (p.drawLine(4, 6, 4, 26), p.drawLine(28, 6, 28, 26), p.drawLine(8, 16, 24, 16),
                         _poly(p, [(12, 12), (8, 16), (12, 20)], False), _poly(p, [(20, 12), (24, 16), (20, 20)], False)),
    "update": lambda p, c: (p.drawLine(16, 26, 16, 8), _poly(p, [(9, 14), (16, 7), (23, 14)], False),
                            p.drawLine(6, 29, 26, 29)),
    "info": lambda p, c: (p.drawEllipse(QPointF(16, 16), 12, 12), p.drawLine(16, 14, 16, 22),
                          p.drawEllipse(QPointF(16, 10), 0.6, 0.6)),
    "lines": lambda p, c: (p.drawLine(13, 7, 28, 7), p.drawLine(13, 16, 28, 16), p.drawLine(13, 25, 28, 25),
                           p.drawLine(6, 6, 6, 26), _poly(p, [(3, 9), (6, 5), (9, 9)], False),
                           _poly(p, [(3, 23), (6, 27), (9, 23)], False)),
}


def ico(kind):
    """crisp vector icon: dark grey on light, white when the button is checked (green)"""
    def draw(color, a=1.0):
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(2, 2)
        p.setPen(QPen(QColor(color), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        ICONS[kind](p, QColor(color))
        p.end()
        pm.setDevicePixelRatio(2)
        return pm
    i = QIcon()
    i.addPixmap(draw("#374151"), QIcon.Normal, QIcon.Off)
    i.addPixmap(draw("#ffffff"), QIcon.Normal, QIcon.On)
    i.addPixmap(draw("#9ca3af"), QIcon.Disabled, QIcon.Off)
    return i


PALETTE = ["#000000", "#404040", "#7f7f7f", "#bfbfbf", "#ffffff", "#8d6e63",
           "#ff0000", "#ff9800", "#ffeb3b", "#22C55E", "#166534", "#00bcd4",
           "#1e66f5", "#7c3aed", "#ec4899", "#f87171", "#fde68a", "#86efac"]


class ColorDrop(QToolButton):
    """Word-style colour button: swatch + dropdown palette + 'more colours'"""
    changed = Signal(QColor)

    def __init__(self, title, color):
        super().__init__()
        self.c = QColor(color)
        self.setText(title)
        self.setToolTip(title)
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.setPopupMode(QToolButton.InstantPopup)
        self.setIconSize(QSize(34, 22))
        self._icon()
        m = QMenu(self)
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(8, 8, 8, 4)
        g.setSpacing(5)
        for i, col in enumerate(PALETTE):
            b = QToolButton()
            b.setFixedSize(28, 28)
            b.setStyleSheet("QToolButton{min-width:0;min-height:0;padding:0;background:%s;"
                            "border:1px solid #6b7280;border-radius:4px}QToolButton:hover{border:2px solid #22C55E}" % col)
            b.clicked.connect(lambda _=False, c=col: self._pick(c, m))
            g.addWidget(b, i // 6, i % 6)
        wa = QWidgetAction(m)
        wa.setDefaultWidget(w)
        m.addAction(wa)
        m.addAction("สีอื่น…", self._more)
        self.setMenu(m)

    def _icon(self):
        pm = QPixmap(34, 22)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setBrush(self.c)
        p.setPen(QPen(QColor("#6b7280"), 1.5))
        p.drawRoundedRect(QRectF(1, 1, 32, 20), 4, 4)
        p.end()
        self.setIcon(QIcon(pm))

    def set_color(self, c):
        self.c = QColor(c)
        self._icon()

    def _pick(self, col, menu):
        menu.close()
        self.set_color(col)
        self.changed.emit(self.c)

    def _more(self):
        c = QColorDialog.getColor(self.c, self, "เลือกสี")
        if c.isValid():
            self.set_color(c)
            self.changed.emit(c)


class NumCombo(QComboBox):
    """editable dropdown of presets: pick one or type any number"""
    valueChanged = Signal(int)

    def __init__(self, value, presets, lo, hi, width=84):
        super().__init__()
        self.lo, self.hi, self._last = lo, hi, value
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setValidator(QIntValidator(lo, hi, self))
        self.setFixedWidth(width)
        self.setMaxVisibleItems(12)
        for v in presets:
            self.addItem(str(v))
        self.setEditText(str(value))
        self.activated.connect(lambda _: self._emit())
        self.lineEdit().editingFinished.connect(self._emit)

    def value(self):
        try:
            return min(self.hi, max(self.lo, int(self.currentText())))
        except ValueError:
            return self._last

    def setValue(self, v):
        self._last = int(v)
        self.setEditText(str(int(v)))

    def _emit(self):
        v = self.value()
        self._last = v
        self.setEditText(str(v))
        self.valueChanged.emit(v)

    def wheelEvent(self, e):  # no accidental changes while scrolling the ribbon
        e.accept() if self.hasFocus() else e.ignore()


class PctSlider(QWidget):
    """title + slider + live % + reset button (easier than typing numbers)"""
    valueChanged = Signal(int)

    def __init__(self, title, lo, hi, tip=""):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        t = QLabel(title)
        t.setFixedWidth(64)
        self.s = QSlider(Qt.Horizontal)
        self.s.setRange(lo, hi)
        self.s.setValue(100)
        self.s.setFixedWidth(150)
        self.s.setPageStep(10)
        self.lbl = QLabel("100%")
        self.lbl.setFixedWidth(46)
        self.lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        rb = QToolButton()
        rb.setObjectName("mini")
        rb.setText("↺")
        rb.setToolTip("กลับเป็น 100%")
        rb.clicked.connect(lambda: self.s.setValue(100))
        for w in (t, self.s, self.lbl, rb):
            h.addWidget(w)
        if tip:
            for w in (t, self.s):
                w.setToolTip(tip)
        self.s.valueChanged.connect(self._changed)

    def _changed(self, v):
        self.lbl.setText("%d%%" % v)
        self.valueChanged.emit(v)

    def value(self):
        return self.s.value()

    def setValue(self, v, emit=True):
        self.s.blockSignals(not emit)
        self.s.setValue(int(v))
        self.s.blockSignals(False)
        self.lbl.setText("%d%%" % self.s.value())


def arrow_png():
    pm = QPixmap(12, 8)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#374151"))
    p.setPen(Qt.NoPen)
    p.drawPolygon(QPolygonF([QPointF(1, 1), QPointF(11, 1), QPointF(6, 7)]))
    p.end()
    f = os.path.join(tempfile.gettempdir(), "zaia_arrow.png")
    pm.save(f)
    return f.replace("\\", "/")


QSS = """
QMainWindow{background:#f3f4f6}
QLabel{color:#1f2937;font-size:12px}
QLabel#cap{color:#15803d;font-size:11px;font-weight:bold}
QTabBar{background:#f3f4f6}
QTabBar::tab{background:transparent;color:#374151;padding:8px 22px;font-size:14px;border:0;border-bottom:3px solid transparent}
QTabBar::tab:hover{background:#e5e7eb}
QTabBar::tab:selected{color:#15803d;font-weight:bold;border-bottom:3px solid #16A34A;background:#ffffff}
QWidget#panel{background:#ffffff;border-top:1px solid #d1d5db;border-bottom:2px solid #16A34A}
QFrame#grp{border:0;border-right:1px solid #d1d5db}
QFrame#tools{background:#ffffff;border-right:1px solid #d1d5db}
QToolButton{color:#1f2937;background:#f9fafb;border:1px solid #d1d5db;border-radius:7px;
  min-width:66px;min-height:68px;padding:3px 6px;font-size:12px}
QToolButton:hover{background:#e8f7ee;border-color:#16A34A}
QToolButton:checked{background:#16A34A;color:#ffffff;font-weight:bold;border-color:#15803d}
QToolButton:disabled{color:#9ca3af}
QToolButton::menu-indicator{subcontrol-position:bottom right;subcontrol-origin:padding;right:4px;bottom:3px}
QToolButton#mini{min-width:38px;min-height:30px;padding:0;font-size:15px}
QToolButton#tool{min-width:78px;min-height:72px;font-size:12px}
QComboBox{background:#ffffff;color:#111827;border:1px solid #9ca3af;border-radius:6px;padding:2px 8px;
  min-height:28px;font-size:13px;combobox-popup:0}
QComboBox:hover,QComboBox:focus{border-color:#16A34A}
QComboBox::drop-down{border:0;width:26px}
QComboBox::down-arrow{image:url(%ARROW%);width:12px;height:8px}
QComboBox QLineEdit{background:transparent;border:0;color:#111827;selection-background-color:#16A34A;selection-color:#ffffff}
QComboBox QAbstractItemView{background:#ffffff;color:#111827;border:1px solid #16A34A;
  selection-background-color:#16A34A;selection-color:#ffffff;outline:0}
QComboBox QAbstractItemView::item{min-height:30px;padding-left:10px}
QMenu{background:#ffffff;color:#111827;border:1px solid #d1d5db;padding:4px;menu-scrollable:1}
QMenu::item{padding:7px 26px;min-height:22px;font-size:13px}
QMenu::item:selected{background:#16A34A;color:#ffffff}
QMenu::separator{height:1px;background:#e5e7eb;margin:4px 8px}
QSlider::groove:horizontal{height:6px;background:#d1d5db;border-radius:3px}
QSlider::sub-page:horizontal{background:#16A34A;border-radius:3px}
QSlider::handle:horizontal{background:#ffffff;border:2px solid #16A34A;width:16px;margin:-7px 0;border-radius:9px}
QSlider::handle:horizontal:hover{background:#dcfce7}
QDialog{background:#f3f4f6}
QLineEdit{background:#ffffff;color:#111827;border:1px solid #9ca3af;border-radius:6px;min-height:28px;padding:0 8px;font-size:13px}
QLineEdit:focus{border-color:#16A34A}
QPushButton#primary{background:#16A34A;color:#ffffff;border:0;border-radius:8px;font-size:20px;font-weight:bold;min-height:58px;min-width:170px}
QPushButton#primary:hover{background:#15803d}
QPushButton#primary:disabled{background:#9ca3af}
QPushButton#plain{background:#f9fafb;color:#1f2937;border:1px solid #9ca3af;border-radius:6px;min-height:34px;padding:0 18px;font-size:13px}
QPushButton#plain:hover{border-color:#16A34A;background:#e8f7ee}
QLabel#title{font-size:26px;font-weight:bold;color:#111827}
QLabel#field{font-size:12px;color:#4b5563;font-weight:bold}
QStatusBar{background:#f3f4f6;color:#4b5563}
QPushButton#upd{background:#16A34A;color:#ffffff;border:0;border-radius:4px;padding:3px 12px;font-weight:bold}
QToolTip{background:#ffffff;color:#111827;border:1px solid #16A34A;padding:4px}
QScrollBar:vertical{background:#f3f4f6;width:14px}
QScrollBar::handle:vertical{background:#9ca3af;border-radius:6px;min-height:30px}
QScrollBar::add-line,QScrollBar::sub-line{height:0}
"""


class Group(QFrame):
    """one titled ribbon category: controls on top, caption below"""
    def __init__(self, title, *cols):
        super().__init__()
        self.setObjectName("grp")
        self.setFrameShape(QFrame.StyledPanel)  # needed for the QSS border to be drawn
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 6, 10, 2)
        v.setSpacing(2)
        row = QHBoxLayout()
        row.setSpacing(8)
        for c in cols:
            row.addWidget(c, 0, Qt.AlignTop)
        v.addLayout(row, 1)
        cap = QLabel(title)
        cap.setObjectName("cap")
        cap.setAlignment(Qt.AlignCenter)
        v.addWidget(cap)


def rows(*rs):
    """vertical stack of rows; a row is a widget or a list of widgets"""
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(5)
    for r in rs:
        if isinstance(r, (list, tuple)):
            h = QHBoxLayout()
            h.setSpacing(6)
            for x in r:
                h.addWidget(x)
            v.addLayout(h)
        else:
            v.addWidget(r)
    v.addStretch()
    return w


def big(text, kind, fn, key=None, checkable=False, name=None):
    b = QToolButton()
    b.setText(text)
    b.setIcon(ico(kind))
    b.setIconSize(QSize(30, 30))
    b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
    b.setToolTip(text.replace("\n", " ") + (" (%s)" % key if key else ""))
    b.setCheckable(checkable)
    if name:
        b.setObjectName(name)
    if key:
        b.setShortcut(QKeySequence(key))
    b.clicked.connect(fn)
    return b


def mini(text, fn, italic=False, underline=False):
    b = QToolButton()
    b.setObjectName("mini")
    b.setText(text)
    b.setCheckable(True)
    f = b.font()
    f.setItalic(italic)
    f.setUnderline(underline)
    f.setBold(not (italic or underline))
    b.setFont(f)
    b.clicked.connect(fn)
    return b


class PaperView(QWidget):
    """big preview: a sheet of the chosen paper with the page placed exactly as it will be printed"""
    MARGIN_PT = 12  # typical unprintable border, only for the preview (the printer reports its real one)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(460, 480)
        self.img, self.page_pts, self.paper, self.fit, self.gray = None, (595.0, 842.0), (595.0, 842.0), True, False

    def show_page(self, img, page_pts, paper_pts, fit, gray):
        self.img = img.convertToFormat(QImage.Format_Grayscale8) if (img is not None and gray) else img
        self.page_pts, self.paper, self.fit, self.gray = page_pts, paper_pts, fit, gray
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#9aa1ab"))
        pw, ph = self.paper
        k = min((self.width() - 56) / pw, (self.height() - 56) / ph)
        rw, rh = pw * k, ph * k
        x, y = (self.width() - rw) / 2, (self.height() - rh) / 2
        p.fillRect(QRectF(x + 5, y + 6, rw, rh), QColor(0, 0, 0, 70))
        p.fillRect(QRectF(x, y, rw, rh), QColor("#ffffff"))
        if self.img is not None:
            m = self.MARGIN_PT
            t = place(pw - 2 * m, ph - 2 * m, self.page_pts[0], self.page_pts[1], self.fit)
            p.setClipRect(QRectF(x + m * k, y + m * k, rw - 2 * m * k, rh - 2 * m * k))
            p.drawImage(QRectF(x + (m + t.x()) * k, y + (m + t.y()) * k, t.width() * k, t.height() * k), self.img)


class PrintDialog(QDialog):
    """Word-style print page: settings on the left, large live preview on the right"""
    PAPERS = [("A4", QPageSize.A4), ("A3", QPageSize.A3), ("A5", QPageSize.A5), ("Letter", QPageSize.Letter),
              ("Legal", QPageSize.Legal), ("F4 / Folio", QPageSize.Folio)]

    def __init__(self, cv, parent=None):
        super().__init__(parent)
        self.cv = cv
        self.setWindowTitle("พิมพ์ - " + APP_NAME)
        self.resize(1080, 760)
        self.n = len(cv.pages)
        self.cur = cv.cur_page
        self.pos = 0          # index into the list of pages to print (for the preview)
        self.cache = {}
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.doc = cv.print_doc()   # also what gets printed: verified, form fields flattened
        finally:
            QApplication.restoreOverrideCursor()
        self._printer = None

        side = QWidget()
        side.setFixedWidth(360)
        v = QVBoxLayout(side)
        v.setContentsMargins(22, 18, 18, 18)
        v.setSpacing(6)
        t = QLabel("พิมพ์")
        t.setObjectName("title")
        v.addWidget(t)
        top = QHBoxLayout()
        self.btn_print = QPushButton("พิมพ์")
        self.btn_print.setObjectName("primary")
        self.btn_print.setDefault(True)
        self.btn_print.clicked.connect(self.print_now)
        self.copies = NumCombo(1, [1, 2, 3, 4, 5, 10], 1, 99, 76)
        cl = QVBoxLayout()
        cl.addWidget(self.field("จำนวนชุด"))
        cl.addWidget(self.copies)
        top.addWidget(self.btn_print)
        top.addSpacing(10)
        top.addLayout(cl)
        top.addStretch()
        v.addLayout(top)
        v.addSpacing(8)

        self.printer = QComboBox()
        names = QPrinterInfo.availablePrinterNames()
        for nme in names:
            self.printer.addItem(nme)
        dflt = QPrinterInfo.defaultPrinterName()
        if dflt in names:
            self.printer.setCurrentText(dflt)
        if not names:
            self.printer.addItem("ไม่พบเครื่องพิมพ์ในเครื่องนี้")
            self.printer.setEnabled(False)
            self.btn_print.setEnabled(False)
        self.printer.currentIndexChanged.connect(self._printer_changed)
        self.paper = QComboBox()
        self.range = QComboBox()
        self.range.addItems(["ทุกหน้า", "หน้าปัจจุบัน (หน้า %d)" % (self.cur + 1), "ระบุหน้าเอง…"])
        self.pages_txt = QLineEdit()
        self.pages_txt.setPlaceholderText("เช่น 1-3, 5")
        self.pages_txt.setText("1-%d" % self.n)
        self.pages_txt.hide()
        self.range.currentIndexChanged.connect(self._range_changed)
        self.pages_txt.textChanged.connect(self._refresh)
        self.duplex = QComboBox()
        self.duplex.addItems(["พิมพ์ด้านเดียว", "สองด้าน (พลิกด้านยาว)", "สองด้าน (พลิกด้านสั้น)"])
        self.orient = QComboBox()
        self.orient.addItems(["อัตโนมัติ (ตามรูปร่างของหน้า)", "แนวตั้ง", "แนวนอน"])
        self.color = QComboBox()
        self.color.addItems(["สี", "ขาวดำ"])
        self.scale = QComboBox()
        self.scale.addItems(["พอดีหน้ากระดาษ", "ขนาดจริง (100%)"])
        for lbl, w in (("เครื่องพิมพ์", self.printer), ("หน้าที่พิมพ์", self.range), (None, self.pages_txt),
                       ("ด้านการพิมพ์", self.duplex), ("ขนาดกระดาษ", self.paper), ("ทิศทางกระดาษ", self.orient),
                       ("สี", self.color), ("ย่อ/ขยาย", self.scale)):
            if lbl:
                v.addWidget(self.field(lbl))
            v.addWidget(w)
        for c in (self.duplex, self.orient, self.color, self.scale, self.paper):
            c.currentIndexChanged.connect(self._refresh)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)
        v.addStretch()
        cancel = QPushButton("ยกเลิก")
        cancel.setObjectName("plain")
        cancel.clicked.connect(self.reject)
        v.addWidget(cancel)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        self.view = PaperView()
        rv.addWidget(self.view, 1)
        nav = QWidget()
        nav.setStyleSheet("background:#f3f4f6")
        nh = QHBoxLayout(nav)
        nh.setContentsMargins(10, 8, 10, 8)
        self.prev = QPushButton("◀")
        self.prev.setObjectName("plain")
        self.next = QPushButton("▶")
        self.next.setObjectName("plain")
        self.prev.clicked.connect(lambda: self._go(-1))
        self.next.clicked.connect(lambda: self._go(1))
        self.info = QLabel("")
        self.info.setAlignment(Qt.AlignCenter)
        nh.addStretch()
        nh.addWidget(self.prev)
        nh.addWidget(self.info)
        nh.addWidget(self.next)
        nh.addStretch()
        rv.addWidget(nav)

        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(side)
        h.addWidget(right, 1)
        if cv.notes:
            self.msg.setText("ℹ " + "\n".join(cv.notes))
        self._printer_changed()

    @staticmethod
    def field(text):
        lb = QLabel(text)
        lb.setObjectName("field")
        return lb

    # ---- state -> printer / preview
    def _info(self):
        return QPrinterInfo.printerInfo(self.printer.currentText()) if self.printer.isEnabled() else None

    def _printer_changed(self, *_):
        info = self._info()
        keep = self.paper.currentData()
        self.paper.blockSignals(True)
        self.paper.clear()
        sizes = [(ps.name(), ps) for ps in info.supportedPageSizes()] if info and not info.isNull() else []
        if not sizes:
            sizes = [(nm, QPageSize(sz)) for nm, sz in self.PAPERS]
        for nm, ps in sizes:
            self.paper.addItem(nm, ps)
        want = info.defaultPageSize().name() if info and not info.isNull() else "A4"
        for i in range(self.paper.count()):
            if self.paper.itemText(i) in (want, getattr(keep, "name", lambda: "")()):
                self.paper.setCurrentIndex(i)
                break
        self.paper.blockSignals(False)
        self._refresh()

    def _range_changed(self, i):
        self.pages_txt.setVisible(i == 2)
        self._refresh()

    def selected_pages(self):
        """0-based pages to print; ValueError if the typed range is invalid"""
        k = self.range.currentIndex()
        if k == 0:
            return list(range(self.n))
        if k == 1:
            return [self.cur]
        return parse_pages(self.pages_txt.text(), self.n)

    def _go(self, d):
        self.pos += d
        self._refresh()

    def _page_img(self, i):
        if i not in self.cache:
            pm = self.doc[i].get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
            self.cache[i] = QImage(pm.samples, pm.width, pm.height, pm.stride, QImage.Format_RGB888).copy()
        return self.cache[i]

    def _refresh(self, *_):
        try:
            pages = self.selected_pages()
            bad = False
        except ValueError:
            pages, bad = [], True
        self.pages_txt.setStyleSheet("border:2px solid #dc2626" if bad else "")
        ok = bool(pages) and self.printer.isEnabled()
        self.btn_print.setEnabled(ok)
        self.prev.setEnabled(self.pos > 0)
        self.next.setEnabled(self.pos < len(pages) - 1)
        if not pages:
            self.view.show_page(None, (1, 1), self.view.paper, True, False)
            self.info.setText("ช่วงหน้าไม่ถูกต้อง (มี %d หน้า)" % self.n if bad else "")
            return
        self.pos = max(0, min(self.pos, len(pages) - 1))
        i = pages[self.pos]
        pg = self.doc[i]
        pw, ph = pg.rect.width, pg.rect.height
        sz = self.paper.currentData().sizePoints() if self.paper.currentData() is not None else QSizeF(595, 842)
        short, long_ = sorted((sz.width(), sz.height()))
        o = self.orient.currentIndex()
        land = o == 2 or (o == 0 and pw > ph)
        paper = (long_, short) if land else (short, long_)
        self.view.show_page(self._page_img(i), (pw, ph), paper, self.scale.currentIndex() == 0, self.color.currentIndex() == 1)
        self.info.setText("หน้า %d   (แผ่นที่ %d จาก %d ที่จะพิมพ์)   %s" % (
            i + 1, self.pos + 1, len(pages), "แนวนอน" if land else "แนวตั้ง"))

    # ---- print
    def make_printer(self):
        info = self._info()
        pr = QPrinter(info, QPrinter.HighResolution) if info and not info.isNull() else QPrinter(QPrinter.HighResolution)
        ps = self.paper.currentData()
        if ps is not None:
            pr.setPageSize(ps)
        pr.setCopyCount(self.copies.value())
        pr.setCollateCopies(True)
        pr.setColorMode(QPrinter.GrayScale if self.color.currentIndex() == 1 else QPrinter.Color)
        pr.setDuplex([QPrinter.DuplexNone, QPrinter.DuplexLongSide, QPrinter.DuplexShortSide][self.duplex.currentIndex()])
        pr.setDocName("ZAIA PDF")
        return pr

    def print_now(self, printer=None):
        """printer=None -> the selected one; tests pass a PDF printer"""
        try:
            pages = self.selected_pages()
        except ValueError:
            return
        self._printer = printer or self.make_printer()  # keep a reference for the whole job
        o = ("auto", "portrait", "landscape")[self.orient.currentIndex()]
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.cv.print_pages(self._printer, self.doc, pages, self.scale.currentIndex() == 0, o, self.color.currentIndex() == 1)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            return QMessageBox.critical(self, APP_NAME, "พิมพ์ไม่สำเร็จ:\n%s: %s" % (type(e).__name__, e))
        QApplication.restoreOverrideCursor()
        self.accept()


class Main(QMainWindow):
    TOOLS = ["select", "pen", "highlight", "eraser", "text", "form"]
    LINES = [("1.0", 100), ("1.15", 115), ("1.5", 150), ("2.0", 200), ("2.5", 250), ("3.0", 300)]

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 820)
        self.setMinimumWidth(1100)
        self.setAcceptDrops(True)
        self.setStyleSheet(QSS.replace("%ARROW%", arrow_png()))
        self.cfg = QSettings("ZAIA", "ZAIA PDF")
        self.path = ""
        self.dirty = False
        self.lh = 100
        self.cv = Canvas()
        self.cv.changed.connect(lambda: setattr(self, "dirty", True))
        self.bus = Bus()
        self.bus.found.connect(self.on_found)

        # ---- left tool palette (always visible)
        tools = QFrame()
        tools.setObjectName("tools")
        tv = QVBoxLayout(tools)
        tv.setContentsMargins(8, 10, 8, 10)
        tv.setSpacing(8)
        cap = QLabel("เครื่องมือ")
        cap.setObjectName("cap")
        cap.setAlignment(Qt.AlignCenter)
        tv.addWidget(cap)
        grp = QButtonGroup(self)
        self.tool_btns = []
        for i, (k, txt, kind) in enumerate([("select", "เลือก/ย้าย", "select"), ("pen", "ปากกา", "pen"),
                                            ("highlight", "ไฮไลท์", "marker"), ("eraser", "ยางลบ", "eraser"),
                                            ("text", "ข้อความ", "text"), ("form", "กรอกฟอร์ม", "form")]):
            b = big(txt, kind, lambda _=False, k=k: self.pick(k), "Ctrl+%d" % (i + 1), True, "tool")
            grp.addButton(b)
            tv.addWidget(b)
            self.tool_btns.append(b)
        tv.addStretch()
        self.tool_btns[0].setChecked(True)
        cw = QWidget()
        h = QHBoxLayout(cw)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(tools)
        h.addWidget(self.cv, 1)
        self.setCentralWidget(cw)

        # ---- shared option widgets
        self.cb_pen = ColorDrop("สีปากกา", self.cv.pen_color)
        self.cb_txt = ColorDrop("สีข้อความ", self.cv.pen_color)
        self.cb_hl = ColorDrop("สีไฮไลท์", self.cv.hl_color)
        self.cb_pen.changed.connect(self.set_pen_color)
        self.cb_txt.changed.connect(self.set_pen_color)
        self.cb_hl.changed.connect(lambda c: setattr(self.cv, "hl_color", c))
        self.pw = NumCombo(self.cv.width, [1, 2, 3, 4, 6, 8, 12, 16, 24, 36, 50, 72, 100], 1, 100)
        self.pw.valueChanged.connect(lambda v: setattr(self.cv, "width", v))
        self.fc = QFontComboBox()
        self.fc.setFixedWidth(210)
        self.fc.setCurrentFont(QFont("Tahoma"))
        self.fc.currentFontChanged.connect(self.font_changed)
        self.fs = NumCombo(16, [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 36, 48, 60, 72, 96], 4, 400)
        self.fw = PctSlider("ความหนา", 25, 300, "ลากไปขวา = หนาขึ้น, ซ้าย = บางลง (100% = ปกติ)")
        self.fls = PctSlider("ระยะห่าง", 50, 300, "ระยะห่างระหว่างตัวอักษร (100% = ปกติ)")
        self.fx = PctSlider("บีบ–ขยาย", 50, 200, "ซ้าย = บีบให้แคบ, ขวา = ขยายให้กว้าง (100% = ปกติ)")
        self.fs.valueChanged.connect(self.font_changed)
        for c in (self.fw, self.fls, self.fx):
            c.valueChanged.connect(self.font_changed)
        self.fi = mini("I", self.font_changed, italic=True)
        self.fu = mini("U", self.font_changed, underline=True)
        self.ls_btn = QToolButton()
        self.ls_btn.setIcon(ico("lines"))
        self.ls_btn.setIconSize(QSize(30, 30))
        self.ls_btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.ls_btn.setPopupMode(QToolButton.InstantPopup)
        self.ls_btn.setToolTip("ระยะห่างระหว่างบรรทัด (เหมือน Word)")
        lm = QMenu(self.ls_btn)
        self.ls_acts = []
        for label, pct in self.LINES:
            self.ls_acts.append((lm.addAction(label, lambda p=pct: self.set_line(p)), label, pct))
        lm.addSeparator()
        lm.addAction("ระยะบรรทัดแบบกำหนดเอง…", self.line_custom)
        self.ls_btn.setMenu(lm)
        self.set_line(100, False)
        hl_sw = []
        for col in ("#ffeb3b", "#86efac", "#67e8f9", "#f9a8d4", "#fdba74"):
            b = QToolButton()
            b.setFixedSize(30, 30)
            b.setToolTip("ไฮไลท์สีนี้")
            b.setStyleSheet("QToolButton{min-width:0;min-height:0;padding:0;background:%s;border:1px solid #6b7280;"
                            "border-radius:5px}QToolButton:hover{border:2px solid #22C55E}" % col)
            b.clicked.connect(lambda _=False, c=col: (self.cb_hl.set_color(c), setattr(self.cv, "hl_color", QColor(c))))
            hl_sw.append(b)

        def lab(t):
            return QLabel(t)

        # ---- ribbon tabs
        home = QWidget()
        home.setObjectName("panel")
        hh = QHBoxLayout(home)
        hh.setContentsMargins(6, 0, 6, 0)
        hh.setSpacing(0)
        for g in (Group("ไฟล์", big("เปิด", "open", self.open_dialog, "Ctrl+O"),
                        big("บันทึก", "save", self.save_over, "Ctrl+S"),
                        big("บันทึกเป็น", "saveas", self.save_as, "Ctrl+Shift+S"),
                        big("พิมพ์", "print", self.print_pdf, "Ctrl+P")),
                  Group("แก้ไข", big("ย้อนกลับ", "undo", self.cv.do_undo, "Ctrl+Z"),
                        big("ทำซ้ำ", "redo", self.cv.do_redo, "Ctrl+Y")),
                  Group("มุมมอง", big("ย่อ", "zoomout", lambda: self.cv.set_zoom(self.cv.zoom / 1.2), "Ctrl+-"),
                        big("ขยาย", "zoomin", lambda: self.cv.set_zoom(self.cv.zoom * 1.2), "Ctrl+="),
                        big("พอดีกว้าง", "fit", self.cv.fit_width, "Ctrl+0")),
                  Group("ช่วยเหลือ", big("อัปเดต", "update", self.check_update),
                        big("เกี่ยวกับ", "info", lambda: QMessageBox.information(
                            self, APP_NAME, "%s %s\nเพิ่มปากกา/ไฮไลท์/ข้อความ/รูปภาพทับบน PDF กรอกฟอร์ม และหมุนหน้า\nไม่แก้ข้อความเดิมใน PDF" % (APP_NAME, APP_VER))))):
            hh.addWidget(g)
        hh.addStretch()

        ins = QWidget()
        ins.setObjectName("panel")
        ih = QHBoxLayout(ins)
        ih.setContentsMargins(6, 0, 6, 0)
        ih.setSpacing(0)
        ih.addWidget(Group("รูปภาพ", big("แทรกรูป", "image", self.insert_image, "Ctrl+I"),
                           rows([lab("เลือกไฟล์รูป (PNG/JPG/...) แล้วลากย้ายได้")],
                                [lab("ลากสี่เหลี่ยมเขียวมุมขวาล่างเพื่อย่อ/ขยาย")],
                                [lab("ลากไฟล์รูปมาวางบนหน้าต่างก็ได้")])))
        ih.addStretch()

        rotp = QWidget()
        rotp.setObjectName("panel")
        rh = QHBoxLayout(rotp)
        rh.setContentsMargins(6, 0, 6, 0)
        rh.setSpacing(0)
        for g in (Group("หมุนหน้าที่เลือก", big("หมุนซ้าย 90°", "rotl", lambda: self.rotate(270)),
                        big("หมุนขวา 90°", "rotr", lambda: self.rotate(90)),
                        big("หมุน 180°", "rotr", lambda: self.rotate(180))),
                  Group("หมุนทุกหน้า", big("ซ้าย 90°", "rotl", lambda: self.rotate(270, True)),
                        big("ขวา 90°", "rotr", lambda: self.rotate(90, True))),
                  Group("วิธีใช้", rows([lab("หน้าที่เลือก = หน้าที่มีกรอบเขียว")], [lab("อยู่กลางจอ (เลื่อนเพื่อเปลี่ยนหน้า)")],
                                        [lab("ของที่เพิ่มไว้บนหน้านั้นหมุนตามไปด้วย")]))):
            rh.addWidget(g)
        rh.addStretch()

        draw = QWidget()
        draw.setObjectName("panel")
        dh = QHBoxLayout(draw)
        dh.setContentsMargins(6, 0, 6, 0)
        dh.setSpacing(0)
        for g in (Group("ปากกา", self.cb_pen, rows([lab("ความหนา")], [self.pw, lab("pt")])),
                  Group("ไฮไลท์", self.cb_hl, rows([lab("สีด่วน")], hl_sw[:3], hl_sw[3:])),
                  Group("ยางลบ", rows([lab("เลือกเครื่องมือ \"ยางลบ\" แล้วคลิกหรือลาก")],
                                      [lab("ทับสิ่งที่เพิ่มไว้เพื่อลบ")], [lab("(ไม่แตะเนื้อหา PDF เดิม)")]))):
            dh.addWidget(g)
        dh.addStretch()

        txt = QWidget()
        txt.setObjectName("panel")
        th = QHBoxLayout(txt)
        th.setContentsMargins(6, 0, 6, 0)
        th.setSpacing(0)
        for g in (Group("ฟอนต์", rows([self.fc, self.fs], [self.fi, self.fu])),
                  Group("สี", self.cb_txt),
                  Group("ตัวอักษร (ลากแถบ)", rows(self.fw, self.fls, self.fx)),
                  Group("ย่อหน้า", self.ls_btn),
                  Group("กล่องข้อความ", big("กว้างอัตโนมัติ", "fit", self.reset_width),
                        rows([lab("ลากจุดเขียวที่ขอบขวา")], [lab("ของกล่อง เพื่อกำหนด")], [lab("ความกว้าง/ตัดบรรทัด")]))):
            th.addWidget(g)
        th.addStretch()

        formp = QWidget()
        formp.setObjectName("panel")
        fh = QHBoxLayout(formp)
        fh.setContentsMargins(6, 0, 6, 0)
        fh.setSpacing(0)
        self.form_lbl = lab("")
        fh.addWidget(Group("กรอกฟอร์ม", rows([self.form_lbl], [lab("คลิกช่องสีฟ้าเพื่อกรอก • ช่องติ๊ก/ปุ่มเลือก: คลิกเพื่อเปลี่ยน")],
                                              [lab("ดรอปลิสต์ (เดือน/ปี/ตัวเลือก): คลิกแล้วเลือกจากรายการ")],
                                              [lab("Enter = ตกลง • Tab = ช่องข้อความถัดไป • Esc = ยกเลิก")])))
        fh.addWidget(Group("หมายเหตุ", rows([lab("ค่าที่กรอกบันทึกลงในช่องของไฟล์ (ยังแก้ไขต่อในโปรแกรมอื่นได้)")],
                                            [lab("สูตร/สคริปต์ของฟอร์ม (เช่น คำนวณอัตโนมัติ) ไม่ถูกรัน")])))
        fh.addStretch()

        self.pages = QStackedWidget()
        self.pages.setFixedHeight(146)
        for p in (home, ins, rotp, draw, txt, formp):
            self.pages.addWidget(p)
        self.tabs = QTabBar()
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        for n in ("หน้าแรก", "แทรก", "หมุนหน้า", "วาด", "ข้อความ", "ฟอร์ม"):
            self.tabs.addTab(n)
        self.tabs.currentChanged.connect(self.pages.setCurrentIndex)
        ribbon = QWidget()
        rv = QVBoxLayout(ribbon)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        rv.addWidget(self.tabs)
        rv.addWidget(self.pages)
        self.setMenuWidget(ribbon)
        self.cv.sc.selectionChanged.connect(self.update_opts)

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
        tab = {"pen": 3, "highlight": 3, "text": 4, "form": 5}.get(k)  # ribbon follows the tool
        if tab is not None:
            self.tabs.setCurrentIndex(tab)

    def update_opts(self):
        sel = [i for i in self.cv.sc.selectedItems() if isinstance(i, ZText)]
        if len(sel) == 1:
            self.load_text_style(sel[0])
        if self.cv.tool == "select" and sel:
            self.tabs.setCurrentIndex(4)  # contextual: a text box is selected

    def reset_width(self):
        for it in self.cv.sc.selectedItems():
            if isinstance(it, ZText):
                it.set_width(-1)

    # ---- line spacing (Word style: 1.0 / 1.15 / 1.5 / 2.0 / 2.5 / 3.0 / custom)
    def set_line(self, pct, apply=True):
        self.lh = pct
        for act, label, p in self.ls_acts:
            act.setText(("✓  " if p == pct else "    ") + label)
        self.ls_btn.setText("ระยะบรรทัด\n" + dict((p, l) for _, l, p in self.ls_acts).get(pct, "%.2f" % (pct / 100)))
        if apply:
            self.font_changed()

    def line_custom(self):
        v, ok = QInputDialog.getDouble(self, "ระยะบรรทัด", "ตัวคูณ (เช่น 1.0 = ปกติ, 1.5, 2.0):",
                                       self.lh / 100, 0.5, 4.0, 2)
        if ok:
            self.set_line(int(round(v * 100)))

    # ---- font / colour
    def set_pen_color(self, c):
        self.cv.pen_color = c
        self.cb_pen.set_color(c)
        self.cb_txt.set_color(c)
        self.cv.apply_text_style()

    def font_changed(self, *_):
        f = QFont(self.fc.currentFont())
        f.setPointSize(self.fs.value())
        if self.fw.value() < 100:  # thinner = real lighter weight (needs a font that has it); thicker = faux-bold stroke
            f.setWeight(QFont.Weight(max(100, self.fw.value() * 4)))
        f.setItalic(self.fi.isChecked())
        f.setUnderline(self.fu.isChecked())
        f.setLetterSpacing(QFont.PercentageSpacing, float(self.fls.value()))  # % of glyph width: same on screen and PDF
        self.cv.font = f
        self.cv.tx_stretch, self.cv.tx_line, self.cv.tx_weight = self.fx.value(), self.lh, self.fw.value()
        self.cv.apply_text_style()

    def load_text_style(self, it):
        """show the selected text box's own style in the controls (without applying anything)"""
        f = it.font()
        self.fc.blockSignals(True)
        self.fc.setCurrentFont(f)
        self.fc.blockSignals(False)
        self.fs.setValue(max(4, f.pointSize()))
        self.fw.setValue(it.wt, False)
        self.fi.setChecked(f.italic())
        self.fu.setChecked(f.underline())
        self.fls.setValue(int(f.letterSpacing()) if f.letterSpacingType() == QFont.PercentageSpacing else 100, False)
        self.fx.setValue(it.sx, False)
        self.set_line(it.lh, False)
        col = it.defaultTextColor()
        self.cb_pen.set_color(col)
        self.cb_txt.set_color(col)
        self.cv.font, self.cv.pen_color = QFont(f), col
        self.cv.tx_stretch, self.cv.tx_line, self.cv.tx_weight = it.sx, it.lh, it.wt

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
        self._form_info()

    def _form_info(self):
        n = len(self.cv.form_items)
        self.form_lbl.setText("พบช่องฟอร์ม %d ช่อง" % n if n else "ไฟล์นี้ไม่มีช่องฟอร์ม")
        if n:
            self.pick("form")  # open straight into fill-in mode
            self.statusBar().showMessage("พบช่องฟอร์ม %d ช่อง: คลิกช่องสีฟ้าเพื่อกรอก (ใช้เครื่องมืออื่นทางซ้ายได้ตามปกติ)" % n, 10000)

    def rotate(self, deg, everything=False):
        if not self.cv.doc:
            return
        if everything:
            for i in range(len(self.cv.pages)):
                self.cv.rotate_page(i, deg)
        else:
            self.cv.rotate_page(self.cv.cur_page, deg)

    def insert_image(self, path=None):
        if not self.cv.doc:
            return QMessageBox.information(self, APP_NAME, "เปิดไฟล์ PDF ก่อน แล้วค่อยแทรกรูป")
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "เลือกรูป", "", "รูปภาพ (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff)")
        if not path:
            return
        try:
            item = self.cv.add_image(path)
        except Exception as e:
            return QMessageBox.critical(self, APP_NAME, "แทรกรูปไม่ได้:\n%s: %s" % (type(e).__name__, e))
        self.pick("select")  # ready to drag / resize
        item.setSelected(True)  # (switching tool clears the selection) keep the green resize handle visible
        self.statusBar().showMessage("แทรกรูปแล้ว: ลากย้าย • ลากสี่เหลี่ยมเขียวมุมขวาล่างเพื่อย่อ/ขยาย • Delete = ลบ", 8000)

    def _ask_form(self):
        """True = flatten form fields, False = keep them, None = cancel"""
        hits = self.cv.form_hits()
        if not hits:
            return False
        m = QMessageBox(self)
        m.setWindowTitle(APP_NAME)
        m.setIcon(QMessageBox.Question)
        m.setText("สิ่งที่เพิ่มทับ %d ช่องฟอร์ม (เช่น ปฏิทินเลือกวันที่ / ดรอปลิสต์)" % hits)
        m.setInformativeText("โปรแกรมอ่าน PDF วาดช่องฟอร์มทับบนสุดเสมอ ข้อความจะถูกบังถ้าไม่ทำฟอร์มเป็นข้อความถาวร\n\n"
                             "• ทำเป็นข้อความถาวร (แนะนำ): แสดงชัวร์ แต่ช่องกรอกในไฟล์ใหม่จะกรอกต่อไม่ได้\n"
                             "• คงช่องกรอกไว้: ยังกรอกต่อได้ แต่ที่ทับช่องอาจถูกบัง")
        b1 = m.addButton("ทำเป็นข้อความถาวร (แนะนำ)", QMessageBox.AcceptRole)
        b2 = m.addButton("คงช่องกรอกไว้", QMessageBox.DestructiveRole)
        m.addButton("ยกเลิก", QMessageBox.RejectRole)
        m.exec()
        if m.clickedButton() is b1:
            return True
        return False if m.clickedButton() is b2 else None

    def _after_save(self, f):
        self.dirty = False
        self.statusBar().showMessage("บันทึกแล้ว: " + f, 6000)
        if self.cv.notes:
            QMessageBox.information(self, APP_NAME, "บันทึกแล้ว มีข้อสังเกต:\n\n" + "\n".join(self.cv.notes))

    def save_over(self):
        """Save = overwrite the opened file (previous version kept as <name>.bak)"""
        if not self.cv.doc:
            return
        p = Path(self.path)
        m = QMessageBox(self)
        m.setWindowTitle(APP_NAME)
        m.setIcon(QMessageBox.Question)
        m.setText("บันทึกทับไฟล์เดิม?\n" + str(p))
        m.setInformativeText("ไฟล์เดิมจะถูกสำรองไว้เป็น %s.bak (ทับ .bak เก่าถ้ามี)" % p.name)
        b1 = m.addButton("บันทึกทับ", QMessageBox.AcceptRole)
        b2 = m.addButton("บันทึกเป็นไฟล์ใหม่…", QMessageBox.ActionRole)
        m.addButton("ยกเลิก", QMessageBox.RejectRole)
        m.exec()
        if m.clickedButton() is b2:
            return self.save_as(force_dialog=True)
        if m.clickedButton() is not b1:
            return
        flatten = self._ask_form()
        if flatten is None:
            return
        try:
            if p.exists():
                shutil.copy2(p, p.with_name(p.name + ".bak"))
            self.cv.save(str(p), flatten)
        except PermissionError as e:
            return QMessageBox.critical(self, APP_NAME, "เขียนไฟล์ไม่ได้ (ไฟล์อาจถูกเปิดค้างในโปรแกรมอื่น หรือไม่มีสิทธิ์):\n%s\n\nปิดไฟล์นั้นก่อน หรือใช้ \"บันทึกเป็น\"" % e)
        except Exception as e:
            return QMessageBox.critical(self, APP_NAME, "บันทึกไม่สำเร็จ:\n%s: %s" % (type(e).__name__, e))
        notes = list(self.cv.notes)
        # the saved file now contains everything: reload it so nothing is drawn twice on the next save
        try:
            data = p.read_bytes()
            doc = fitz.open("pdf", data)
            if doc.needs_pass:
                doc.authenticate(self.cv.password)
            keep = (self.cv.zoom, self.cv.verticalScrollBar().value(), self.cv.horizontalScrollBar().value())
            self.cv.set_doc(doc, data, self.cv.password, keep)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, "บันทึกแล้ว แต่โหลดไฟล์กลับมาไม่ได้: %s\nกรุณาเปิดไฟล์ใหม่" % e)
        self.cv.notes = notes
        self._after_save(str(p))

    def save_as(self, force_dialog=False):
        if not self.cv.doc:
            return
        p = Path(self.path)
        f, _ = QFileDialog.getSaveFileName(self, "บันทึกเป็น", str(p.with_name(p.stem + "_edited.pdf")), "PDF (*.pdf)")
        if not f:
            return
        if not f.lower().endswith(".pdf"):
            f += ".pdf"
        if Path(f).resolve() == p.resolve():
            return self.save_over()
        flatten = self._ask_form()
        if flatten is None:
            return
        try:
            self.cv.save(f, flatten)
        except Exception as e:
            return QMessageBox.critical(self, APP_NAME, "บันทึกไม่สำเร็จ:\n%s: %s" % (type(e).__name__, e))
        self._after_save(f)

    def print_pdf(self):
        if not self.cv.doc:
            return
        dlg = PrintDialog(self.cv, self)
        if dlg.exec() == QDialog.Accepted:
            self.statusBar().showMessage("ส่งงานพิมพ์แล้ว", 6000)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            f = u.toLocalFile()
            if f.lower().endswith(".pdf"):
                return self.open_path(f)
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")):
                return self.insert_image(f)

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


def light_theme(app):
    app.setStyle("Fusion")
    p = QPalette()
    for role, c in [(QPalette.Window, "#f3f4f6"), (QPalette.WindowText, "#1f2937"), (QPalette.Base, "#ffffff"),
                    (QPalette.AlternateBase, "#f3f4f6"), (QPalette.ToolTipBase, "#ffffff"),
                    (QPalette.ToolTipText, "#111827"), (QPalette.Text, "#111827"), (QPalette.Button, "#ffffff"),
                    (QPalette.ButtonText, "#1f2937"), (QPalette.BrightText, "#000000"),
                    (QPalette.Highlight, "#16A34A"), (QPalette.HighlightedText, "#ffffff"),
                    (QPalette.Link, "#15803d"), (QPalette.PlaceholderText, "#9ca3af")]:
        p.setColor(role, QColor(c))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor("#9ca3af"))
    app.setPalette(p)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    light_theme(app)
    for ef in (Qt.UI_AnimateCombo, Qt.UI_AnimateMenu, Qt.UI_FadeMenu, Qt.UI_FadeTooltip):
        app.setEffectEnabled(ef, True)  # smooth dropdowns

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
