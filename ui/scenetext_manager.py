
from typing import List, Union, Tuple
import numpy as np
import copy
import time

from qtpy.QtWidgets import QApplication, QWidget, QGraphicsItem
from qtpy.QtCore import QObject, QRectF, Qt, Signal, QPointF, QPoint
from qtpy.QtGui import QKeyEvent, QTextCursor, QFontMetricsF, QFont, QTextCharFormat, QClipboard
try:
    from qtpy.QtWidgets import QUndoCommand
except:
    from qtpy.QtGui import QUndoCommand

from .textitem import TextBlkItem, TextBlock
from .canvas import Canvas
from .textedit_area import TransTextEdit, SourceTextEdit, TransPairWidget, SelectTextMiniMenu, TextEditListScrollArea, QVBoxLayout, Widget
from utils.fontformat import FontFormat
from .textedit_commands import propagate_user_edit, TextEditCommand, ReshapeItemCommand, MoveBlkItemsCommand, AutoLayoutCommand, ApplyFontformatCommand, RotateItemCommand, TextItemEditCommand, TextEditCommand, PageReplaceOneCommand, PageReplaceAllCommand, MultiPasteCommand, ResetAngleCommand, SqueezeCommand
from .text_panel import FontFormatPanel
from utils.config import pcfg
from utils import shared
from utils.imgproc_utils import extract_ballon_region, rotate_polygons, get_block_mask
from utils.text_processing import seg_text, is_cjk
from utils.text_layout import layout_text
from .misc import apply_fontformat_to_html

class CreateItemCommand(QUndoCommand):
    def __init__(self, blk_item: TextBlkItem, ctrl, parent=None):
        super().__init__(parent)
        self.blk_item = blk_item
        self.ctrl: SceneTextManager = ctrl
        self.op_count = -1
        self.ctrl.addTextBlock(self.blk_item)
        self.pairw = self.ctrl.pairwidget_list[self.blk_item.idx]
        self.ctrl.txtblkShapeControl.setBlkItem(self.blk_item)

    def redo(self):
        if self.op_count < 0:
            self.op_count += 1
            self.blk_item.setSelected(True)
            return
        self.ctrl.recoverTextblkItemList([self.blk_item], [self.pairw])

    def undo(self):
        self.ctrl.deleteTextblkItemList([self.blk_item], [self.pairw])

class EmptyCommand(QUndoCommand):
    def __init__(self, parent=None):
        super().__init__(parent=parent)

class DeleteBlkItemsCommand(QUndoCommand):
    def __init__(self, blk_list: List[TextBlkItem], mode: int, ctrl, parent=None):
        super().__init__(parent)
        self.op_counter = 0
        self.blk_list = []
        self.pwidget_list: List[TransPairWidget] = []
        self.ctrl: SceneTextManager = ctrl
        self.sw = self.ctrl.canvas.search_widget
        self.canvas: Canvas = ctrl.canvas
        self.mode = mode

        self.undo_img_list = []
        self.redo_img_list = []
        self.inpaint_rect_lst = []
        self.mask_pnts = []
        img_array = self.canvas.imgtrans_proj.inpainted_array
        mask_array = self.canvas.imgtrans_proj.mask_array
        original_array = self.canvas.imgtrans_proj.img_array

        self.search_rstedit_list: List[SourceTextEdit] = []
        self.search_counter_list = []
        self.highlighter_list = []
        self.old_counter_sum = self.sw.counter_sum
        self.sw_changed = False

        blk_list.sort(key=lambda blk: blk.idx)
        
        for blkitem in blk_list:
            if not isinstance(blkitem, TextBlkItem):
                continue
            self.blk_list.append(blkitem)
            pw: TransPairWidget = ctrl.pairwidget_list[blkitem.idx]
            self.pwidget_list.append(pw)

            if mode == 1:
                is_empty = False
                msk, xyxy = get_block_mask(blkitem.absBoundingRect(), mask_array, blkitem.rotation())
                if msk is None:
                    is_empty = True
                if is_empty:
                    self.undo_img_list.append(None)
                    self.redo_img_list.append(None)
                    self.inpaint_rect_lst.append(None)
                    self.mask_pnts.append(None)
                else:
                    x1, y1, x2, y2 = xyxy
                    self.mask_pnts.append(np.where(msk))
                    self.undo_img_list.append(np.copy(img_array[y1: y2, x1: x2]))
                    self.redo_img_list.append(np.copy(original_array[y1: y2, x1: x2]))
                    self.inpaint_rect_lst.append([x1, y1, x2, y2])

            rst_idx = self.sw.get_result_edit_index(pw.e_trans)
            if rst_idx != -1:
                self.sw_changed = True
                highlighter = self.sw.highlighter_list.pop(rst_idx)
                counter = self.sw.search_counter_list.pop(rst_idx)
                self.sw.counter_sum -= counter
                if self.sw.current_edit == pw.e_trans:
                    highlighter.set_current_span(-1, -1)
                self.search_rstedit_list.append(self.sw.search_rstedit_list.pop(rst_idx))
                self.search_counter_list.append(counter)
                self.highlighter_list.append(highlighter)

            rst_idx = self.sw.get_result_edit_index(pw.e_source)
            if rst_idx != -1:
                self.sw_changed = True
                highlighter = self.sw.highlighter_list.pop(rst_idx)
                counter = self.sw.search_counter_list.pop(rst_idx)
                self.sw.counter_sum -= counter
                if self.sw.current_edit == pw.e_trans:
                    highlighter.set_current_span(-1, -1)
                self.search_rstedit_list.append(self.sw.search_rstedit_list.pop(rst_idx))
                self.search_counter_list.append(counter)
                self.highlighter_list.append(highlighter)

        self.new_counter_sum = self.sw.counter_sum
        if self.sw_changed:
            if self.sw.counter_sum > 0:
                idx = self.sw.get_result_edit_index(self.sw.current_edit)
                if self.sw.current_cursor is not None and idx != -1:
                    self.sw.result_pos = self.sw.highlighter_list[idx].matched_map[self.sw.current_cursor.position()]
                    if idx > 0:
                        self.sw.result_pos += sum(self.sw.search_counter_list[: idx])
                    self.sw.updateCounterText()
                else:
                    self.sw.setCurrentEditor(self.sw.search_rstedit_list[0])
            else:
                self.sw.setCurrentEditor(None)

        self.ctrl.deleteTextblkItemList(self.blk_list, self.pwidget_list)

    def redo(self):

        if self.mode == 1:
            self.canvas.saved_drawundo_step -= 1
            img_array = self.canvas.imgtrans_proj.inpainted_array
            mask_array = self.canvas.imgtrans_proj.mask_array
            for mskpnt, inpaint_rect, redo_img in zip(self.mask_pnts, self.inpaint_rect_lst, self.redo_img_list):
                if mskpnt == None:
                    continue
                x1, y1, x2, y2 = inpaint_rect
                img_array[y1: y2, x1: x2][mskpnt] = redo_img[mskpnt]
                mask_array[y1: y2, x1: x2][mskpnt] = 0
            self.canvas.updateLayers()

        if self.op_counter == 0:
            self.op_counter += 1
            return

        self.ctrl.deleteTextblkItemList(self.blk_list, self.pwidget_list)
        if self.sw_changed:
            self.sw.counter_sum = self.new_counter_sum
            cursor_removed = False
            for edit in self.search_rstedit_list:
                idx = self.sw.get_result_edit_index(edit)
                if idx != -1:
                    self.sw.search_rstedit_list.pop(idx)
                    self.sw.search_counter_list.pop(idx)
                    self.sw.highlighter_list.pop(idx)
                if edit == self.sw.current_edit:
                    cursor_removed = True
            if cursor_removed:
                if self.sw.counter_sum > 0:
                    self.sw.setCurrentEditor(self.sw.search_rstedit_list[0])
                else:
                    self.sw.setCurrentEditor(None)

    def undo(self):

        if self.mode == 1:
            self.canvas.saved_drawundo_step += 1
            img_array = self.canvas.imgtrans_proj.inpainted_array
            mask_array = self.canvas.imgtrans_proj.mask_array
            for mskpnt, inpaint_rect, undo_img in zip(self.mask_pnts, self.inpaint_rect_lst, self.undo_img_list):
                if mskpnt == None:
                    continue
                x1, y1, x2, y2 = inpaint_rect
                img_array[y1: y2, x1: x2][mskpnt] = undo_img[mskpnt]
                mask_array[y1: y2, x1: x2][mskpnt] = 255
            self.canvas.updateLayers()

        self.ctrl.recoverTextblkItemList(self.blk_list, self.pwidget_list)
        if self.sw_changed:
            self.sw.counter_sum = self.old_counter_sum
            self.sw.search_rstedit_list += self.search_rstedit_list
            self.sw.search_counter_list += self.search_counter_list
            self.sw.highlighter_list += self.highlighter_list
            self.sw.updateCounterText()

class PasteBlkItemsCommand(QUndoCommand):
    def __init__(self, blk_list: List[TextBlkItem], pwidget_list: List[TransPairWidget], ctrl, parent=None):
        super().__init__(parent)
        self.op_counter = 0
        self.blk_list = blk_list
        self.ctrl:SceneTextManager = ctrl
        blk_list.sort(key=lambda blk: blk.idx)

        self.ctrl.canvas.block_selection_signal = True
        for blkitem in blk_list:
            blkitem.setSelected(True)
        self.ctrl.on_incanvas_selection_changed()
        self.ctrl.canvas.block_selection_signal = False
        self.pwidget_list = pwidget_list
        

    def redo(self):
        if self.op_counter == 0:
            self.op_counter += 1
            return
        self.ctrl.recoverTextblkItemList(self.blk_list, self.pwidget_list)

    def undo(self):
        self.ctrl.deleteTextblkItemList(self.blk_list, self.pwidget_list)

class PasteSrcItemsCommand(QUndoCommand):
    def __init__(self, src_list: List[SourceTextEdit], paste_list: List[str]):
        super().__init__()
        self.src_list = src_list
        self.paste_list = paste_list
        self.ori_text_list = [src.toPlainText() for src in src_list]

    def redo(self):
        for src, text in zip(self.src_list, self.paste_list):
            src.setPlainText(text)

    def undo(self):
        for src, text in zip(self.src_list, self.ori_text_list):
            src.setPlainText(text)

