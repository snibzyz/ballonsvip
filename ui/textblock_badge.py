# Number badge floating at the top-left of each TextBlkItem.
#
# Design rationale:
# - Implemented as a QGraphicsObject child of the TextBlkItem so it inherits
#   the parent's transform (rotation + position) but ItemIgnoresTransformations
#   keeps the badge size constant under zoom.
# - Click selects the parent block + scrolls the right panel to it.
# - Press-then-drag on the badge starts a drag-reorder gesture handled by
#   SceneTextManager via the badge_press / badge_drag / badge_release signals.
#   We forward scene mouse events from the badge instead of installing a scene
#   filter so existing event flow on other items stays unaffected.

from qtpy.QtWidgets import QGraphicsItem, QGraphicsObject, QStyleOptionGraphicsItem, QWidget, QGraphicsSceneMouseEvent, QGraphicsSceneHoverEvent, QGraphicsSceneContextMenuEvent, QMenu, QLineEdit
from qtpy.QtCore import Qt, QRectF, QPointF, Signal, QEvent
from qtpy.QtGui import QPainter, QColor, QPen, QBrush, QFont, QIntValidator, QKeySequence, QFocusEvent, QKeyEvent


# Visual constants. Kept module-level so styling is easy to tweak.
BADGE_W = 22.0
BADGE_H = 18.0
BADGE_RADIUS = 5.0
BADGE_OFFSET_X = -4.0
BADGE_OFFSET_Y = -4.0
BADGE_BG = QColor(0, 0, 0, 191)            # ~0.75 opacity black
BADGE_FG = QColor(255, 255, 255, 255)
BADGE_BORDER_DEFAULT = QColor(255, 255, 255, 80)
BADGE_BORDER_HIGHLIGHT = QColor(255, 200, 0, 230)   # drop-target highlight
BADGE_BORDER_DRAGSRC = QColor(255, 80, 80, 230)     # source while dragging

# Drag distance threshold in pixels before a press becomes a drag.
DRAG_START_DISTANCE = 4.0

# Painter-state singletons. paint() runs once per badge per frame -- across
# 100+ blocks that's a hot path, and rebuilding QBrush/QPen/QFont each call
# was a measurable allocation hit on profile traces. Module-level instances
# are read-only after import; QPainter copies state internally.
_BG_BRUSH = QBrush(BADGE_BG)
_FG_PEN = QPen(BADGE_FG)
_BORDER_PEN_DEFAULT = QPen(BADGE_BORDER_DEFAULT, 1.0)
_BORDER_PEN_HIGHLIGHT = QPen(BADGE_BORDER_HIGHLIGHT, 2.0)
_BORDER_PEN_DRAGSRC = QPen(BADGE_BORDER_DRAGSRC, 2.0)
_BADGE_FONT = QFont()
_BADGE_FONT.setBold(True)
_BADGE_FONT.setPointSize(9)
_BADGE_RECT = QRectF(0, 0, BADGE_W, BADGE_H)