class RearrangeBlksCommand(QUndoCommand):

    def __init__(self, rmap: Tuple, ctrl, parent=None):
        super().__init__(parent)
        self.ctrl: SceneTextManager = ctrl
        self.src_ids, self.tgt_ids = rmap[0], rmap[1]

        self.nr = len(self.src_ids)
        self.src2tgt = {}
        self.tgt2src = {}
        for s, t in zip(self.src_ids, self.tgt_ids):
            self.src2tgt[s] = t
            self.tgt2src[t] = s
        self.visible_ = None
        self.redo_visible_idx = self.undo_visible_idx = None
        if len(rmap) > 2:
            self.redo_visible_idx, self.undo_visible_idx = rmap[2]

    def redo(self):
        self.rearange_blk_ids(self.src_ids, self.tgt_ids, self.redo_visible_idx)

    def undo(self):
        self.rearange_blk_ids(self.tgt_ids, self.src_ids, self.undo_visible_idx)

    def rearange_blk_ids(self, src_ids, tgt_ids, visible_idx = None):
        src_ids = np.array(src_ids)
        tgt_ids = np.array(tgt_ids)
        if src_ids.size == 0:
            # Nothing to move. Avoids the `pw.height()` reference at the
            # bottom (pw is loop-local) when the redo/undo replays an
            # empty permutation -- previously raised NameError.
            return
        src_order_ids = np.argsort(src_ids)[::-1]

        src_ids = src_ids[src_order_ids]
        tgt_ids = tgt_ids[src_order_ids]

        blks: List[TextBlkItem] = []
        pws: List[TransPairWidget] = []
        last_pw = None
        for pos, pos_tgt in zip(src_ids, tgt_ids):
            pw = self.ctrl.pairwidget_list.pop(pos)
            last_pw = pw
            if visible_idx == pos_tgt:
                pw.hide()
            blk = self.ctrl.textblk_item_list.pop(pos)
            pws.append(pw)
            blks.append(blk)

        tgt_order_ids = np.argsort(tgt_ids)
        for ii in tgt_order_ids:
            pos = tgt_ids[ii]
            self.ctrl.textblk_item_list.insert(pos, blks[ii])

            self.ctrl.textEditList.insertPairWidget(pws[ii], pos)
            self.ctrl.pairwidget_list.insert(pos, pws[ii])

        self.ctrl.updateTextBlkItemIdx(set(int(t) for t in tgt_ids))
        if visible_idx is not None:
            # Bounds check: callers (textEditList drag flow) compute visible_idx
            # before the rearrange, so a deletion in flight could leave it
            # pointing past the end. Skip silently rather than IndexError.
            if 0 <= int(visible_idx) < len(self.ctrl.pairwidget_list):
                pw_ct = self.ctrl.pairwidget_list[int(visible_idx)]
                pw_ct.show()
                anchor_h = last_pw.height() if last_pw is not None else pw_ct.height()
                self.ctrl.textEditList.ensureWidgetVisible(pw_ct, yMargin=anchor_h)