class TextBlockNumberBadge(QGraphicsObject):
    # Emitted with the parent block's idx when the user clicks (no drag) the
    # badge. SceneTextManager uses this to select+scroll the list.
    badge_clicked = Signal(int)
    # Emitted with idx when a drag gesture starts (after press + threshold).
    badge_drag_started = Signal(int)
    # Emitted with current scene pos (QPointF) on every move while dragging.
    badge_dragging = Signal(QPointF)
    # Emitted with scene release pos when the mouse is released; no drop target
    # info because the manager owns target detection (it knows all badges).
    badge_drag_ended = Signal(QPointF)
    # Context menu / keyboard reorder requests. Manager owns the actual move so
    # the badge stays free of textblk_item_list awareness.
    # (src_idx, target_idx) -- target_idx is 0-based, manager validates bounds.
    move_to_position_requested = Signal(int, int)
    # Request manager to spawn the Quick Reorder popup anchored at this badge.
    # Carries this badge's idx so the manager can find the screen anchor.
    quick_reorder_requested = Signal(int)
    # Trigger manager.auto_sort_reading_order(); same effect as Ctrl+Shift+R.
    auto_sort_requested = Signal()
    # Toggle pcfg.show_textblock_number; matches the N shortcut behaviour.
    toggle_numbers_requested = Signal()

    def __init__(self, blk_item, idx: int = 0):
        # Parent the badge to the block so it follows position/rotation.
        super().__init__(blk_item)
        self._idx = idx
        self._highlight = 0  # 0=none, 1=drop-target, 2=drag-source
        self._press_scene_pos: QPointF = None
        self._dragging = False

        # ItemIgnoresTransformations keeps the badge a constant on-screen size
        # regardless of view zoom. Without it the badge becomes unreadable at
        # low zooms and a giant overlay at high zooms.
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        # ZValue 0.5: above the text item (default 0) but below the shape
        # control widget (which sets ZValue 1 in show()).
        self.setZValue(0.5)
        self.setAcceptHoverEvents(True)
        # Position is set in updatePosition; stays at the parent's local origin
        # offset for now so a not-yet-positioned badge still draws sensibly.
        self.updatePosition()

    def set_idx(self, idx: int):
        # Update visible number. Called when idx changes (reorder, page swap).
        if self._idx != idx:
            self._idx = idx
            self.update()

    @property
    def idx(self) -> int:
        return self._idx

    def updatePosition(self):
        # Pin the badge to the parent's top-left, then push slightly outward
        # so it floats above the bounding rect rather than overlapping text.
        parent: QGraphicsItem = self.parentItem()
        if parent is None:
            return
        pr = parent.boundingRect()
        # parent.boundingRect() returns padded rect for TextBlkItem; the unpadded
        # top-left is at (padding, padding). We want the badge anchored to the
        # padded outer top-left with a small outward offset for visual breathing.
        self.setPos(pr.x() + BADGE_OFFSET_X, pr.y() + BADGE_OFFSET_Y)

    def set_highlight(self, mode: int):
        # 0 = no border accent, 1 = drop-target, 2 = currently-being-dragged.
        if self._highlight != mode:
            self._highlight = mode
            self.update()

    def boundingRect(self) -> QRectF:
        # Add a small margin so the (possibly thicker) highlight border isn't
        # clipped against the boundingRect on repaint.
        return QRectF(-1, -1, BADGE_W + 2, BADGE_H + 2)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget = None) -> None:
        # Hot path: runs per badge per repaint. Reuse module-level QBrush/QPen/
        # QFont/QRectF to avoid per-frame allocations. setBrush/setPen copy
        # state internally so sharing the source object is safe.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Background pill
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_BG_BRUSH)
        painter.drawRoundedRect(_BADGE_RECT, BADGE_RADIUS, BADGE_RADIUS)

        # Border: subtle by default, accented when interacting.
        if self._highlight == 1:
            border_pen = _BORDER_PEN_HIGHLIGHT
        elif self._highlight == 2:
            border_pen = _BORDER_PEN_DRAGSRC
        else:
            border_pen = _BORDER_PEN_DEFAULT
        painter.setPen(border_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(_BADGE_RECT, BADGE_RADIUS, BADGE_RADIUS)

        # Number label (1-based for human readability).
        painter.setFont(_BADGE_FONT)
        painter.setPen(_FG_PEN)
        painter.drawText(_BADGE_RECT, Qt.AlignmentFlag.AlignCenter, str(self._idx + 1))

    def hoverEnterEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        # OpenHand cursor + tooltip hints at draggability without a click.
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to reorder")
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        self.unsetCursor()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        # Track the press in scene coords so we can decide click-vs-drag based
        # on movement distance during mouseMoveEvent.
        self._press_scene_pos = event.scenePos()
        self._dragging = False
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        # Accept so the scene routes mouseMove/Release here instead of falling
        # through to the parent TextBlkItem (which would start a move drag).
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._press_scene_pos is None:
            super().mouseMoveEvent(event)
            return
        delta = event.scenePos() - self._press_scene_pos
        if not self._dragging:
            if (abs(delta.x()) + abs(delta.y())) >= DRAG_START_DISTANCE:
                self._dragging = True
                self.badge_drag_started.emit(self._idx)
        if self._dragging:
            self.badge_dragging.emit(event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        was_dragging = self._dragging
        self._dragging = False
        self._press_scene_pos = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if was_dragging:
            self.badge_drag_ended.emit(event.scenePos())
        else:
            # Treat as a click: select the underlying block.
            self.badge_clicked.emit(self._idx)
        event.accept()

    def cancel(self):
        # Hard-reset all interaction state. Used by the manager when:
        #   - ESC is pressed mid-drag (cursor was ClosedHand, _dragging True)
        #   - Page change happens mid-drag (the badge may be about to be pooled
        #     and the scene-level mouse grab must be released first; otherwise
        #     the eventual mouseRelease lands on a detached item).
        self._dragging = False
        self._press_scene_pos = None
        self.set_highlight(0)
        # unsetCursor reverts to the parent's cursor (or Arrow at scene level).
        # This is the fix for the "stuck closed-hand" bug after ESC during drag.
        self.unsetCursor()
        # Some Qt builds raise on ungrabMouse when nothing is grabbed; swallow
        # that defensively so cancel() is always safe to call.
        try:
            self.ungrabMouse()
        except Exception:
            pass

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        # Right-click on a badge surfaces quick-reorder operations without
        # requiring drag precision (the original pain point with 40+ badges).
        # All actions delegate to the manager via signals so the badge stays
        # free of textblk_item_list awareness.
        menu = QMenu()
        # Header showing which block this menu acts on (1-based for users).
        header = menu.addAction(self.tr("Block #{0}").format(self._idx + 1))
        header.setEnabled(False)
        menu.addSeparator()

        move_top_act = menu.addAction(self.tr("Move to top"))
        move_top_act.setShortcut(QKeySequence("Alt+Home"))
        move_up_act = menu.addAction(self.tr("Move up"))
        move_up_act.setShortcut(QKeySequence("Alt+Up"))
        move_down_act = menu.addAction(self.tr("Move down"))
        move_down_act.setShortcut(QKeySequence("Alt+Down"))
        move_bottom_act = menu.addAction(self.tr("Move to bottom"))
        move_bottom_act.setShortcut(QKeySequence("Alt+End"))
        menu.addSeparator()
        # The "..." suffix is the Qt convention for actions that prompt for input.
        # Hint at the keyboard shortcut so users discover Ctrl+J -- note: the
        # shortcut text is purely informational; the action itself fires the
        # signal we handle below.
        move_to_act = menu.addAction(self.tr("Move to position..."))
        move_to_act.setShortcut(QKeySequence("Ctrl+J"))
        menu.addSeparator()
        auto_sort_act = menu.addAction(self.tr("Auto-sort reading order"))
        auto_sort_act.setShortcut(QKeySequence("Ctrl+Shift+R"))
        hide_numbers_act = menu.addAction(self.tr("Hide block numbers"))
        hide_numbers_act.setShortcut(QKeySequence("N"))

        # Use screenPos so the menu pops at the cursor regardless of viewport
        # transforms. event.screenPos() is QPoint in global screen coordinates.
        rst = menu.exec(event.screenPos())
        if rst is None:
            event.accept()
            return

        # Compute target indices using the badge's current idx. Bounds are
        # clamped here for clean semantics -- the manager re-validates so
        # stale/out-of-range requests are still safe.
        idx = self._idx
        if rst is move_top_act:
            self.move_to_position_requested.emit(idx, 0)
        elif rst is move_up_act:
            # max(0, idx-1); manager no-ops when src == target.
            self.move_to_position_requested.emit(idx, idx - 1 if idx > 0 else 0)
        elif rst is move_down_act:
            # The manager clamps against current list length; passing idx+1 is
            # always safe -- if it overflows we just become a no-op.
            self.move_to_position_requested.emit(idx, idx + 1)
        elif rst is move_bottom_act:
            # Use a sentinel large index; manager clamps to len-1. This avoids
            # querying list length here (which the badge intentionally doesn't
            # know about).
            self.move_to_position_requested.emit(idx, 10 ** 9)
        elif rst is move_to_act:
            self.quick_reorder_requested.emit(idx)
        elif rst is auto_sort_act:
            self.auto_sort_requested.emit()
        elif rst is hide_numbers_act:
            self.toggle_numbers_requested.emit()
        event.accept()


# Width chosen to fit 4 digits + small padding. 28px tall matches Qt's default
# QLineEdit baseline so it does not look out-of-place over the canvas.
QUICK_REORDER_W = 60
QUICK_REORDER_H = 28


class QuickReorderInputPopup(QLineEdit):
    # Mini overlay that lets the user type a 1-based target index and press
    # Enter to reorder the currently selected block. Lives as a child of the
    # canvas viewport so it floats above the scene without participating in
    # the QGraphicsScene event flow (avoids fighting badge mouse handlers).
    #
    # Contract:
    #   - submitted(int)        -- emitted with the 0-based target index when
    #                              the user presses Enter on a valid value.
    #   - cancelled()           -- emitted when ESC, focus loss, or invalid
    #                              submit happens. Manager closes / cleans up.
    submitted = Signal(int)
    cancelled = Signal()

    def __init__(self, parent: QWidget, current_idx_0based: int, max_idx_1based: int):
        super().__init__(parent)
        self._current_idx_0based = current_idx_0based
        self._max_idx_1based = max(1, max_idx_1based)
        # QIntValidator is the first line of defense; we still re-check on
        # submit because the user may submit an empty field.
        self.setValidator(QIntValidator(1, self._max_idx_1based, self))
        self.setMaxLength(len(str(self._max_idx_1based)) + 1)
        self.setFixedSize(QUICK_REORDER_W, QUICK_REORDER_H)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Hard-coded dark style: the canvas background varies (light/dark mode,
        # image content) so we pin a high-contrast pill to stay readable.
        self.setStyleSheet(
            "QLineEdit {"
            " background-color: rgba(20, 20, 20, 240);"
            " color: white;"
            " border: 1px solid rgba(255, 200, 0, 230);"
            " border-radius: 4px;"
            " padding: 2px 4px;"
            "}"
        )
        # Pre-fill with the block's current 1-based position so the user sees
        # context, then selectAll so typing immediately overwrites.
        self.setText(str(current_idx_0based + 1))
        self.selectAll()
        self.returnPressed.connect(self._on_submit)

    def _on_submit(self):
        text = self.text().strip()
        if not text:
            self.cancelled.emit()
            return
        try:
            target_1based = int(text)
        except ValueError:
            self.cancelled.emit()
            return
        if target_1based < 1 or target_1based > self._max_idx_1based:
            self.cancelled.emit()
            return
        target_0based = target_1based - 1
        if target_0based == self._current_idx_0based:
            # No-op move; treat as cancel so manager doesn't push an empty
            # undo command (manager already short-circuits this case but we
            # still want to close the popup cleanly).
            self.cancelled.emit()
            return
        self.submitted.emit(target_0based)

    def event(self, e):
        # MainWindow registers single-key QShortcuts (A, D, W, N, Space, etc.)
        # at WindowShortcut scope. Qt dispatches a ShortcutOverride event to the
        # focused widget BEFORE activating the shortcut; if we accept it here,
        # the shortcut never fires and the keystroke flows into our QLineEdit
        # (where QIntValidator filters non-digits). Without this, typing "d" in
        # the popup would trigger the next-page shortcut and dismiss the popup.
        if e.type() == QEvent.Type.ShortcutOverride:
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # ESC cancels without submitting. We handle it explicitly here because
        # the application-wide Ctrl+Escape handler also calls
        # st_manager.cancel_badge_drag(); but the popup's own ESC handling
        # closes the popup more directly than relying on focus-out, which
        # racy-fires on alt-tab between windows.
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.cancelled.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        # Click outside / tab away => treat as cancel. We don't auto-submit on
        # focus loss because the user may have only partially typed a number.
        super().focusOutEvent(event)
        self.cancelled.emit()