class TextPanel(Widget):
    def __init__(self, app: QApplication, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        layout = QVBoxLayout(self)
        self.textEditList = TextEditListScrollArea(self)
        self.formatpanel = FontFormatPanel(app, self)
        layout.addWidget(self.formatpanel)
        layout.addWidget(self.textEditList)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

class SceneTextManager(QObject):
    new_textblk = Signal(int)
    def __init__(self, 
                 app: QApplication,
                 mainwindow: QWidget,
                 canvas: Canvas, 
                 textpanel: TextPanel, 
                 *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.app = app     
        self.mainwindow = mainwindow
        self.canvas = canvas
        canvas.switch_text_item.connect(self.on_switch_textitem)
        self.selectext_minimenu: SelectTextMiniMenu = None
        self.canvas.scalefactor_changed.connect(self.adjustSceneTextRect)
        self.canvas.end_create_textblock.connect(self.onEndCreateTextBlock)
        self.canvas.paste2selected_textitems.connect(self.on_paste2selected_textitems)
        self.canvas.delete_textblks.connect(self.onDeleteBlkItems)
        self.canvas.copy_textblks.connect(self.onCopyBlkItems)
        self.canvas.paste_textblks.connect(self.onPasteBlkItems)
        self.canvas.format_textblks.connect(self.onFormatTextblks)
        self.canvas.layout_textblks.connect(self.onAutoLayoutTextblks)
        self.canvas.reset_angle.connect(self.onResetAngle)
        self.canvas.squeeze_blk.connect(self.onSqueezeBlk)
        # Reorder context-menu actions reuse the same handlers as the keyboard
        # shortcuts so menu / keyboard / drag stay consistent. Resolution of
        # "which block to move" happens in onCanvasReorderRequested via the
        # standard selection -> shape control fallback chain.
        self.canvas.reorder_to_top.connect(lambda: self.onCanvasReorderRequested('top'))
        self.canvas.reorder_up.connect(lambda: self.onCanvasReorderRequested('up'))
        self.canvas.reorder_down.connect(lambda: self.onCanvasReorderRequested('down'))
        self.canvas.reorder_to_bottom.connect(lambda: self.onCanvasReorderRequested('bottom'))
        self.canvas.reorder_to_position.connect(lambda: self.onCanvasReorderRequested('position'))
        self.canvas.reorder_auto_sort.connect(lambda: self.onCanvasReorderRequested('auto_sort'))
        self.canvas.incanvas_selection_changed.connect(self.on_incanvas_selection_changed)
        self.txtblkShapeControl = canvas.txtblkShapeControl
        self.textpanel = textpanel
        self.textEditList = textpanel.textEditList
        self.textEditList.focus_out.connect(self.on_textedit_list_focusout)
        self.textEditList.textpanel_contextmenu_requested.connect(canvas.on_create_contextmenu)
        self.textEditList.selection_changed.connect(self.on_transwidget_selection_changed)
        self.textEditList.rearrange_blks.connect(self.on_rearrange_blks)
        self.formatpanel = textpanel.formatpanel
        self.formatpanel.textstyle_panel.apply_fontfmt.connect(self.onFormatTextblks)
        self.formatpanel.apply_font_to_all_pages.connect(self.onApplyFontToAllPages)

        self.imgtrans_proj = self.canvas.imgtrans_proj
        self.textblk_item_list: List[TextBlkItem] = []
        self.pairwidget_list: List[TransPairWidget] = self.textEditList.pairwidget_list

        self.auto_textlayout_flag = False
        self.hovering_transwidget : TransTextEdit = None

        self.prev_blkitem: TextBlkItem = None

        # Object pool for fast page switching: TextBlkItem and TransPairWidget are
        # expensive to construct (signal wiring + font/document setup), so pool the
        # excess instead of destroying and rebuilding every page change.
        # Capped to avoid retaining huge buffers on multi-page projects with one
        # outlier page that has hundreds of blocks.
        self._blk_pool: List[TextBlkItem] = []
        self._pw_pool: List[TransPairWidget] = []
        self._pool_max_size: int = 100

        # Badge drag-reorder state. None when no drag in progress; populated by
        # onBadgeDragStarted, mutated by onBadgeDragging, cleared on end/cancel.
        self._badge_drag_src = None
        self._badge_drag_target = None

        # Quick-reorder popup (Ctrl+J / "Move to position..." context action).
        # Only ever one popup at a time; spawn replaces any prior popup.
        self._quick_reorder_popup = None
        self._quick_reorder_src_idx = None

    def on_switch_textitem(self, switch_delta: int, key_event: QKeyEvent = None, current_editing_widget: Union[SourceTextEdit, TransTextEdit] = None):
        n_blk = len(self.textblk_item_list)
        if n_blk < 1:
            return
        
        editing_blk = None
        if current_editing_widget is None:
            editing_blk = self.editingTextItem()
            if editing_blk is not None:
                tgt_idx = editing_blk.idx + switch_delta
            else:
                sel_blks = self.canvas.selected_text_items(sort=False)
                if len(sel_blks) == 0:
                    return
                sel_blk = sel_blks[0]
                tgt_idx = sel_blk.idx + switch_delta
        else:
            tgt_idx = current_editing_widget.idx + switch_delta

        if tgt_idx < 0:
            tgt_idx += n_blk
        elif tgt_idx >= n_blk:
            tgt_idx -= n_blk
        blk = self._safe_blk_item(tgt_idx)
        if blk is None:
            return

        if current_editing_widget is None:
            if editing_blk is None:
                self.canvas.block_selection_signal = True
                self.canvas.clearSelection()
                blk.setSelected(True)
                self.canvas.block_selection_signal = False
                self.canvas.gv.ensureVisible(blk)
                self.txtblkShapeControl.setBlkItem(blk)
                pw = self._safe_pair_widget(tgt_idx)
                if pw is not None:
                    self.changeHoveringWidget(pw.e_trans)
                self.textEditList.set_selected_list([blk.idx])
            else:
                editing_blk.endEdit()
                editing_blk.setSelected(False)
                self.txtblkShapeControl.setBlkItem(blk)
                blk.setSelected(True)
                blk.startEdit()
                self.canvas.gv.ensureVisible(blk)
        else:
            cur_blk = self._safe_blk_item(getattr(current_editing_widget, 'idx', -1))
            if cur_blk is not None:
                cur_blk.setSelected(False)
            current_pw = self._safe_pair_widget(tgt_idx)
            if current_pw is None:
                return
            is_trans = isinstance(current_editing_widget, TransTextEdit)
            if is_trans:
                w = current_pw.e_trans
            else:
                w = current_pw.e_source

            self.changeHoveringWidget(w)
            w.setFocus()

        if key_event is not None:
            key_event.accept()

    def setTextEditMode(self, edit: bool = False):
        if edit:
            self.textpanel.show()
            self.canvas.textLayer.show()
        else:
            self.txtblkShapeControl.setBlkItem(None)
            self.textpanel.hide()
            self.textpanel.formatpanel.set_textblk_item()
            self.canvas.textLayer.hide()

    def adjustSceneTextRect(self):
        self.txtblkShapeControl.updateBoundingRect()

    def _pool_release_blk_item(self, blkitem: TextBlkItem):
        # Hide and detach the item from the scene without destroying it. Signal
        # connections established in addTextBlkItem stay live so reuse is safe.
        # Avoid firing end_edit / selectionChanged during teardown -- those slots
        # rely on textblk_item_list[blk_id] which is in flux during the swap.
        #
        # NOTE: blkitem.blockSignals only suppresses signals emitted by the item
        # itself. QGraphicsScene.selectionChanged fires from the scene when
        # setSelected(False) is called below, so we also raise
        # canvas.block_selection_signal to keep on_selection_changed from
        # cascading into on_incanvas_selection_changed during the swap (which
        # would touch a half-cleared textblk_item_list).
        if self.canvas.editing_textblkitem is blkitem:
            self.canvas.editing_textblkitem = None
        was_blocked = blkitem.signalsBlocked()
        prev_block_sel = self.canvas.block_selection_signal
        blkitem.blockSignals(True)
        self.canvas.block_selection_signal = True
        try:
            if blkitem.isSelected():
                blkitem.setSelected(False)
            if blkitem.is_editting():
                blkitem.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
                blkitem.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
            # under_ctrl is set when txtblkShapeControl points at this item.
            # Clear it before pooling so a recycled item never starts life with
            # a stale "I'm under shape control" flag (would mis-route
            # squeezeBoundingRect, set_size etc.).
            blkitem.under_ctrl = False
            # Clear any in-progress drag-reorder highlight so a recycled badge
            # does not paint with stale red/yellow borders next page.
            if getattr(blkitem, 'number_badge', None) is not None:
                blkitem.number_badge.set_highlight(0)
            blkitem.setVisible(False)
            # Detaching from the scene avoids paint/hit-test cost while pooled.
            if blkitem.scene() is not None:
                self.canvas.removeItem(blkitem)
        finally:
            blkitem.blockSignals(was_blocked)
            self.canvas.block_selection_signal = prev_block_sel
        if len(self._blk_pool) < self._pool_max_size:
            self._blk_pool.append(blkitem)
        # else: item drops out of all references and is GC'd, capping memory.

    def _pool_release_pair_widget(self, pw: TransPairWidget):
        # textEditList.removeWidget hides + removes from layout but keeps the
        # widget alive (parented to the layout's owner) so we can re-insert it.
        self.textEditList.removeWidget(pw)
        if len(self._pw_pool) < self._pool_max_size:
            self._pw_pool.append(pw)

    def _pool_acquire_blk_item(self) -> TextBlkItem:
        if self._blk_pool:
            return self._blk_pool.pop()
        return None

    def _pool_acquire_pair_widget(self) -> TransPairWidget:
        if self._pw_pool:
            return self._pw_pool.pop()
        return None

    def clearSceneTextitems(self):
        # Cancel any in-progress badge drag so stale src/target indices from the
        # previous page can't leak into the next page's reorder logic.
        self.cancel_badge_drag()
        self.hovering_transwidget = None
        self.txtblkShapeControl.setBlkItem(None)
        # Pool live items rather than destroying them so updateSceneTextitems can
        # reuse them on the next page. removeItem detaches from the scene; the
        # TextBlkItem instance itself stays alive in the pool with signals intact.
        for blkitem in self.textblk_item_list:
            self._pool_release_blk_item(blkitem)
        self.textblk_item_list.clear()
        self.textEditList.clearAllSelected()
        for textwidget in self.pairwidget_list:
            self._pool_release_pair_widget(textwidget)
        self.pairwidget_list.clear()

    def updateSceneTextitems(self):
        # Save current selection (all selected items, not just shape-control target)
        # so multi-select survives the page rebuild.
        selected_item_ids = [item.idx for item in self.canvas.selected_text_items(sort=False)]
        prev_shape_target_idx = None
        if self.txtblkShapeControl.blk_item is not None:
            prev_shape_target_idx = self.txtblkShapeControl.blk_item.idx
            if prev_shape_target_idx not in selected_item_ids:
                # Shape control can target an item that's hovered but not selected.
                # Track it separately so we can re-bind after the rebuild.
                pass
        self.hovering_transwidget = None

        # Hold off scene selectionChanged side effects across the whole rebuild
        # (including the txtblkShapeControl.setBlkItem(None) below, which can
        # trigger endEdit -> setSelected(True) on the outgoing item).
        # Each pooled-out item triggers selectionChanged from the scene, which
        # cascades into on_incanvas_selection_changed -> formatpanel.set_textblk_item.
        # Doing that against a half-cleared textblk_item_list is what causes
        # the formatpanel + selection box to flicker during a page switch.
        prev_block_sel = self.canvas.block_selection_signal
        self.canvas.block_selection_signal = True

        self.txtblkShapeControl.setBlkItem(None)

        # Clear text undo stack: the pool recycles TextBlkItem instances, so any QUndoCommand
        # holding strong references to old items would mutate the wrong page's data on Ctrl+Z.
        # Some callers (merge_pages, on_merge_finished, on_fin_import_doc, retranslation-no-pagechange)
        # don't clear it themselves, so we clear it unconditionally here to make pool reuse safe.
        if hasattr(self.canvas, 'text_undo_stack') and self.canvas.text_undo_stack is not None:
            self.canvas.text_undo_stack.clear()

        # Suppress canvas/view repaints while we batch-swap items to avoid
        # paint storms when a page has 50-100+ text blocks.
        gv = self.canvas.gv if hasattr(self.canvas, 'gv') else None
        viewport = gv.viewport() if gv is not None else None
        if viewport is not None:
            viewport.setUpdatesEnabled(False)

        debug_timing = bool(getattr(shared, 'DEBUG', False))
        t_clear = t_build = 0.0
        if debug_timing:
            t0 = time.perf_counter()

        try:
            self.clearSceneTextitems()

            if debug_timing:
                t1 = time.perf_counter()
                t_clear = t1 - t0

            block_list = self.imgtrans_proj.current_block_list()
            if block_list is not None:
                default_family = self.formatpanel.familybox.currentText()
                for textblock in block_list:
                    if textblock.font_family is None or textblock.font_family.strip() == '':
                        textblock.font_family = default_family
                    self.addTextBlock(textblock)

            if self.auto_textlayout_flag:
                self.updateTextBlkList()

            # Outline is ON by default. The only way to False is the user
            # pressing W (which flips pcfg.imgtrans_textblock). Read pcfg
            # directly so freshly-added blocks always render an outline unless
            # the user explicitly toggled them off -- previously OR'ing with
            # canvas.textblock_mode let the two flags drift out of sync and
            # silently leave a single block without an outline.
            display_mode = bool(getattr(pcfg, 'imgtrans_textblock', True))
            self.showTextblkItemRect(display_mode)

            # Restore selection (all items) so multi-select survives, and re-bind
            # the shape control target so the dashed selection frame reappears.
            shape_target = None
            if selected_item_ids and self.textblk_item_list:
                wanted = set(selected_item_ids)
                for blk_item in self.textblk_item_list:
                    if blk_item.idx in wanted:
                        blk_item.setSelected(True)
                        if blk_item.idx == prev_shape_target_idx:
                            shape_target = blk_item
                if shape_target is None:
                    # Previous shape target wasn't selected (or wasn't in the
                    # selection set); fall back to the first restored selection
                    # so the user still sees a frame.
                    for blk_item in self.textblk_item_list:
                        if blk_item.idx in wanted:
                            shape_target = blk_item
                            break
            if shape_target is not None:
                self.txtblkShapeControl.setBlkItem(shape_target)
        finally:
            self.canvas.block_selection_signal = prev_block_sel
            if viewport is not None:
                viewport.setUpdatesEnabled(True)
                viewport.update()

        # Resync panels with the restored selection now that the scene state
        # is consistent. Done outside block_selection_signal so the formatpanel
        # actually receives the update.
        if not self.canvas.block_selection_signal:
            self.on_incanvas_selection_changed()

        if debug_timing:
            t2 = time.perf_counter()
            t_build = t2 - t1 if t_clear else t2 - t0
            try:
                from utils.logger import logger as _logger
                _logger.debug(f"[updateSceneTextitems] clear={t_clear*1000:.1f}ms build={t_build*1000:.1f}ms blocks={len(self.textblk_item_list)} pool_blk={len(self._blk_pool)} pool_pw={len(self._pw_pool)}")
            except Exception:
                pass

    def addTextBlock(self, blk: Union[TextBlock, TextBlkItem] = None) -> TextBlkItem:
        if isinstance(blk, TextBlkItem):
            blk_item = blk
            blk_item.idx = len(self.textblk_item_list)
            self.addTextBlkItem(blk_item)
            blk_for_pair = blk_item.blk
            pair_widget = TransPairWidget(blk_for_pair, len(self.pairwidget_list), pcfg.fold_textarea)
            self.pairwidget_list.append(pair_widget)
            self.textEditList.addPairWidget(pair_widget)
            self._wire_pair_widget(pair_widget, blk_item, fresh=True)
            self.new_textblk.emit(blk_item.idx)
            return blk_item

        translation = ''
        if self.auto_textlayout_flag and not blk.vertical:
            translation = blk.translation
            blk.translation = ''

        # Outline is ON by default; the only path to False is W toggling
        # pcfg.imgtrans_textblock. Reading pcfg directly avoids the desync
        # bug where a fresh block (manual create / pipeline / pool reuse)
        # silently lacked an outline because canvas.textblock_mode and pcfg
        # had drifted apart mid-flow.
        display_mode = bool(getattr(pcfg, 'imgtrans_textblock', True))
        new_idx = len(self.textblk_item_list)

        # Pool reuse path: a recycled item already has its signals wired so we
        # skip addTextBlkItem (which would double-connect) and just re-attach it
        # to the scene after resetting its content.
        pooled = self._pool_acquire_blk_item()
        if pooled is not None:
            blk_item = pooled
            # _pool_release_blk_item raised blockSignals(True) to silence the
            # teardown phase. Append to the list FIRST while signals are still
            # blocked, then unblock -- otherwise reset_with_blk's reconnect of
            # documentSizeChanged can emit doc_size_changed before the item is
            # in textblk_item_list, and onTextBlkItemSizeChanged crashes with
            # IndexError trying to look up the new idx.
            blk_item.reset_with_blk(blk, new_idx, show_rect=display_mode)
            self.textblk_item_list.append(blk_item)
            blk_item.setParentItem(self.canvas.textLayer)
            blk_item.setVisible(True)
            blk_item.blockSignals(False)
            # Re-sync badge visibility with the current pcfg toggle: pooled items
            # could have been hidden by an earlier "N" press on a different page.
            if getattr(blk_item, 'number_badge', None) is not None:
                blk_item.number_badge.setVisible(getattr(pcfg, 'show_textblock_number', True))
        else:
            blk_item = TextBlkItem(blk, new_idx, show_rect=display_mode)
            self.addTextBlkItem(blk_item)

        if translation:
            blk.translation = translation
            rst = self.layout_textblk(blk_item, text=translation)
            if rst is None:
                blk_item.setPlainText(translation)

        # Pool reuse path for the pair widget: signals already wired, just
        # reset text content + re-insert into the layout.
        pooled_pw = self._pool_acquire_pair_widget()
        if pooled_pw is not None:
            pair_widget = pooled_pw
            pair_widget.textblock = blk
            pair_widget.idx = len(self.pairwidget_list)
            pair_widget.idx_label.setText(str(pair_widget.idx + 1).zfill(2))
            pair_widget.e_source.idx = pair_widget.idx
            pair_widget.e_trans.idx = pair_widget.idx
            self.pairwidget_list.append(pair_widget)
            self.textEditList.insertPairWidget(pair_widget, pair_widget.idx)
            self._wire_pair_widget(pair_widget, blk_item, fresh=False)
            # Clear stale per-widget undo history AFTER setPlainText so Ctrl+Z
            # on a recycled editor cannot replay edits from a different page
            # (and also discards the just-now setPlainText as an undo step).
            pair_widget.e_source.document().clearUndoRedoStacks()
            pair_widget.e_trans.document().clearUndoRedoStacks()
            pair_widget.e_source.old_undo_steps = pair_widget.e_source.document().availableUndoSteps()
            pair_widget.e_trans.old_undo_steps = pair_widget.e_trans.document().availableUndoSteps()
        else:
            pair_widget = TransPairWidget(blk, len(self.pairwidget_list), pcfg.fold_textarea)
            self.pairwidget_list.append(pair_widget)
            self.textEditList.addPairWidget(pair_widget)
            self._wire_pair_widget(pair_widget, blk_item, fresh=True)

        self.new_textblk.emit(blk_item.idx)
        return blk_item

    def _wire_pair_widget(self, pair_widget: 'TransPairWidget', blk_item: TextBlkItem, fresh: bool):
        # Set per-page text content. fresh=False means widget came from the pool
        # and signals are already connected -- skip re-connecting to avoid
        # duplicate slot invocations on edit.
        pair_widget.e_source.setPlainText(blk_item.blk.get_text())
        pair_widget.e_trans.setPlainText(blk_item.toPlainText())
        if not fresh:
            return

        pair_widget.e_source.focus_in.connect(self.on_transwidget_focus_in)
        pair_widget.e_source.ensure_scene_visible.connect(self.on_ensure_textitem_svisible)
        pair_widget.e_source.push_undo_stack.connect(self.on_push_edit_stack)
        pair_widget.e_source.redo_signal.connect(self.on_textedit_redo)
        pair_widget.e_source.undo_signal.connect(self.on_textedit_undo)
        pair_widget.e_source.show_select_menu.connect(self.on_show_select_menu)
        pair_widget.e_source.focus_out.connect(self.on_pairw_focusout)

        pair_widget.e_trans.focus_in.connect(self.on_transwidget_focus_in)
        pair_widget.e_trans.propagate_user_edited.connect(self.on_propagate_transwidget_edit)
        pair_widget.e_trans.ensure_scene_visible.connect(self.on_ensure_textitem_svisible)
        pair_widget.e_trans.push_undo_stack.connect(self.on_push_edit_stack)
        pair_widget.e_trans.redo_signal.connect(self.on_textedit_redo)
        pair_widget.e_trans.undo_signal.connect(self.on_textedit_undo)
        pair_widget.e_trans.show_select_menu.connect(self.on_show_select_menu)
        pair_widget.e_trans.focus_out.connect(self.on_pairw_focusout)
        pair_widget.drag_move.connect(self.textEditList.handle_drag_pos)
        pair_widget.pw_drop.connect(self.textEditList.on_pw_dropped)
        pair_widget.idx_edited.connect(self.textEditList.on_idx_edited)

    def addTextBlkItem(self, textblk_item: TextBlkItem) -> TextBlkItem:
        self.textblk_item_list.append(textblk_item)
        textblk_item.setParentItem(self.canvas.textLayer)
        textblk_item.begin_edit.connect(self.onTextBlkItemBeginEdit)
        textblk_item.end_edit.connect(self.onTextBlkItemEndEdit)
        textblk_item.hover_enter.connect(self.onTextBlkItemHoverEnter)
        textblk_item.leftbutton_pressed.connect(self.onLeftbuttonPressed)
        textblk_item.moving.connect(self.onTextBlkItemMoving)
        textblk_item.moved.connect(self.onTextBlkItemMoved)
        textblk_item.reshaped.connect(self.onTextBlkItemReshaped)
        textblk_item.rotated.connect(self.onTextBlkItemRotated)
        textblk_item.push_undo_stack.connect(self.on_push_textitem_undostack)
        textblk_item.undo_signal.connect(self.on_textedit_undo)
        textblk_item.redo_signal.connect(self.on_textedit_redo)
        textblk_item.propagate_user_edited.connect(self.on_propagate_textitem_edit)
        textblk_item.doc_size_changed.connect(self.onTextBlkItemSizeChanged)
        textblk_item.pasted.connect(self.onBlkitemPaste)
        # Badge signals are wired once per TextBlkItem instance. Pool reuse
        # keeps these connections intact -- reset_with_blk only refreshes the
        # badge's text/highlight, never rebuilds the badge object.
        if getattr(textblk_item, 'number_badge', None) is not None:
            badge = textblk_item.number_badge
            badge.badge_clicked.connect(self.onBadgeClicked)
            badge.badge_drag_started.connect(self.onBadgeDragStarted)
            badge.badge_dragging.connect(self.onBadgeDragging)
            badge.badge_drag_ended.connect(self.onBadgeDragEnded)
            # Context menu actions: reorder requests + auto-sort + visibility
            # toggle. Wired once per item; pool reuse preserves these.
            badge.move_to_position_requested.connect(self.move_block_to_position)
            badge.quick_reorder_requested.connect(self.open_quick_reorder_popup)
            badge.auto_sort_requested.connect(self.auto_sort_reading_order)
            badge.toggle_numbers_requested.connect(self.toggle_numbers_visible)
            badge.setVisible(getattr(pcfg, 'show_textblock_number', True))
        return textblk_item

    def deleteTextblkItemList(self, blkitem_list: List[TextBlkItem], p_widget_list: List[TransPairWidget]):
        # Dismiss any open quick-reorder popup -- its captured src_idx may
        # point at a block being deleted, leaving the popup ghosted.
        # Also cancel any badge drag for the same reason.
        self.cancel_badge_drag()
        selection_changed = False
        for blkitem, p_widget in zip(blkitem_list, p_widget_list):
            if blkitem.isSelected():
                selection_changed = True
            self.canvas.removeItem(blkitem) # removeItem itself will block incanvas_selection_changed
            self.textblk_item_list.remove(blkitem)
            self.pairwidget_list.remove(p_widget)
            self.textEditList.removeWidget(p_widget)
        self.updateTextBlkItemIdx()
        self.txtblkShapeControl.setBlkItem(None)
        if selection_changed:
            # it must be called after updateTextBlkItemIdx if blk.idx changed
            self.on_incanvas_selection_changed()

    def recoverTextblkItemList(self, blkitem_list: List[TextBlkItem], p_widget_list: List[TransPairWidget]):
        self.canvas.block_selection_signal = True
        for blkitem, p_widget in zip(blkitem_list, p_widget_list):
            self.textblk_item_list.insert(blkitem.idx, blkitem)
            blkitem.setParentItem(self.canvas.textLayer)
            self.pairwidget_list.insert(p_widget.idx, p_widget)
            self.textEditList.insertPairWidget(p_widget, p_widget.idx)
            if self.txtblkShapeControl.blk_item is not None and blkitem.isSelected():
                blkitem.setSelected(False)
        self.updateTextBlkItemIdx()
        self.on_incanvas_selection_changed()
        self.canvas.block_selection_signal = False
        
    def _safe_blk_item(self, idx: int):
        # Pooled TextBlkItems can fire signals with a stale idx during page
        # rebuilds (their layout/document signals reconnect mid-acquire and
        # may emit before textblk_item_list is repopulated). Every external
        # signal handler that indexes by id should route through this guard.
        if not isinstance(idx, int):
            return None
        if 0 <= idx < len(self.textblk_item_list):
            return self.textblk_item_list[idx]
        return None

    def _safe_pair_widget(self, idx: int):
        if not isinstance(idx, int):
            return None
        if 0 <= idx < len(self.pairwidget_list):
            return self.pairwidget_list[idx]
        return None

    def onTextBlkItemSizeChanged(self, idx: int):
        blk_item = self._safe_blk_item(idx)
        if blk_item is None:
            return
        if not self.txtblkShapeControl.reshaping:
            if self.txtblkShapeControl.blk_item == blk_item:
                self.txtblkShapeControl.updateBoundingRect()

    @property
    def app_clipborad(self) -> QClipboard:
        return self.app.clipboard()

    def onBlkitemPaste(self, idx: int):
        blk_item = self._safe_blk_item(idx)
        if blk_item is None:
            return
        text = self.app_clipborad.text()
        cursor = blk_item.textCursor()
        cursor.insertText(text)

    def onTextBlkItemBeginEdit(self, blk_id: int):
        blk_item = self._safe_blk_item(blk_id)
        if blk_item is None:
            return
        self.txtblkShapeControl.setBlkItem(blk_item)
        self.canvas.editing_textblkitem = blk_item
        self.formatpanel.set_textblk_item(blk_item)
        self.txtblkShapeControl.startEditing()
        pw = self._safe_pair_widget(blk_item.idx)
        if pw is None:
            return
        self.changeHoveringWidget(pw.e_trans)

    def changeHoveringWidget(self, edit: SourceTextEdit):
        if self.hovering_transwidget is not None and self.hovering_transwidget != edit:
            try:
                self.hovering_transwidget.setHoverEffect(False)
            except RuntimeError:
                # Previous hovering widget was Qt-deleted by a page rebuild.
                # Drop the dangling reference silently.
                pass
        self.hovering_transwidget = edit
        if edit is not None:
            # Guarded lookup: edit.idx can be stale during a paged rebuild.
            pw = self._safe_pair_widget(getattr(edit, 'idx', -1))
            if pw is None:
                return
            h = pw.height()
            if shared.USE_PYSIDE6:
                self.textEditList.ensureWidgetVisible(pw, ymargin=h)
            else:
                self.textEditList.ensureWidgetVisible(pw, yMargin=h)
            edit.setHoverEffect(True)

    def onLeftbuttonPressed(self, blk_id: int):
        blk_item = self._safe_blk_item(blk_id)
        if blk_item is None:
            return
        self.txtblkShapeControl.setBlkItem(blk_item)
        selections: List[TextBlkItem] = self.canvas.selectedItems()
        if len(selections) > 1:
            for item in selections:
                item.oldPos = item.pos()
        pw = self._safe_pair_widget(blk_id)
        if pw is not None:
            self.changeHoveringWidget(pw.e_trans)
        # Ensure canvas maintains focus when clicking on text blocks
        # This prevents selection from being lost due to focus issues
        if not self.canvas.gv.hasFocus():
            self.canvas.gv.setFocus()

    def onTextBlkItemEndEdit(self, blk_id: int):
        self.canvas.editing_textblkitem = None
        blk_item = self._safe_blk_item(blk_id)
        if blk_item is None:
            return
        blk_item.setSelected(True)
        self.txtblkShapeControl.endEditing()

    def editingTextItem(self) -> TextBlkItem:
        if self.txtblkShapeControl.isVisible() and self.canvas.editing_textblkitem is not None:
            return self.canvas.editing_textblkitem
        return None

    def savePrevBlkItem(self, blkitem: TextBlkItem):
        self.prev_blkitem = blkitem
        self.prev_textCursor = QTextCursor(self.prev_blkitem.textCursor())

    def is_editting(self):
        blk_item = self.txtblkShapeControl.blk_item
        return blk_item is not None and blk_item.is_editting()

    def onTextBlkItemHoverEnter(self, blk_id: int):
        if self.is_editting():
            return
        blk_item = self._safe_blk_item(blk_id)
        if blk_item is None:
            return
        if not blk_item.hasFocus():
            self.txtblkShapeControl.setBlkItem(blk_item)

    def onTextBlkItemMoving(self, item: TextBlkItem):
        self.txtblkShapeControl.updateBoundingRect()

    def onTextBlkItemMoved(self):
        selected_blks = self.canvas.selected_text_items()
        if len(selected_blks) > 0:
            self.canvas.push_undo_command(MoveBlkItemsCommand(selected_blks, self.txtblkShapeControl))
        
    def onTextBlkItemReshaped(self, item: TextBlkItem):
        self.canvas.push_undo_command(ReshapeItemCommand(item))

    def onTextBlkItemRotated(self, new_angle: float):
        blk_item = self.txtblkShapeControl.blk_item
        if blk_item:
            self.canvas.push_undo_command(RotateItemCommand(blk_item, new_angle, self.txtblkShapeControl))

    def onDeleteBlkItems(self, mode: int):
        selected_blks = self.canvas.selected_text_items()
        if len(selected_blks) == 0 and self.txtblkShapeControl.blk_item is not None:
            selected_blks.append(self.txtblkShapeControl.blk_item)
        if len(selected_blks) > 0:
            self.canvas.push_undo_command(DeleteBlkItemsCommand(selected_blks, mode, self))

    def onCopyBlkItems(self):
        selected_blks = self.canvas.selected_text_items()
        if len(selected_blks) == 0 and self.txtblkShapeControl.blk_item is not None:
            selected_blks.append(self.txtblkShapeControl.blk_item)

        if len(selected_blks) == 0:            
            return

        self.canvas.clipboard_blks.clear()
        if self.canvas.text_change_unsaved():
            self.updateTextBlkList()

        pos = selected_blks[0].blk.bounding_rect()
        pos_x = int(pos[0] + pos[2] / 2)
        pos_y = int(pos[1] + pos[3] / 2)

        textlist = []
        for blkitem in selected_blks:
            blk = copy.deepcopy(blkitem.blk)
            blk.adjust_pos(-pos_x, -pos_y)
            self.canvas.clipboard_blks.append(blk)
            textlist.append(blkitem.toPlainText().strip())
        textlist = '\n'.join(textlist)
        self.app_clipborad.setText(textlist, QClipboard.Mode.Clipboard)

    def onPasteBlkItems(self, pos: QPointF):
        if pos is None:
            pos_x, pos_y = 0, 0
        else:
            pos_x, pos_y = pos.x(), pos.y()
            pos_x = int(pos_x / self.canvas.scale_factor)
            pos_y = int(pos_y / self.canvas.scale_factor)
        blkitem_list, pair_widget_list = [], []
        for blk in self.canvas.clipboard_blks:
            blk = copy.deepcopy(blk)
            blk.adjust_pos(pos_x, pos_y)
            blkitem = self.addTextBlock(blk)
            pairw = self.pairwidget_list[-1]
            blkitem_list.append(blkitem)
            pair_widget_list.append(pairw)
        if len(blkitem_list) > 0:
            self.canvas.clearSelection()
            self.canvas.push_undo_command(PasteBlkItemsCommand(blkitem_list, pair_widget_list, self))
            if len(blkitem_list) == 1:
                self.formatpanel.set_textblk_item(blkitem_list[0])
            else:
                self.formatpanel.set_textblk_item(multi_select=True)

    def onFormatTextblks(self, fmt: FontFormat = None):
        if fmt is None:
            fmt = self.formatpanel.global_format
        self.apply_fontformat(fmt)

    def onAutoLayoutTextblks(self):
        selected_blks = self.canvas.selected_text_items()
        old_html_lst, old_rect_lst, trans_widget_lst = [], [], []
        selected_blks = [blk for blk in selected_blks if not blk.fontformat.vertical]
        if len(selected_blks) > 0:
            kept_blks = []
            for blkitem in selected_blks:
                pw = self._safe_pair_widget(getattr(blkitem, 'idx', -1))
                if pw is None:
                    # Drop blocks whose pair widget is in flux; the operation
                    # will simply act on the remaining valid set.
                    continue
                old_html_lst.append(blkitem.toHtml())
                old_rect_lst.append(blkitem.absBoundingRect(qrect=True))
                trans_widget_lst.append(pw.e_trans)
                self.layout_textblk(blkitem)
                kept_blks.append(blkitem)

            if kept_blks:
                self.canvas.push_undo_command(AutoLayoutCommand(kept_blks, old_rect_lst, old_html_lst, trans_widget_lst))

    def onResetAngle(self, reset_all: bool = False, items: List[TextBlkItem] = None):
        # If items list is provided, use it directly; otherwise use selected items
        if items is not None:
            selected_blks = items
        else:
            selected_blks = self.canvas.selected_text_items()
        if len(selected_blks) > 0:
            cmd = ResetAngleCommand(selected_blks, self.txtblkShapeControl, reset_all=reset_all)
            self.canvas.push_undo_command(cmd)

    def onSqueezeBlk(self):
        selected_blks = self.canvas.selected_text_items()
        if len(selected_blks) > 0:
            self.canvas.push_undo_command(SqueezeCommand(selected_blks, self.txtblkShapeControl))

    def on_incanvas_selection_changed(self):
        if self.canvas.textEditMode():
            textitems = self.canvas.selected_text_items()
            selected_ids = [t.idx for t in textitems]
            self.textEditList.set_selected_list(selected_ids)
            if len(textitems) == 1:
                self.formatpanel.set_textblk_item(textitems[-1])
            else:
                self.formatpanel.set_textblk_item(multi_select=bool(textitems))

    def layout_textblk(self, blkitem: TextBlkItem, text: str = None, mask: np.ndarray = None, bounding_rect: List = None, region_rect: List = None):
        
        '''
        auto text layout, vertical writing is not supported yet.
        '''

        img = self.imgtrans_proj.img_array
        if img is None:
            return

        src_is_cjk = is_cjk(pcfg.module.translate_source)
        tgt_is_cjk = is_cjk(pcfg.module.translate_target)

        # disable for vertical writing
        if blkitem.blk.vertical:
            return
        
        old_br = blkitem.absBoundingRect(qrect=True)
        old_br = [old_br.x(), old_br.y(), old_br.width(), old_br.height()]
        if old_br[2] < 1:
            return

        blk_font = blkitem.font()
        fmt = blkitem.get_fontformat()
        blk_font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, fmt.letter_spacing * 100)
        text_size_func = lambda text: get_text_size(QFontMetricsF(blk_font), text)

        restore_charfmts = False
        if text is None:
            text = blkitem.toPlainText()
            restore_charfmts = True

        if not text.strip():
            return

        if mask is None:
            im_h, im_w = img.shape[:2]
            bounding_rect = blkitem.absBoundingRect(max_h=im_h, max_w=im_w)
            if bounding_rect[2] <= 0 or bounding_rect[3] <= 0:
                blkitem.setPlainText(text)
                if len(self.pairwidget_list) > blkitem.idx:
                    self.pairwidget_list[blkitem.idx].e_trans.setPlainText(text)
                return
            if tgt_is_cjk:
                max_enlarge_ratio = 2.5
            else:
                max_enlarge_ratio = 3
            enlarge_ratio = min(max(bounding_rect[2] / bounding_rect[3], bounding_rect[3] / bounding_rect[2]) * 1.5, max_enlarge_ratio)
            mask, ballon_area, mask_xyxy, region_rect = extract_ballon_region(img, bounding_rect, enlarge_ratio=enlarge_ratio, cal_region_rect=True)
        else:
            mask_xyxy = [bounding_rect[0], bounding_rect[1], bounding_rect[0]+bounding_rect[2], bounding_rect[1]+bounding_rect[3]]
        
        words, delimiter = seg_text(text, pcfg.module.translate_target)
        if len(words) < 1:
            return

        wl_list = get_words_length_list(QFontMetricsF(blk_font), words)
        text_w, text_h = text_size_func(text)
        text_area = text_w * text_h
        if tgt_is_cjk:
            line_height = int(round(fmt.line_spacing * text_size_func('X木')[1]))
        else:
            line_height = int(round(fmt.line_spacing * text_size_func('X')[1]))
        delimiter_len = text_size_func(delimiter)[0]
 
        ref_src_lines = False
        if not blkitem.blk.src_is_vertical:
            ref_src_lines = blkitem.blk.line_coord_valid(old_br)

        adaptive_fntsize = False
        resize_ratio = 1
        if self.auto_textlayout_flag and pcfg.let_fntsize_flag == 0 and pcfg.let_autolayout_flag:
            if blkitem.blk.src_is_vertical and blkitem.blk.vertical != blkitem.blk.src_is_vertical:
                adaptive_fntsize = True
                area_ratio = ballon_area / text_area
                ballon_area_thresh = 1.7
                downscale_constraint = 0.6
                resize_ratio = np.clip(min(area_ratio / ballon_area_thresh, region_rect [2] / max(wl_list)), downscale_constraint, 1.0)

            else:
                if not src_is_cjk:
                    resize_ratio_ballon = max(ballon_area / 1.2 / text_area, 0.7)
                    if ref_src_lines:
                        _, src_width = blkitem.blk.normalizd_width_list(normalize=False)
                        resize_ratio_src = src_width / (sum(wl_list) + max((len(wl_list) - 1 - len(blkitem.blk.lines_array())), 0) * delimiter_len)
                        resize_ratio = min(resize_ratio_ballon, resize_ratio_src)
                    else:
                        resize_ratio = resize_ratio_ballon
                elif not blkitem.blk.src_is_vertical and ref_src_lines:
                    _, src_width = blkitem.blk.normalizd_width_list(normalize=False)
                    resize_ratio_src = src_width / (sum(wl_list) + max((len(wl_list) - 1 - len(blkitem.blk.lines_array())), 0) * delimiter_len)
                    resize_ratio = max(resize_ratio_src * 1.5, 0.5)
                resize_ratio = min(max(resize_ratio, 0.6), 1)

        if resize_ratio != 1:
            new_font_size = blk_font.pointSizeF() * resize_ratio   
            blk_font.setPointSizeF(new_font_size)
            wl_list = (np.array(wl_list, np.float64) * resize_ratio).astype(np.int32).tolist()
            line_height = int(line_height * resize_ratio)
            text_w = int(text_w * resize_ratio)
            delimiter_len = int(delimiter_len * resize_ratio)

        max_central_width = np.inf
        if fmt.alignment == 1:
            if len(blkitem.blk) > 0:
                centroid = blkitem.blk.center().astype(np.int64).tolist()
                centroid[0] -= mask_xyxy[0]
                centroid[1] -= mask_xyxy[1]
            else:
                centroid = [bounding_rect[2] // 2, bounding_rect[3] // 2]
        else:
            max_central_width = np.inf
            centroid = [0, 0]
            abs_centroid = [bounding_rect[0], bounding_rect[1]]
            if len(blkitem.blk) > 0:
                blkitem.blk.lines[0]
                abs_centroid = blkitem.blk.lines[0][0]
                centroid[0] = int(abs_centroid[0] - mask_xyxy[0])
                centroid[1] = int(abs_centroid[1] - mask_xyxy[1])

        new_text, xywh, start_from_top, adjust_xy = layout_text(
            blkitem.blk,
            mask, 
            mask_xyxy, 
            centroid, 
            words, 
            wl_list, 
            delimiter, 
            delimiter_len, 
            line_height, 
            0, 
            max_central_width,
            src_is_cjk=src_is_cjk,
            tgt_is_cjk=tgt_is_cjk,
            ref_src_lines=ref_src_lines
        )

        # font size post adjustment
        post_resize_ratio = 1
        if adaptive_fntsize:
            downscale_constraint = 0.5
            w = xywh[2]
            post_resize_ratio = np.clip(max(region_rect[2] / w, downscale_constraint), 0, 1)
            resize_ratio *= post_resize_ratio

        if post_resize_ratio != 1:
            cx, cy = xywh[0] + xywh[2] / 2, xywh[1] + xywh[3] / 2
            w, h = xywh[2] * post_resize_ratio, xywh[3] * post_resize_ratio
            xywh = [int(cx - w / 2), int(cy - h / 2), int(w), int(h)]

        if resize_ratio != 1:
            new_font_size = blkitem.font().pointSizeF() * resize_ratio
            blkitem.textCursor().clearSelection()
            blkitem.setFontSize(new_font_size)
            blk_font.setPointSizeF(new_font_size)

        if restore_charfmts:
            char_fmts = blkitem.get_char_fmts()        
        
        ffmt = QFontMetricsF(blk_font)
        maxw = max([ffmt.horizontalAdvance(t) for t in new_text.split('\n')])
        blkitem.set_size(maxw * 1.5, xywh[3], set_layout_maxsize=True)
        blkitem.setPlainText(new_text)
        if len(self.pairwidget_list) > blkitem.idx:
            self.pairwidget_list[blkitem.idx].e_trans.setPlainText(new_text)
        if restore_charfmts:
            self.restore_charfmts(blkitem, text, new_text, char_fmts)
        blkitem.squeezeBoundingRect()
        return True
    
    def restore_charfmts(self, blkitem: TextBlkItem, text: str, new_text: str, char_fmts: List[QTextCharFormat]):
        cursor = blkitem.textCursor()
        cpos = 0
        num_text = len(new_text)
        num_fmt = len(char_fmts)
        blkitem.layout.relayout_on_changed = False
        blkitem.repaint_on_changed = False
        if num_text >= num_fmt:
            for fmt_i in range(num_fmt):
                fmt = char_fmts[fmt_i]
                ori_char = text[fmt_i].strip()
                if ori_char == '':
                    continue
                else:
                    if cursor.atEnd():   
                        break
                    matched = False
                    while cpos < num_text:
                        if new_text[cpos] == ori_char:
                            matched = True
                            break
                        cpos += 1
                    if matched:
                        cursor.clearSelection()
                        cursor.setPosition(cpos)
                        cursor.setPosition(cpos+1, QTextCursor.MoveMode.KeepAnchor)
                        cursor.setCharFormat(fmt)
                        cursor.setBlockCharFormat(fmt)
                        cpos += 1
        blkitem.repaint_on_changed = True
        blkitem.layout.relayout_on_changed = True
        blkitem.layout.reLayout()
        blkitem.repaint_background()

    def onEndCreateTextBlock(self, rect: QRectF):
        
        xyxy = np.array([rect.x(), rect.y(), rect.right(), rect.bottom()])        
        xyxy = np.round(xyxy).astype(np.int32)
        block = TextBlock(xyxy)
        xywh = np.copy(xyxy)
        xywh[[2, 3]] -= xywh[[0, 1]]
        block.set_lines_by_xywh(xywh)
        block.src_is_vertical = self.formatpanel.global_format.vertical
        blk_item = TextBlkItem(block, len(self.textblk_item_list), set_format=False, show_rect=True)
        default_fontformat = self.formatpanel.global_format.deepcopy()
        if pcfg.fixed_font_enabled:
            default_fontformat.font_family = pcfg.fixed_font_family
            default_fontformat.font_size = pcfg.fixed_font_size
        blk_item.set_fontformat(default_fontformat)
        block.fontformat.merge(default_fontformat)
        
        
        self.canvas.push_undo_command(CreateItemCommand(blk_item, self))

    def on_paste2selected_textitems(self):
        blkitems = self.canvas.selected_text_items()
        text = self.app_clipborad.text()

        num_blk = len(blkitems)
        if num_blk < 1:
            return
        
        if num_blk > 1:
            text_list = text.rstrip().split('\n')
            num_text = len(text_list)
            if num_text > 1:
                if num_text > num_blk:
                    text_list = text_list[:num_blk]
                elif num_text < num_blk:
                    text_list = text_list + [text_list[-1]] * (num_blk - num_text)
                text = text_list
        
        # Drop blocks whose pair widget is missing (page rebuild in flight).
        # The MultiPasteCommand expects 1:1 length parity between blkitems and
        # etrans, so filter both sides together.
        kept_blks = []
        etrans = []
        for blkitem in blkitems:
            pw = self._safe_pair_widget(getattr(blkitem, 'idx', -1))
            if pw is None:
                continue
            kept_blks.append(blkitem)
            etrans.append(pw.e_trans)
        if not kept_blks:
            return
        self.canvas.push_undo_command(MultiPasteCommand(text, kept_blks, etrans))

    def onRotateTextBlkItem(self, item: TextBlock):
        self.canvas.push_undo_command(RotateItemCommand(item))
    
    def on_transwidget_focus_in(self, idx: int):
        if self.is_editting():
            textitm = self.editingTextItem()
            textitm.endEdit()
            # Guard nested lookup; textitm could have been recycled into the
            # pool by a signal storm before we get here.
            pw_active = self._safe_pair_widget(getattr(textitm, 'idx', -1))
            if pw_active is not None:
                pw_active.e_trans.setHoverEffect(False)
            self.textEditList.clearAllSelected()

        blk_item = self._safe_blk_item(idx)
        if blk_item is not None:
            sender = self.sender()
            if isinstance(sender, TransTextEdit):
                blk_item.setCacheMode(QGraphicsItem.CacheMode.NoCache)
            self.canvas.gv.ensureVisible(blk_item)
            self.txtblkShapeControl.setBlkItem(blk_item)

    def on_textedit_redo(self):
        self.canvas.redo_textedit()

    def on_textedit_undo(self):
        self.canvas.undo_textedit()

    def on_show_select_menu(self, pos: QPoint, selected_text: str):
        if pcfg.textselect_mini_menu:
            if not selected_text:
                if self.selectext_minimenu.isVisible():
                    self.selectext_minimenu.hide()
            else:
                self.selectext_minimenu.show()
                self.selectext_minimenu.move(self.mainwindow.mapFromGlobal(pos))
                self.selectext_minimenu.selected_text = selected_text

    def on_block_current_editor(self, block: bool):
        w: SourceTextEdit = self.app.focusWidget()
        if isinstance(w, SourceTextEdit) or isinstance(w, TextBlkItem):
            w.block_all_input = block

    def on_pairw_focusout(self, idx: int):
        if self.selectext_minimenu.isVisible():
            self.selectext_minimenu.hide()
        sender = self.sender()
        if isinstance(sender, TransTextEdit):
            blk_item = self._safe_blk_item(idx)
            if blk_item is not None:
                blk_item.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)

    def on_push_textitem_undostack(self, num_steps: int, is_formatting: bool):
        blkitem: TextBlkItem = self.sender()
        # Guard with _safe_pair_widget: pooled blkitem may emit push_undo_stack
        # mid-rebuild when the pairwidget at blkitem.idx has not yet been
        # reattached. Without this we'd crash with IndexError on rapid page swap.
        e_trans = None
        if not is_formatting:
            pw = self._safe_pair_widget(getattr(blkitem, 'idx', -1))
            if pw is None:
                return
            e_trans = pw.e_trans
        self.canvas.push_undo_command(TextItemEditCommand(blkitem, e_trans, num_steps, self.textpanel.formatpanel), update_pushed_step=is_formatting)

    def on_push_edit_stack(self, num_steps: int):
        edit: Union[TransTextEdit, SourceTextEdit] = self.sender()
        is_trans = type(edit) == TransTextEdit
        blkitem = self._safe_blk_item(getattr(edit, 'idx', -1)) if is_trans else None
        if is_trans and blkitem is None:
            # The text-edit widget was orphaned (page rebuild in flight).
            # Skip pushing -- the new page's undo stack is the right target.
            return
        self.canvas.push_undo_command(TextEditCommand(edit, num_steps, blkitem), update_pushed_step=not is_trans)

    def on_propagate_textitem_edit(self, pos: int, added_text: str, joint_previous: bool):
        blk_item: TextBlkItem = self.sender()
        pw = self._safe_pair_widget(getattr(blk_item, 'idx', -1))
        if pw is None:
            return
        edit = pw.e_trans
        propagate_user_edit(blk_item, edit, pos, added_text, joint_previous)
        self.canvas.push_text_command(command=None, update_pushed_step=True)

    def on_propagate_transwidget_edit(self, pos: int, added_text: str, joint_previous: bool):
        edit: TransTextEdit = self.sender()
        blk_item = self._safe_blk_item(getattr(edit, 'idx', -1))
        if blk_item is None:
            return
        if blk_item.isEditing():
            blk_item.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        propagate_user_edit(edit, blk_item, pos, added_text, joint_previous)
        self.canvas.push_text_command(command=None, update_pushed_step=True)

    def apply_fontformat(self, fontformat: FontFormat):
        selected_blks = self.canvas.selected_text_items()
        trans_widget_list = []
        for blk in selected_blks:
            pw = self._safe_pair_widget(getattr(blk, 'idx', -1))
            # Skip silently rather than crashing on a stale-indexed selection.
            if pw is None:
                continue
            trans_widget_list.append(pw.e_trans)
        if len(selected_blks) > 0:
            self.canvas.push_undo_command(ApplyFontformatCommand(selected_blks, trans_widget_list, fontformat))
            if self.formatpanel.global_mode():
                if id(self.formatpanel.active_text_style_format()) != id(fontformat):
                    self.formatpanel.deactivate_style_label()
                self.formatpanel.on_active_textstyle_label_changed()
            else:
                self.formatpanel.set_active_format(fontformat)

    def on_transwidget_selection_changed(self):
        selitems = self.canvas.selected_text_items()
        selset = {pw.idx: pw for pw in self.textEditList.checked_list}
        self.canvas.block_selection_signal = True
        for blkitem in selitems:
            if blkitem.idx not in selset:
                blkitem.setSelected(False)
            else:
                selset.pop(blkitem.idx)
        for idx in selset:
            # Guard: pw idx in checked_list can outlive a page rebuild for one
            # paint cycle. Skip rather than IndexError -- the next selection
            # event reconciles state.
            blk = self._safe_blk_item(idx)
            if blk is not None:
                blk.setSelected(True)
        self.canvas.block_selection_signal = False

    def on_textedit_list_focusout(self):
        fw = self.app.focusWidget()
        focusing_edit = isinstance(fw, (SourceTextEdit, TransTextEdit))
        if fw == self.canvas.gv or focusing_edit:
            self.textEditList.clearDrag()
        if focusing_edit:
            self.textEditList.clearAllSelected()

    def on_rearrange_blks(self, mv_map: Tuple[np.ndarray]):
        self.canvas.push_undo_command(RearrangeBlksCommand(mv_map, self))

    def updateTextBlkItemIdx(self, sel_ids: set = None):
        for ii, blk_item in enumerate(self.textblk_item_list):
            if sel_ids is not None and ii not in sel_ids:
                continue
            blk_item.idx = ii
            self.pairwidget_list[ii].updateIndex(ii)
            # Keep the floating badge label in sync after every renumber. The
            # badge owns its own number cache so we refresh explicitly.
            if getattr(blk_item, 'number_badge', None) is not None:
                blk_item.number_badge.set_idx(ii)
        cl = self.textEditList.checked_list
        if len(cl) != 0:
            cl.sort(key=lambda x: x.idx)

    def updateTextBlkList(self):
        cbl = self.imgtrans_proj.current_block_list()
        if cbl is None:
            return
        cbl.clear()
        for blk_item, trans_pair in zip(self.textblk_item_list, self.pairwidget_list):
            if not blk_item.document().isEmpty():
                blk_item.blk.rich_text = blk_item.toHtml()
                blk_item.blk.translation = blk_item.toPlainText()
            else:
                blk_item.blk.rich_text = ''
                blk_item.blk.translation = ''
            blk_item.blk.text = [trans_pair.e_source.toPlainText()]
            blk_item.blk._bounding_rect = blk_item.absBoundingRect()
            blk_item.updateBlkFormat()
            cbl.append(blk_item.blk)

    def updateTranslation(self):
        for blk_item, transwidget in zip(self.textblk_item_list, self.pairwidget_list):
            transwidget.e_trans.setPlainText(blk_item.blk.translation)
            blk_item.setPlainText(blk_item.blk.translation)
        self.canvas.clear_text_stack()

    def showTextblkItemRect(self, draw_rect: bool):
        for blk_item in self.textblk_item_list:
            blk_item.draw_rect = draw_rect
            blk_item.update()

    def set_blkitems_selection(self, selected: bool, blk_items: List[TextBlkItem] = None):
        self.canvas.block_selection_signal = True
        if blk_items is None:
            blk_items = self.textblk_item_list
        for blk_item in blk_items:
            blk_item.setSelected(selected)
        self.canvas.block_selection_signal = False
        self.on_incanvas_selection_changed()

    def on_ensure_textitem_svisible(self):
        edit: Union[TransTextEdit, SourceTextEdit] = self.sender()
        self.changeHoveringWidget(edit)
        # Guard the lookup: signal can fire from a soon-to-be-pooled edit
        # whose .idx is stale during page swap. _safe_blk_item returns None
        # for out-of-range indices instead of raising IndexError.
        blk_item = self._safe_blk_item(getattr(edit, 'idx', -1))
        if blk_item is None:
            return
        self.canvas.gv.ensureVisible(blk_item)
        self.txtblkShapeControl.setBlkItem(blk_item)

    def onApplyFontToAllPages(self):
        fmt = self.formatpanel.global_format.deepcopy()
        if self.imgtrans_proj.current_img is not None:
            self.updateTextBlkList()
        for pagename, blklist in self.imgtrans_proj.pages.items():
            for blk in blklist:
                blk.fontformat.merge(fmt)
                if blk.rich_text:
                    blk.rich_text = apply_fontformat_to_html(blk.rich_text, blk.fontformat)
        for blk_item in self.textblk_item_list:
            blk_item.set_fontformat(blk_item.blk.fontformat, set_char_format=True)
        self.mainwindow.applyFontToAllPagesAndSave()

    def on_page_replace_one(self):
        self.canvas.push_undo_command(PageReplaceOneCommand(self.canvas.search_widget))

    def on_page_replace_all(self):
        self.canvas.push_undo_command(PageReplaceAllCommand(self.canvas.search_widget))

    # --- Number badge: visibility toggle, click-to-select, drag-to-reorder ---

    def set_numbers_visible(self, visible: bool):
        # Toggle every live block's badge. Pooled items are picked up via
        # addTextBlock when they re-enter the scene, so we only walk the live list.
        for blk_item in self.textblk_item_list:
            badge = getattr(blk_item, 'number_badge', None)
            if badge is not None:
                badge.setVisible(visible)

    def onBadgeClicked(self, idx: int):
        # Single-click on a badge: select that block and surface it in the
        # right-side translation panel. Mimics the click flow of selecting via
        # the canvas item itself but skips edit-mode entry.
        if idx < 0 or idx >= len(self.textblk_item_list):
            return
        blk_item = self.textblk_item_list[idx]
        self.canvas.block_selection_signal = True
        self.canvas.clearSelection()
        blk_item.setSelected(True)
        self.canvas.block_selection_signal = False
        self.txtblkShapeControl.setBlkItem(blk_item)
        if 0 <= idx < len(self.pairwidget_list):
            self.changeHoveringWidget(self.pairwidget_list[idx].e_trans)
            self.textEditList.set_selected_list([idx])
        self.canvas.gv.ensureVisible(blk_item)
        # Sync formatpanel + scene state since we suppressed selection signal.
        self.on_incanvas_selection_changed()

    def _badge_at_scene_pos(self, scene_pos: QPointF):
        # Find the topmost badge under the cursor. ItemIgnoresTransformations
        # invalidates plain mapToScene-based hit tests (the badge's scene
        # bounding rect is in untransformed local size, not the painted screen
        # size), so we route the test through QGraphicsView.items(viewport_pos)
        # which handles the flag correctly.
        from .textblock_badge import TextBlockNumberBadge  # local: avoid cyclic import at module load
        gv = self.canvas.gv
        viewport_pt = gv.mapFromScene(scene_pos)
        for it in gv.items(viewport_pt):
            if isinstance(it, TextBlockNumberBadge) and it.isVisible():
                return it.idx
        return None

    def onBadgeDragStarted(self, src_idx: int):
        # Begin a drag-reorder gesture. Manager owns target tracking so the
        # dragged badge does not need to know about siblings.
        self._badge_drag_src = src_idx
        self._badge_drag_target = None
        if 0 <= src_idx < len(self.textblk_item_list):
            badge = self.textblk_item_list[src_idx].number_badge
            if badge is not None:
                badge.set_highlight(2)

    def onBadgeDragging(self, scene_pos: QPointF):
        if getattr(self, '_badge_drag_src', None) is None:
            return
        new_target = self._badge_at_scene_pos(scene_pos)
        # Do not treat hovering the source badge as a drop target -- a drop on
        # the same idx is a no-op and the highlight would otherwise flash.
        if new_target == self._badge_drag_src:
            new_target = None
        prev = self._badge_drag_target
        if prev != new_target:
            if prev is not None and 0 <= prev < len(self.textblk_item_list):
                old_badge = self.textblk_item_list[prev].number_badge
                if old_badge is not None:
                    old_badge.set_highlight(0)
            self._badge_drag_target = new_target
            if new_target is not None and 0 <= new_target < len(self.textblk_item_list):
                new_badge = self.textblk_item_list[new_target].number_badge
                if new_badge is not None:
                    new_badge.set_highlight(1)

    def onBadgeDragEnded(self, scene_pos: QPointF):
        src = getattr(self, '_badge_drag_src', None)
        tgt = getattr(self, '_badge_drag_target', None)
        # Reset all visual state regardless of outcome.
        if src is not None and 0 <= src < len(self.textblk_item_list):
            badge = self.textblk_item_list[src].number_badge
            if badge is not None:
                badge.set_highlight(0)
        if tgt is not None and 0 <= tgt < len(self.textblk_item_list):
            badge = self.textblk_item_list[tgt].number_badge
            if badge is not None:
                badge.set_highlight(0)
        self._badge_drag_src = None
        self._badge_drag_target = None
        if src is None or tgt is None or src == tgt:
            return
        self._reorder_block_to_position(src, tgt)

    def cancel_badge_drag(self):
        # ESC handler / page-change handler. Wipe drag state without reordering.
        # Safe to call when no drag is active (e.g. ESC pressed outside any
        # badge press, or clearSceneTextitems on first page load).
        #
        # Bug fixes hardened here (M1, M2, L9):
        #   * M1/L9: previously we only cleared the highlight border. The badge
        #     itself still held _dragging=True, _press_scene_pos and a
        #     ClosedHand cursor. After ESC the cursor stayed stuck and a
        #     subsequent click on the same badge re-entered drag mode without
        #     a fresh press. badge.cancel() now resets _dragging,
        #     _press_scene_pos, the cursor, and releases any pending mouse
        #     grab.
        #   * M2: when the page changes mid-drag, _pool_release_blk_item hides
        #     and removes the source badge from the scene. If the scene mouse
        #     grab was still on that badge, the eventual mouseRelease event
        #     would route to a detached item -> AttributeError. We now call
        #     badge.cancel() (which calls ungrabMouse()) BEFORE pool release
        #     happens. Note: clearSceneTextitems() calls cancel_badge_drag()
        #     before the pool release loop, so as long as we ungrab here the
        #     subsequent pool release is safe.
        src = getattr(self, '_badge_drag_src', None)
        tgt = getattr(self, '_badge_drag_target', None)
        if src is not None and 0 <= src < len(self.textblk_item_list):
            b = self.textblk_item_list[src].number_badge
            if b is not None:
                # cancel() resets cursor + drag flags + ungrabs mouse, in
                # addition to clearing the highlight.
                b.cancel()
        if tgt is not None and 0 <= tgt < len(self.textblk_item_list):
            b = self.textblk_item_list[tgt].number_badge
            if b is not None:
                b.set_highlight(0)
        self._badge_drag_src = None
        self._badge_drag_target = None
        # Also dismiss any open quick-reorder popup. ESC during drag should
        # leave nothing floating either.
        self._dismiss_quick_reorder_popup()

    def _reorder_block_to_position(self, src_idx: int, target_idx: int):
        # "Insert before target" semantics: removing src first shifts indices.
        # We translate that into the (src_ids, tgt_ids) tuple format that
        # RearrangeBlksCommand already supports for the textEditList drag flow.
        n = len(self.textblk_item_list)
        if n < 2 or not (0 <= src_idx < n) or not (0 <= target_idx < n):
            return
        if src_idx == target_idx:
            return
        order = list(range(n))
        moved = order.pop(src_idx)
        # If the source was before the target, popping shifts the target left
        # by 1. Insert-before becomes insert at the new (shifted) target index.
        if src_idx < target_idx:
            insert_pos = target_idx - 1
        else:
            insert_pos = target_idx
        order.insert(insert_pos, moved)
        ids_ori, ids_tgt = [], []
        for new_pos, old_pos in enumerate(order):
            if new_pos != old_pos:
                ids_ori.append(old_pos)
                ids_tgt.append(new_pos)
        if not ids_ori:
            return
        self.canvas.push_undo_command(RearrangeBlksCommand((ids_ori, ids_tgt), self))

    def move_block_to_position(self, src_idx: int, target_idx: int):
        # END-POSITION semantics: target_idx is the desired FINAL 0-based index
        # of the moved block. Used by Ctrl+J popup, context menu, and any
        # programmatic mover where the user thinks in terms of "where should
        # this block end up" (e.g. typing "8" must put the block at slot 8).
        #
        # Drag-and-drop uses the separate insert-before flow in
        # _reorder_block_to_position, which keeps the standard "drop on
        # target" UI metaphor and does not pass through this wrapper.
        n = len(self.textblk_item_list)
        if n < 2:
            return
        if not (0 <= src_idx < n):
            return
        if target_idx < 0:
            target_idx = 0
        elif target_idx >= n:
            target_idx = n - 1
        if src_idx == target_idx:
            return
        if getattr(self, '_badge_drag_src', None) is not None:
            self.cancel_badge_drag()

        order = list(range(n))
        moved = order.pop(src_idx)
        order.insert(target_idx, moved)
        ids_ori, ids_tgt = [], []
        for new_pos, old_pos in enumerate(order):
            if new_pos != old_pos:
                ids_ori.append(old_pos)
                ids_tgt.append(new_pos)
        if not ids_ori:
            return
        self.canvas.push_undo_command(RearrangeBlksCommand((ids_ori, ids_tgt), self))

    def toggle_numbers_visible(self):
        # Flip pcfg + push the new visibility through to all live badges.
        # Mirrors MainWindow.shortcutToggleNumberBadge so badge-context-menu
        # and the N hotkey converge on the same persisted state.
        new_visible = not getattr(pcfg, 'show_textblock_number', True)
        pcfg.show_textblock_number = new_visible
        self.set_numbers_visible(new_visible)

    def onCanvasReorderRequested(self, action: str):
        # Right-click context menu on canvas dispatches reorder actions here.
        # We resolve the target block from the current selection / shape
        # control, then route to the same primitives the keyboard shortcuts
        # use. action is one of: 'top', 'up', 'down', 'bottom', 'position',
        # 'auto_sort'.
        if action == 'auto_sort':
            self.auto_sort_reading_order()
            return
        target_blk = None
        sel = self.canvas.selected_text_items()
        if sel:
            target_blk = sel[0]
        elif self.txtblkShapeControl.blk_item is not None:
            target_blk = self.txtblkShapeControl.blk_item
        if target_blk is None:
            return
        idx = getattr(target_blk, 'idx', None)
        if idx is None:
            return
        n = len(self.textblk_item_list)
        if n < 2:
            return
        if action == 'position':
            self.open_quick_reorder_popup(idx)
            return
        if action == 'top':
            self.move_block_to_position(idx, 0)
        elif action == 'up':
            self.move_block_to_position(idx, idx - 1)
        elif action == 'down':
            self.move_block_to_position(idx, idx + 1)
        elif action == 'bottom':
            self.move_block_to_position(idx, n - 1)

    def open_quick_reorder_popup(self, badge_or_idx):
        # Spawn the QuickReorderInputPopup anchored on the target badge. The
        # popup is a child of the canvas viewport so it floats above the scene
        # without participating in QGraphicsScene event flow.
        #
        # badge_or_idx accepts either:
        #   * int -- the block idx (0-based) -- used by Ctrl+J and the badge
        #     context menu's "Move to position..." action (which emits its
        #     own idx).
        #   * TextBlockNumberBadge -- direct reference; we read .idx from it.
        # The dual signature keeps callers compact.
        if shared.HEADLESS:
            return
        if not self.canvas.textEditMode():
            return
        from .textblock_badge import TextBlockNumberBadge, QuickReorderInputPopup, QUICK_REORDER_W, QUICK_REORDER_H

        if isinstance(badge_or_idx, TextBlockNumberBadge):
            idx = badge_or_idx.idx
        else:
            try:
                idx = int(badge_or_idx)
            except (TypeError, ValueError):
                return
        n = len(self.textblk_item_list)
        if n == 0 or not (0 <= idx < n):
            return
        blk_item = self.textblk_item_list[idx]
        badge = getattr(blk_item, 'number_badge', None)
        if badge is None:
            return

        # Tear down any prior popup so we don't accumulate floating widgets if
        # the user fires Ctrl+J twice in a row.
        self._dismiss_quick_reorder_popup()

        gv = self.canvas.gv
        viewport = gv.viewport()
        if viewport is None:
            return

        # Anchor the popup at the badge's screen position. The badge sits in
        # scene coords on the parent block; mapping through the badge's
        # parentItem -> scene -> viewport keeps the popup glued to whatever
        # the user can see, even under zoom/pan.
        parent_item = badge.parentItem()
        badge_local_pos = badge.pos()
        if parent_item is not None:
            badge_scene_pos = parent_item.mapToScene(badge_local_pos)
        else:
            badge_scene_pos = badge_local_pos
        viewport_pt = gv.mapFromScene(badge_scene_pos)

        popup = QuickReorderInputPopup(viewport, idx, n)
        # Nudge the popup so it sits just below-right of the badge rather than
        # directly on top of it (the user still wants to see which block is
        # being moved). 12px down clears the badge's ~18px height from the
        # anchor point and the popup itself is 28px tall.
        x = viewport_pt.x()
        y = viewport_pt.y() + 12
        # Clamp inside the viewport so the popup never spawns offscreen when
        # the badge is near the canvas edge.
        max_x = max(0, viewport.width() - QUICK_REORDER_W)
        max_y = max(0, viewport.height() - QUICK_REORDER_H)
        x = max(0, min(x, max_x))
        y = max(0, min(y, max_y))
        popup.move(x, y)

        popup.submitted.connect(self._on_quick_reorder_submitted)
        popup.cancelled.connect(self._dismiss_quick_reorder_popup)
        self._quick_reorder_popup = popup
        self._quick_reorder_src_idx = idx
        popup.show()
        popup.raise_()
        popup.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_quick_reorder_submitted(self, target_0based: int):
        # Called from QuickReorderInputPopup.submitted. Drives the move via
        # the public mover so bounds checks run uniformly.
        src = getattr(self, '_quick_reorder_src_idx', None)
        # Tear down the popup first so move_block_to_position's eventual
        # selection updates don't fight with focus on a doomed widget.
        self._dismiss_quick_reorder_popup()
        if src is None:
            return
        self.move_block_to_position(src, target_0based)

    def _dismiss_quick_reorder_popup(self):
        # Idempotent close. Called from cancel paths and after a successful
        # submit. Safe to call when no popup exists.
        popup = getattr(self, '_quick_reorder_popup', None)
        if popup is not None:
            try:
                popup.blockSignals(True)
                popup.hide()
                popup.deleteLater()
            except Exception:
                pass
        self._quick_reorder_popup = None
        self._quick_reorder_src_idx = None

    def auto_sort_reading_order(self):
        # Build the desired permutation and feed it into the existing
        # RearrangeBlksCommand for an undoable, panel-syncing reorder. No-op
        # when there's nothing to sort.
        # Cancel any in-progress badge drag and dismiss any open quick-reorder
        # popup first: the indices about to change underneath them would
        # otherwise leave stale highlights / a stale src idx on the popup.
        self.cancel_badge_drag()
        n = len(self.textblk_item_list)
        if n < 2:
            return
        new_order = self._compute_reading_order(self.textblk_item_list)
        if new_order == list(range(n)):
            return
        ids_ori, ids_tgt = [], []
        for new_pos, old_pos in enumerate(new_order):
            if new_pos != old_pos:
                ids_ori.append(old_pos)
                ids_tgt.append(new_pos)
        if not ids_ori:
            return
        self.canvas.push_undo_command(RearrangeBlksCommand((ids_ori, ids_tgt), self))

    def _compute_reading_order(self, blk_list):
        # Manhwa convention: top-to-bottom primary, left-to-right within rows.
        # Two-pass approach:
        #   1. Sort by center-y to establish vertical ordering.
        #   2. Within rows (defined by overlap on the y axis with tolerance ~30%
        #      of the average block height), break ties by center-x.
        # This is robust to slightly misaligned blocks where naive cy sorting
        # would otherwise order side-by-side bubbles by a tiny y offset.
        n = len(blk_list)
        if n == 0:
            return []
        rects = []
        for blk_item in blk_list:
            br = blk_item.absBoundingRect(qrect=True)
            cx = br.x() + br.width() / 2.0
            cy = br.y() + br.height() / 2.0
            rects.append((cx, cy, br.height()))
        avg_h = sum(r[2] for r in rects) / max(1, n)
        # 30% of avg height tolerance: blocks closer than this on cy are
        # considered same-row and sorted L-to-R.
        tol = max(avg_h * 0.3, 1.0)
        # First pass: stable sort by cy ascending.
        order = sorted(range(n), key=lambda i: rects[i][1])
        # Second pass: walk groups whose cy fall within tolerance and resort
        # them by cx. We expand the group as long as the next item's cy is
        # within tol of the *first* item in the group (not the running median),
        # which keeps the grouping deterministic.
        result = []
        i = 0
        while i < n:
            j = i + 1
            base_cy = rects[order[i]][1]
            while j < n and abs(rects[order[j]][1] - base_cy) <= tol:
                j += 1
            group = order[i:j]
            group.sort(key=lambda idx: rects[idx][0])
            result.extend(group)
            i = j
        return result


def get_text_size(fm: QFontMetricsF, text: str) -> Tuple[int, int]:
    brt = fm.tightBoundingRect(text)
    br = fm.boundingRect(text)
    return int(np.ceil(fm.horizontalAdvance(text))), int(np.ceil(brt.height()))
    
def get_words_length_list(fm: QFontMetricsF, words: List[str]) -> List[int]:
    return [int(np.ceil(fm.horizontalAdvance(word))) for word in words]
