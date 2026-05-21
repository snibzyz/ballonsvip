import os.path as osp
import os, re, traceback, sys
from typing import List, Union
from pathlib import Path
import subprocess
from functools import partial
import time
import cv2

from tqdm import tqdm
from qtpy.QtWidgets import QAction, QFileDialog, QMenu, QHBoxLayout, QVBoxLayout, QApplication, QStackedWidget, QSplitter, QListWidget, QShortcut, QListWidgetItem, QMessageBox, QTextEdit, QPlainTextEdit, QProgressDialog, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QAbstractSpinBox
from qtpy.QtCore import Qt, QPoint, QSize, QEvent, Signal, QTimer
from qtpy.QtGui import QContextMenuEvent, QTextCursor, QGuiApplication, QIcon, QCloseEvent, QKeySequence, QKeyEvent, QPainter, QClipboard, QImage, QShowEvent, QFocusEvent

from utils.logger import logger as LOGGER
from utils.text_processing import is_cjk, full_len, half_len
from utils.textblock import TextBlock, TextAlignment
from utils import shared
from utils.message import create_error_dialog, create_info_dialog
from modules.translators.trans_chatgpt import GPTTranslator
from modules import GET_VALID_TEXTDETECTORS, GET_VALID_INPAINTERS, GET_VALID_TRANSLATORS, GET_VALID_OCR
from .misc import parse_stylesheet, set_html_family, QKEY, _modint, SHORTCUT_MOD_MASK, MOD_NONE, MOD_CTRL, MOD_SHIFT, MOD_ALT, MOD_CTRL_SHIFT
from utils.config import ProgramConfig, pcfg, save_config, text_styles, save_text_styles, load_textstyle_from, FontFormat
from utils.proj_imgtrans import ProjImgTrans
from .canvas import Canvas
from .configpanel import ConfigPanel
from .module_manager import ModuleManager
from .textedit_area import SourceTextEdit, SelectTextMiniMenu, TransTextEdit
from .textblock_badge import QuickReorderInputPopup
from .drawingpanel import DrawingPanel
from .scenetext_manager import SceneTextManager, TextPanel, PasteSrcItemsCommand
from .mainwindowbars import TitleBar, LeftBar, BottomBar
from .io_thread import ImgSaveThread, ImportDocThread, ExportDocThread
from .custom_widget import Widget, ViewWidget
from .global_search_widget import GlobalSearchWidget
from .textedit_commands import GlobalRepalceAllCommand
from .framelesswindow import FramelessWindow, FramelessMoveResize
from .drawing_commands import RunBlkTransCommand
from .keywordsubwidget import KeywordSubWidget
from . import shared_widget as SW
from .custom_widget import MessageBox, FrameLessMessageBox, ImgtransProgressMessageBox

class PageListView(QListWidget):

    reveal_file = Signal()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setIconSize(QSize(shared.PAGELIST_THUMBNAIL_SIZE, shared.PAGELIST_THUMBNAIL_SIZE))

    def contextMenuEvent(self, e: QContextMenuEvent):
        menu = QMenu()
        reveal_act = menu.addAction(self.tr('Reveal in File Explorer'))
        rst = menu.exec_(e.globalPos())

        if rst == reveal_act:
            self.reveal_file.emit()

        return super().contextMenuEvent(e)

mainwindow_cls = Widget if shared.HEADLESS else FramelessWindow
class MainWindow(mainwindow_cls):

    imgtrans_proj: ProjImgTrans = ProjImgTrans()
    save_on_page_changed = True
    opening_dir = False
    page_changing = False
    postprocess_mt_toggle = True

    translator = None

    restart_signal = Signal()
    create_errdialog = Signal(str, str, str)
    create_infodialog = Signal(dict)
    
    def __init__(self, app: QApplication, config: ProgramConfig, open_dir='', **exec_args) -> None:
        super().__init__()

        shared.create_errdialog_in_mainthread = self.create_errdialog.emit
        self.create_errdialog.connect(self.on_create_errdialog)
        shared.create_infodialog_in_mainthread = self.create_infodialog.emit
        self.create_infodialog.connect(self.on_create_infodialog)
        shared.register_view_widget = self.register_view_widget

        self.app = app
        self.backup_blkstyles = []
        self._run_imgtrans_wo_textstyle_update = False

        # Setup autosave timer (10s sweet spot: long enough to avoid UI hitches from rapid edits,
        # short enough to keep autosave protection useful).
        from qtpy.QtCore import QTimer
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.timeout.connect(self.on_autosave_timeout)
        self.autosave_timer.setInterval(10000)

        # In-flight guard: counts autosave-tagged items still pending in imsave_thread.
        # Prevents a new autosave round from stacking on top of an unfinished one
        # (which would cause queue pile-up and visible UI hitches).
        self._autosave_pending = 0
        # Re-entrancy guard for the deferred render step itself (between the
        # singleShot scheduling and the actual saveCurrentPage call running).
        self._autosave_running = False

        self.setupThread()
        self.setupUi()
        self.setupConfig()
        self.setupShortcuts()
        self._build_vk_shortcut_table()
        self.setupRegisterWidget()

        # Global key event filter for layout-independent shortcut dispatch.
        # Qt's QShortcut matches by produced character: on a Thai (Kedmanee)
        # layout the physical A key emits "ฟ", so the registered "A" shortcut
        # never fires. The existing per-shortcut Thai variants help for plain
        # base characters but not for combining marks (Ctrl+J -> Ctrl+่), and
        # the keyPressEvent fallback only runs when the event actually reaches
        # MainWindow -- which doesn't happen when the QGraphicsView (or any
        # focused child widget) consumes the key first.
        #
        # An application-level filter sees every QKeyEvent BEFORE Qt's normal
        # shortcut dispatch and widget routing, so we can match on
        # nativeVirtualKey() (the Windows VK_* code, identical across every
        # layout) and consume the event before it ever needs to bubble.
        self.app.installEventFilter(self)
        # self.showMaximized()
        FramelessMoveResize.toggleMaxState(self)
        self.setAcceptDrops(True)

        if open_dir != '' and osp.exists(open_dir):
            self.OpenProj(open_dir)
        elif pcfg.open_recent_on_startup:
            if len(self.leftBar.recent_proj_list) > 0:
                proj_dir = self.leftBar.recent_proj_list[0]
                if osp.exists(proj_dir):
                    self.OpenProj(proj_dir)

        if shared.HEADLESS:
            self.run_batch(**exec_args)

        if shared.ON_MACOS:
            # https://bugreports.qt.io/browse/QTBUG-133215
            self.hideSystemTitleBar()
            self.showMaximized()

    def setStyleSheet(self, styleSheet: str) -> None:
        self.imgtrans_progress_msgbox.setStyleSheet(styleSheet)
        self.export_doc_thread.progress_bar.setStyleSheet(styleSheet)
        self.import_doc_thread.progress_bar.setStyleSheet(styleSheet)
        return super().setStyleSheet(styleSheet)

    def setupThread(self):
        self.imsave_thread = ImgSaveThread()
        # Force QueuedConnection: signal is emitted from the worker run() loop, but the
        # ImgSaveThread instance lives on the main thread, so AutoConnection would resolve
        # to DirectConnection and race with the main-thread `_autosave_pending += 1`.
        self.imsave_thread.autosave_item_done.connect(self._on_autosave_item_done, Qt.ConnectionType.QueuedConnection)
        self.export_doc_thread = ExportDocThread()
        self.export_doc_thread.fin_io.connect(self.on_fin_export_doc)
        self.import_doc_thread = ImportDocThread(self)
        self.import_doc_thread.fin_io.connect(self.on_fin_import_doc)

    def _on_autosave_item_done(self):
        # Decrement guard; clamp at zero in case an exception path emitted extra signals.
        if self._autosave_pending > 0:
            self._autosave_pending -= 1

    def resetStyleSheet(self, reverse_icon: bool = False):
        theme = 'eva-dark' if pcfg.darkmode else 'eva-light'
        self.setStyleSheet(parse_stylesheet(theme, reverse_icon))

    def setupUi(self):
        screen_size = QGuiApplication.primaryScreen().geometry().size()
        self.setMinimumWidth(screen_size.width() // 2)
        self.configPanel = ConfigPanel(self)
        self.configPanel.trans_config_panel.show_pre_MT_keyword_window.connect(self.show_pre_MT_keyword_window)
        self.configPanel.trans_config_panel.show_MT_keyword_window.connect(self.show_MT_keyword_window)
        self.configPanel.trans_config_panel.show_OCR_keyword_window.connect(self.show_OCR_keyword_window)

        self.leftBar = LeftBar(self)
        self.leftBar.showPageListLabel.clicked.connect(self.pageLabelStateChanged)
        self.leftBar.imgTransChecked.connect(self.setupImgTransUI)
        self.leftBar.configChecked.connect(self.setupConfigUI)
        self.leftBar.globalSearchChecker.clicked.connect(self.on_set_gsearch_widget)
        self.leftBar.open_dir.connect(self.OpenProj)
        self.leftBar.open_json_proj.connect(self.openJsonProj)
        self.leftBar.reload_proj.connect(self.reloadCurrentProject)
        self.leftBar.save_proj.connect(self.manual_save)
        self.leftBar.export_doc.connect(self.on_export_doc)
        self.leftBar.import_doc.connect(self.on_import_doc)
        self.leftBar.export_src_txt.connect(lambda : self.on_export_txt(dump_target='source'))
        self.leftBar.export_trans_txt.connect(lambda : self.on_export_txt(dump_target='translation'))
        self.leftBar.export_src_md.connect(lambda : self.on_export_txt(dump_target='source', suffix='.md'))
        self.leftBar.export_trans_md.connect(lambda : self.on_export_txt(dump_target='translation', suffix='.md'))
        self.leftBar.import_trans_txt.connect(self.on_import_trans_txt)
        # Quick menu buttons
        self.leftBar.export_src_txt_clicked.connect(self.on_export_src_txt_quick)
        self.leftBar.import_trans_txt_clicked.connect(self.on_import_trans_txt_quick)
        self.leftBar.save_all_clicked.connect(self.saveAllPages)
        self.leftBar.reload_proj_clicked.connect(self.reloadCurrentProject)

        self.pageList = PageListView()
        self.pageList.reveal_file.connect(self.on_reveal_file)
        self.pageList.setHidden(True)
        self.pageList.currentItemChanged.connect(self.pageListCurrentItemChanged)

        self.leftStackWidget = QStackedWidget(self)
        self.leftStackWidget.addWidget(self.pageList)

        self.global_search_widget = GlobalSearchWidget(self.leftStackWidget)
        self.global_search_widget.req_update_pagetext.connect(self.on_req_update_pagetext)
        self.global_search_widget.req_move_page.connect(self.on_req_move_page)
        self.imsave_thread.img_writed.connect(self.global_search_widget.on_img_writed)
        self.global_search_widget.search_tree.result_item_clicked.connect(self.on_search_result_item_clicked)
        self.leftStackWidget.addWidget(self.global_search_widget)
        
        self.centralStackWidget = QStackedWidget(self)
        
        self.titleBar = TitleBar(self)
        self.titleBar.closebtn_clicked.connect(self.on_closebtn_clicked)
        self.titleBar.display_lang_changed.connect(self.on_display_lang_changed)
        self.bottomBar = BottomBar(self)
        self.bottomBar.textedit_checkchanged.connect(self.setTextEditMode)
        self.bottomBar.paintmode_checkchanged.connect(self.setPaintMode)
        self.bottomBar.textblock_checkchanged.connect(self.setTextBlockMode)

        mainHLayout = QHBoxLayout()
        mainHLayout.addWidget(self.leftBar)
        mainHLayout.addWidget(self.centralStackWidget)
        mainHLayout.setContentsMargins(0, 0, 0, 0)
        mainHLayout.setSpacing(0)

        # set up canvas
        SW.canvas = self.canvas = Canvas()
        self.canvas.imgtrans_proj = self.imgtrans_proj
        self.canvas.gv.hide_canvas.connect(self.onHideCanvas)
        self.canvas.proj_savestate_changed.connect(self.on_savestate_changed)
        # Connect autosave timer to save state changes
        self.canvas.proj_savestate_changed.connect(self.on_projstate_changed_for_autosave)
        self.canvas.textstack_changed.connect(self.on_textstack_changed)
        self.canvas.run_blktrans.connect(self.on_run_blktrans)
        self.canvas.drop_open_folder.connect(self.dropOpenDir)
        self.canvas.originallayer_trans_slider = self.bottomBar.originalSlider
        self.canvas.textlayer_trans_slider = self.bottomBar.textlayerSlider
        self.canvas.copy_src_signal.connect(self.on_copy_src)
        self.canvas.paste_src_signal.connect(self.on_paste_src)

        self.bottomBar.originalSlider.valueChanged.connect(self.canvas.setOriginalTransparencyBySlider)
        self.bottomBar.textlayerSlider.valueChanged.connect(self.canvas.setTextLayerTransparencyBySlider)
        
        self.drawingPanel = DrawingPanel(self.canvas, self.configPanel.inpaint_config_panel)
        self.textPanel = TextPanel(self.app)
        self.textPanel.formatpanel.foldTextBtn.checkStateChanged.connect(self.fold_textarea)
        self.textPanel.formatpanel.sourceBtn.checkStateChanged.connect(self.show_source_text)
        self.textPanel.formatpanel.transBtn.checkStateChanged.connect(self.show_trans_text)
        self.textPanel.formatpanel.textstyle_panel.export_style.connect(self.export_tstyles)
        self.textPanel.formatpanel.textstyle_panel.import_style.connect(self.import_tstyles)

        self.ocrSubWidget = KeywordSubWidget(self.tr("Keyword substitution for source text"))
        self.ocrSubWidget.setParent(self)
        self.ocrSubWidget.setWindowFlags(Qt.WindowType.Window)
        self.ocrSubWidget.hide()
        self.mtPreSubWidget = KeywordSubWidget(self.tr("Keyword substitution for machine translation source text"))
        self.mtPreSubWidget.setParent(self)
        self.mtPreSubWidget.setWindowFlags(Qt.WindowType.Window)
        self.mtPreSubWidget.hide()
        self.mtSubWidget = KeywordSubWidget(self.tr("Keyword substitution for machine translation"))
        self.mtSubWidget.setParent(self)
        self.mtSubWidget.setWindowFlags(Qt.WindowType.Window)
        self.mtSubWidget.hide()

        SW.st_manager = self.st_manager = SceneTextManager(self.app, self, self.canvas, self.textPanel)
        self.st_manager.new_textblk.connect(self.canvas.search_widget.on_new_textblk)
        self.canvas.search_widget.pairwidget_list = self.st_manager.pairwidget_list
        self.canvas.search_widget.textblk_item_list = self.st_manager.textblk_item_list
        self.canvas.search_widget.replace_one.connect(self.st_manager.on_page_replace_one)
        self.canvas.search_widget.replace_all.connect(self.st_manager.on_page_replace_all)

        # comic trans pannel
        self.rightComicTransStackPanel = QStackedWidget(self)
        self.rightComicTransStackPanel.addWidget(self.drawingPanel)
        self.rightComicTransStackPanel.addWidget(self.textPanel)
        self.rightComicTransStackPanel.currentChanged.connect(self.on_transpanel_changed)

        self.comicTransSplitter = QSplitter(Qt.Orientation.Horizontal)
        self.comicTransSplitter.addWidget(self.leftStackWidget)
        self.comicTransSplitter.addWidget(self.canvas.gv)
        self.comicTransSplitter.addWidget(self.rightComicTransStackPanel)

        self.centralStackWidget.addWidget(self.comicTransSplitter)
        self.centralStackWidget.addWidget(self.configPanel)

        self.selectext_minimenu = self.st_manager.selectext_minimenu = SelectTextMiniMenu(self.app, self)
        self.selectext_minimenu.block_current_editor.connect(self.st_manager.on_block_current_editor)
        self.selectext_minimenu.hide()

        mainVBoxLayout = QVBoxLayout(self)
        mainVBoxLayout.addWidget(self.titleBar)
        mainVBoxLayout.addLayout(mainHLayout)
        mainVBoxLayout.addWidget(self.bottomBar)
        margin = mainVBoxLayout.contentsMargins()
        self.main_margin = margin
        mainVBoxLayout.setContentsMargins(0, 0, 0, 0)
        mainVBoxLayout.setSpacing(0)

        self.mainvlayout = mainVBoxLayout
        self.comicTransSplitter.setStretchFactor(0, 1)
        self.comicTransSplitter.setStretchFactor(1, 10)
        self.comicTransSplitter.setStretchFactor(2, 1)
        self.imgtrans_progress_msgbox = ImgtransProgressMessageBox()
        self.resetStyleSheet()

    def on_finish_setdetector(self):
        module_manager = self.module_manager
        if module_manager.textdetector is not None:
            name = module_manager.textdetector.name
            pcfg.module.textdetector = name
            self.configPanel.detect_config_panel.setDetector(name)
            self.bottomBar.textdet_selector.setSelectedValue(name)
            LOGGER.info('Text detector set to {}'.format(name))

    def on_finish_setocr(self):
        module_manager = self.module_manager
        if module_manager.ocr is not None:
            name = module_manager.ocr.name
            pcfg.module.ocr = name
            self.configPanel.ocr_config_panel.setOCR(name)
            self.bottomBar.ocr_selector.setSelectedValue(name)
            LOGGER.info('OCR set to {}'.format(name))

    def on_finish_setinpainter(self):
        module_manager = self.module_manager
        if module_manager.inpainter is not None:
            name = module_manager.inpainter.name
            pcfg.module.inpainter = name
            self.configPanel.inpaint_config_panel.setInpainter(name)
            self.bottomBar.inpaint_selector.setSelectedValue(name)
            LOGGER.info('Inpainter set to {}'.format(name))

    def on_finish_settranslator(self):
        module_manager = self.module_manager
        translator = module_manager.translator
        if translator is not None:
            name = translator.name
            pcfg.module.translator = name
            self.bottomBar.trans_selector.finishSetTranslator(translator)
            self.configPanel.trans_config_panel.finishSetTranslator(translator)
            LOGGER.info('Translator set to {}'.format(name))
        else:
            LOGGER.error('invalid translator')
        
    def on_enable_module(self, idx, checked):
        if idx == 0:
            pcfg.module.enable_detect = checked
            self.bottomBar.textdet_selector.setVisible(checked)
        elif idx == 1:
            pcfg.module.enable_ocr = checked
            self.bottomBar.ocr_selector.setVisible(checked)
        elif idx == 2:
            pcfg.module.enable_translate = checked
            self.bottomBar.trans_selector.setVisible(checked)
        elif idx == 3:
            pcfg.module.enable_inpaint = checked
            self.bottomBar.inpaint_selector.setVisible(checked)
        pcfg.module.update_finish_code()

    def setupConfig(self):

        self.bottomBar.originalSlider.setValue(int(pcfg.original_transparency * 100))
        self.bottomBar.trans_selector.selector.addItems(GET_VALID_TRANSLATORS())
        self.bottomBar.ocr_selector.selector.addItems(GET_VALID_OCR())
        self.bottomBar.textdet_selector.selector.addItems(GET_VALID_TEXTDETECTORS())
        self.bottomBar.textdet_selector.selector.currentTextChanged.connect(self.on_textdet_changed)
        self.bottomBar.inpaint_selector.selector.addItems(GET_VALID_INPAINTERS())
        self.bottomBar.inpaint_selector.selector.currentTextChanged.connect(self.on_inpaint_changed)
        self.bottomBar.trans_selector.cfg_clicked.connect(self.to_trans_config)
        self.bottomBar.trans_selector.selector.currentTextChanged.connect(self.on_trans_changed)
        self.bottomBar.trans_selector.tgt_selector.currentTextChanged.connect(self.on_trans_tgt_changed)
        self.bottomBar.trans_selector.src_selector.currentTextChanged.connect(self.on_trans_src_changed)
        self.bottomBar.textdet_selector.cfg_clicked.connect(self.to_detect_config)
        self.bottomBar.inpaint_selector.cfg_clicked.connect(self.to_inpaint_config)
        self.bottomBar.ocr_selector.cfg_clicked.connect(self.to_ocr_config)
        self.bottomBar.ocr_selector.selector.currentTextChanged.connect(self.on_ocr_changed)
        self.bottomBar.textdet_selector.setVisible(pcfg.module.enable_detect)
        self.bottomBar.ocr_selector.setVisible(pcfg.module.enable_ocr)
        self.bottomBar.trans_selector.setVisible(pcfg.module.enable_translate)
        self.bottomBar.inpaint_selector.setVisible(pcfg.module.enable_inpaint)

        self.configPanel.trans_config_panel.target_combobox.currentTextChanged.connect(self.on_trans_tgt_changed)
        self.configPanel.trans_config_panel.source_combobox.currentTextChanged.connect(self.on_trans_src_changed)

        self.drawingPanel.maskTransperancySlider.setValue(int(pcfg.mask_transparency * 100))
        self.leftBar.initRecentProjMenu(pcfg.recent_proj_list)
        self.leftBar.showPageListLabel.setChecked(pcfg.show_page_list)
        self.updatePageList()
        self.leftBar.save_config.connect(self.save_config)
        self.leftBar.imgTransChecker.setChecked(True)
        self.st_manager.formatpanel.global_format = pcfg.global_fontformat
        self.st_manager.formatpanel.set_active_format(pcfg.global_fontformat)
        
        self.rightComicTransStackPanel.setHidden(True)
        self.st_manager.setTextEditMode(False)
        self.st_manager.formatpanel.foldTextBtn.setChecked(pcfg.fold_textarea)
        self.st_manager.formatpanel.transBtn.setCheckState(pcfg.show_trans_text)
        self.st_manager.formatpanel.sourceBtn.setCheckState(pcfg.show_source_text)
        self.fold_textarea(pcfg.fold_textarea)
        self.show_trans_text(pcfg.show_trans_text)
        self.show_source_text(pcfg.show_source_text)

        self.module_manager = module_manager = ModuleManager(self.imgtrans_proj)
        module_manager.finish_translate_page.connect(self.finishTranslatePage)
        module_manager.imgtrans_pipeline_finished.connect(self.on_imgtrans_pipeline_finished)
        module_manager.page_trans_finished.connect(self.on_pagtrans_finished)
        module_manager.setupThread(self.configPanel, self.imgtrans_progress_msgbox, self.ocr_postprocess, self.translate_preprocess, self.translate_postprocess)
        module_manager.progress_msgbox.showed.connect(self.on_imgtrans_progressbox_showed)
        module_manager.blktrans_pipeline_finished.connect(self.on_blktrans_finished)
        module_manager.imgtrans_thread.post_process_mask = self.drawingPanel.rectPanel.post_process_mask
        module_manager.inpaint_thread.finish_set_module.connect(self.on_finish_setinpainter)
        module_manager.translate_thread.finish_set_module.connect(self.on_finish_settranslator)
        module_manager.textdetect_thread.finish_set_module.connect(self.on_finish_setdetector)
        module_manager.ocr_thread.finish_set_module.connect(self.on_finish_setocr)
        module_manager.setTextDetector()
        module_manager.setOCR()
        module_manager.setTranslator()
        module_manager.setInpainter()

        self.leftBar.run_imgtrans_clicked.connect(self.run_imgtrans)

        self.titleBar.darkModeAction.setChecked(pcfg.darkmode)

        self.drawingPanel.set_config(pcfg.drawpanel)
        self.drawingPanel.initDLModule(module_manager)

        self.global_search_widget.imgtrans_proj = self.imgtrans_proj
        self.global_search_widget.setupReplaceThread(self.st_manager.pairwidget_list, self.st_manager.textblk_item_list)
        self.global_search_widget.replace_thread.finished.connect(self.on_global_replace_finished)

        self.configPanel.setupConfig()
        self.configPanel.save_config.connect(self.save_config)
        self.configPanel.reload_textstyle.connect(self.load_textstyle_from_proj_dir)
        self.configPanel.show_only_custom_font.connect(self.on_show_only_custom_font)
        if pcfg.let_show_only_custom_fonts_flag:
            self.on_show_only_custom_font(True)

        textblock_mode = pcfg.imgtrans_textblock
        if pcfg.imgtrans_textedit:
            if textblock_mode:
                self.bottomBar.textblockChecker.setChecked(True)
            self.bottomBar.texteditChecker.click()
        elif pcfg.imgtrans_paintmode:
            self.bottomBar.paintChecker.click()

        self.textPanel.formatpanel.textstyle_panel.initStyles(text_styles)

        self.canvas.search_widget.whole_word_toggle.setChecked(pcfg.fsearch_whole_word)
        self.canvas.search_widget.case_sensitive_toggle.setChecked(pcfg.fsearch_case)
        self.canvas.search_widget.regex_toggle.setChecked(pcfg.fsearch_regex)
        self.canvas.search_widget.range_combobox.setCurrentIndex(pcfg.fsearch_range)
        self.global_search_widget.whole_word_toggle.setChecked(pcfg.gsearch_whole_word)
        self.global_search_widget.case_sensitive_toggle.setChecked(pcfg.gsearch_case)
        self.global_search_widget.regex_toggle.setChecked(pcfg.gsearch_regex)
        self.global_search_widget.range_combobox.setCurrentIndex(pcfg.gsearch_range)

        if self.rightComicTransStackPanel.isHidden():
            self.setPaintMode()

        try:
            self.ocrSubWidget.loadCfgSublist(pcfg.ocr_sublist)
        except Exception as e:
            LOGGER.error(traceback.format_exc())
            pcfg.ocr_sublist = []
            self.ocrSubWidget.loadCfgSublist(pcfg.ocr_sublist)

        try:
            self.mtPreSubWidget.loadCfgSublist(pcfg.pre_mt_sublist)
        except Exception as e:
            LOGGER.error(traceback.format_exc())
            pcfg.pre_mt_sublist = []
            self.mtPreSubWidget.loadCfgSublist(pcfg.pre_mt_sublist)

        try:
            self.mtSubWidget.loadCfgSublist(pcfg.mt_sublist)
        except Exception as e:
            LOGGER.error(traceback.format_exc())
            pcfg.mt_sublist = []
            self.mtSubWidget.loadCfgSublist(pcfg.mt_sublist)

    def setupImgTransUI(self):
        self.centralStackWidget.setCurrentIndex(0)
        if self.leftBar.needleftStackWidget():
            self.leftStackWidget.show()
        else:
            self.leftStackWidget.hide()

    def setupConfigUI(self):
        self.centralStackWidget.setCurrentIndex(1)

    def set_display_lang(self, lang: str):
        self.retranslateUI()

    def OpenProj(self, proj_path: str):
        if osp.isdir(proj_path):
            self.openDir(proj_path)
        else:
            self.openJsonProj(proj_path)
        
        if pcfg.let_textstyle_indep_flag and not shared.HEADLESS:
            self.load_textstyle_from_proj_dir(from_proj=True)

    def load_textstyle_from_proj_dir(self, from_proj=False):
        if from_proj:
            text_style_path = osp.join(self.imgtrans_proj.directory, 'textstyles.json')
        else:
            text_style_path = 'config/textstyles/default.json'
        if osp.exists(text_style_path):
            load_textstyle_from(text_style_path)
            self.textPanel.formatpanel.textstyle_panel.setStyles(text_styles)
        else:
            pcfg.text_styles_path = text_style_path
            save_text_styles()

    def on_show_only_custom_font(self, only_custom: bool):
        if only_custom:
            font_list = shared.CUSTOM_FONTS
        else:
            font_list = shared.FONT_FAMILIES
        self.textPanel.formatpanel.familybox.update_font_list(font_list)

    def openDir(self, directory: str):
        try:
            self.opening_dir = True
            # 在加载项目前检查并生成TIF文件的预览图
            self.generate_tif_thumbnails(directory)
            # 重新加载项目，此时应该只加载预览图
            self.imgtrans_proj.load(directory)
            self.st_manager.clearSceneTextitems()
            self.titleBar.setTitleContent(osp.basename(directory))
            self.updatePageList()
            self.opening_dir = False
        except Exception as e:
            self.opening_dir = False
            create_error_dialog(e, self.tr('Failed to load project ') + directory)
            return

    def generate_tif_thumbnails(self, directory: str):
        """
        为目录中的TIF文件生成预览图，并确保只加载预览图
        """
        try:
            from utils.io_utils import create_thumbnail, find_tif_files
            # 查找目录中的所有TIF文件
            tif_files = find_tif_files(directory)
            
            # 为每个TIF文件生成预览图
            for tif_file in tif_files:
                tif_path = osp.join(directory, tif_file)
                # 检查是否已经存在对应的预览图
                base_path = Path(tif_path)
                thumb_path = base_path.parent / f"{base_path.stem}_thumb.jpg"
                
                # 如果预览图不存在，则生成预览图
                if not osp.exists(thumb_path):
                    create_thumbnail(tif_path, max_width=1000)
                    
        except Exception as e:
            LOGGER.error(f"Failed to generate TIF thumbnails: {e}")
        
    def dropOpenDir(self, directory: str):
        if isinstance(directory, str) and osp.exists(directory):
            self.leftBar.updateRecentProjList(directory)
            self.OpenProj(directory)

    def openJsonProj(self, json_path: str):
        original_save_on_page_changed = self.save_on_page_changed
        try:
            self.opening_dir = True
            self.save_on_page_changed = False
            self.imgtrans_proj.load_from_json(json_path)
            self.st_manager.clearSceneTextitems()
            self.canvas.clear_undostack(update_saved_step=True)
            self.leftBar.updateRecentProjList(self.imgtrans_proj.proj_path)
            self.updatePageList()
            if self.imgtrans_proj.current_img in self.imgtrans_proj.pages:
                current_idx = self.imgtrans_proj.current_idx
                if current_idx >= 0:
                    self.pageList.setCurrentRow(current_idx)
                self.canvas.updateCanvas()
                self.st_manager.updateSceneTextitems()
            self.titleBar.setTitleContent(osp.basename(self.imgtrans_proj.proj_path))
            self.opening_dir = False
        except Exception as e:
            self.opening_dir = False
            create_error_dialog(e, self.tr('Failed to load project from') + json_path)
        finally:
            self.save_on_page_changed = original_save_on_page_changed

    def reloadCurrentProject(self):
        if self.imgtrans_proj.directory is None:
            return

        current_img = self.imgtrans_proj.current_img
        proj_path = self.imgtrans_proj.proj_path
        original_save_on_page_changed = self.save_on_page_changed
        try:
            self.opening_dir = True
            self.save_on_page_changed = False
            self.st_manager.clearSceneTextitems()
            self.canvas.clear_undostack(update_saved_step=True)
            self.canvas.clear_text_stack()

            if proj_path is not None and proj_path.lower().endswith('.json') and osp.exists(proj_path):
                self.imgtrans_proj.load_from_json(proj_path)
                self.leftBar.updateRecentProjList(self.imgtrans_proj.proj_path)
                title = osp.basename(self.imgtrans_proj.proj_path)
            else:
                self.generate_tif_thumbnails(self.imgtrans_proj.directory)
                self.imgtrans_proj.load(self.imgtrans_proj.directory)
                title = osp.basename(self.imgtrans_proj.directory)

            if current_img in self.imgtrans_proj.pages:
                self.imgtrans_proj.set_current_img(current_img)

            self.updatePageList()
            if self.imgtrans_proj.current_img in self.imgtrans_proj.pages:
                current_idx = self.imgtrans_proj.current_idx
                if current_idx >= 0:
                    self.pageList.setCurrentRow(current_idx)
                self.canvas.updateCanvas()
                self.st_manager.updateSceneTextitems()
            self.titleBar.setTitleContent(title)
        except Exception as e:
            create_error_dialog(e, self.tr('Failed to reload current project'))
        finally:
            self.opening_dir = False
            self.save_on_page_changed = original_save_on_page_changed
        
    def updatePageList(self):
        if self.pageList.count() != 0:
            self.pageList.clear()
        if len(self.imgtrans_proj.pages) >= shared.PAGELIST_THUMBNAIL_MAXNUM:
            item_func = lambda imgname: QListWidgetItem(imgname)
        else:
            item_func = lambda imgname:\
                QListWidgetItem(QIcon(osp.join(self.imgtrans_proj.directory, imgname)), imgname)
        for imgname in self.imgtrans_proj.pages:
            lstitem =  item_func(imgname)
            self.pageList.addItem(lstitem)
            if imgname == self.imgtrans_proj.current_img:
                self.pageList.setCurrentItem(lstitem)

    def pageLabelStateChanged(self):
        setup = self.leftBar.showPageListLabel.isChecked()
        if setup:
            if self.leftStackWidget.isHidden():
                self.leftStackWidget.show()
            if self.leftBar.globalSearchChecker.isChecked():
                self.leftBar.globalSearchChecker.setChecked(False)
            self.leftStackWidget.setCurrentWidget(self.pageList)
        else:
            self.leftStackWidget.hide()
        pcfg.show_page_list = setup
        save_config()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        return super().keyPressEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        """Auto-select canvas when window is shown"""
        super().showEvent(event)
        # Multi-stage focus restore on first show / unminimize. Each retry uses
        # the shared idle check so a real text editor is never robbed.
        if hasattr(self, 'canvas'):
            def activate_and_restore():
                self.activateWindow()
                QApplication.setActiveWindow(self)
                self._restore_canvas_focus_if_idle()
            QTimer.singleShot(0, activate_and_restore)
            QTimer.singleShot(100, activate_and_restore)
            QTimer.singleShot(200, activate_and_restore)

    def closeEvent(self, event: QCloseEvent) -> None:
        # Check if there are unsaved changes
        if not self.imgtrans_proj.is_empty:
            has_unsaved_changes = (self.canvas.projstate_unsaved or 
                                  self.canvas.text_change_unsaved() or 
                                  self.canvas.draw_change_unsaved())
            
            if has_unsaved_changes:
                # Ask user if they want to save
                msg = QMessageBox(self)
                msg.setWindowTitle(self.tr('Unsaved Changes'))
                msg.setText(self.tr('You have unsaved changes. Do you want to save before closing?'))
                msg.setStandardButtons(QMessageBox.StandardButton.Save | 
                                      QMessageBox.StandardButton.Discard | 
                                      QMessageBox.StandardButton.Cancel)
                msg.setDefaultButton(QMessageBox.StandardButton.Save)
                msg.setIcon(QMessageBox.Icon.Warning)
                
                ret = msg.exec_()
                
                if ret == QMessageBox.StandardButton.Cancel:
                    # User cancelled, don't close
                    event.ignore()
                    return
                elif ret == QMessageBox.StandardButton.Save:
                    # User wants to save
                    self.conditional_save(keep_exist_as_backup=True)
            else:
                # No unsaved changes, just save normally
                self.conditional_save(keep_exist_as_backup=True)
        
        # Wait for save thread to finish. QThread.wait() blocks on an OS
        # primitive instead of a 100ms Python poll, so the close path is
        # noticeably snappier when save is already done; the 5 s timeout
        # caps the worst case and prevents an indefinite hang if the
        # writer thread is stuck on disk.
        if self.imsave_thread.isRunning():
            self.imsave_thread.wait(5000)
        self.st_manager.hovering_transwidget = None
        self.st_manager.blockSignals(True)
        self.canvas.prepareClose()
        self.save_config()
        return super().closeEvent(event)

    def _focus_widget_is_text_input(self, focus_widget):
        # Whitelist of widgets where keystrokes mean "user is typing", so the
        # focus-restore path must not steal focus from them.
        # Editable QComboBox / spinbox in edit mode delegate the keyboard to an
        # internal QLineEdit, so the QLineEdit branch handles them. We also
        # match the popup widget directly since editable=False combos still
        # absorb arrow keys when popped.
        # Imports are at module scope -- this function fires on every key event.
        if focus_widget is None:
            return False
        if isinstance(focus_widget, (SourceTextEdit, TransTextEdit, QuickReorderInputPopup)):
            return True
        if isinstance(focus_widget, (QLineEdit, QTextEdit, QPlainTextEdit)):
            # Only treat as "user typing" when the field is actually editable.
            # ReadOnly QLineEdit/QTextEdit (e.g. result viewers) shouldn't trap
            # the canvas shortcuts.
            try:
                if focus_widget.isReadOnly():
                    return False
            except Exception:
                pass
            return True
        if isinstance(focus_widget, QAbstractSpinBox):
            try:
                if focus_widget.isReadOnly():
                    return False
            except Exception:
                pass
            return True
        if isinstance(focus_widget, QComboBox):
            # Editable combobox => user is typing in its line edit. Non-editable
            # combos navigate by arrow keys but don't conflict with our A/D/W/N
            # shortcuts (they trigger only as bare letters), so we leave focus
            # alone only when a popup is visible.
            try:
                if focus_widget.isEditable():
                    return True
                if focus_widget.view() is not None and focus_widget.view().isVisible():
                    return True
            except Exception:
                pass
            return False
        return False

    def _restore_canvas_focus_if_idle(self):
        # Move focus back to the graphics view unless the user is actively
        # typing. Cheap to call repeatedly: returns early when focus is already
        # correct or when a real text input owns it.
        # Wrapped in try/except because deferred timers can fire after a child
        # widget has been Qt-deleted during a page rebuild (RuntimeError:
        # "wrapped C/C++ object has been deleted"). A silent skip is the right
        # behaviour: focus will settle naturally on the next user interaction.
        try:
            if not hasattr(self, 'canvas') or self.canvas is None:
                return
            gv = getattr(self.canvas, 'gv', None)
            if gv is None:
                return
            focus_widget = QApplication.focusWidget()
            if self._focus_widget_is_text_input(focus_widget):
                return
            if gv.hasFocus():
                return
            gv.setFocus()
        except RuntimeError:
            return
        except Exception:
            return

    def changeEvent(self, event: QEvent):
        if event.type() == QEvent.Type.WindowStateChange:
            if self.windowState() & Qt.WindowState.WindowMaximized:
                if not shared.ON_MACOS:
                    self.titleBar.maxBtn.setChecked(True)
        elif event.type() == QEvent.Type.ActivationChange:
            self.canvas.on_activation_changed()
            # Restore canvas focus when window is activated. Windows delays the
            # final focus settle after Alt+Tab / taskbar click, and the timing
            # varies by compositor state, so we kick off three retries at 0ms,
            # 50ms, and 200ms instead of relying on a single 50ms shot. Each
            # retry self-checks against _focus_widget_is_text_input so a real
            # text editor never gets robbed.
            if self.isActiveWindow():
                self._restore_canvas_focus_if_idle()
                QTimer.singleShot(0, self._restore_canvas_focus_if_idle)
                QTimer.singleShot(50, self._restore_canvas_focus_if_idle)
                QTimer.singleShot(200, self._restore_canvas_focus_if_idle)

        super().changeEvent(event)

    # focusInEvent override removed: ActivationChange in changeEvent already
    # covers Alt-Tab / taskbar return cases via the multi-stage timer, and the
    # extra focusInEvent path was firing during programmatic widget rebuilds
    # (page change, pool reuse) which races with Qt-internal focus dispatch
    # and can crash the renderer on Windows.

    def eventFilter(self, obj, event):
        # Installed on QApplication: catches every QKeyEvent before Qt's
        # shortcut dispatch and widget routing. We use this exclusively to
        # turn nativeVirtualKey() into a shortcut, which keeps every binding
        # working on any keyboard layout (Thai, Russian, ...). All other
        # events fall through to default handling.
        try:
            if event.type() == QEvent.Type.KeyPress:
                if self._dispatch_shortcut_by_vk(event):
                    return True
        except Exception:
            # Never let a filter fault break global event flow.
            pass
        return super().eventFilter(obj, event)

    def _build_vk_shortcut_table(self):
        # (modifiers_int, native_vk) -> zero-arg callable.
        # Modifier values come from ui.misc pre-computed constants
        # (`int(Qt.KeyboardModifier.X)` raises on PyQt6 6.x, so we go via
        # the .value accessor). native_vk is the Windows VK_* code returned
        # by QKeyEvent.nativeVirtualKey() -- position-based, identical
        # across every keyboard layout on Windows.
        NONE = MOD_NONE
        CTRL = MOD_CTRL
        SHIFT = MOD_SHIFT
        ALT = MOD_ALT
        CTRL_SHIFT = MOD_CTRL_SHIFT

        scale_up = lambda: self.canvas.gv.scale_up_signal.emit()
        scale_down = lambda: self.canvas.gv.scale_down_signal.emit()

        self._vk_shortcut_table = {
            # Plain letters / symbols
            (NONE, 0x41): self.shortcutBefore,                              # A
            (NONE, 0x44): self.shortcutNext,                                # D
            (NONE, 0x57): self.shortcutTextblock,                           # W
            (NONE, 0x4E): self.shortcutToggleNumberBadge,                   # N
            (NONE, 0x50): self.shortcutDrawboard,                           # P
            (NONE, 0x54): self.shortcutTextedit,                            # T
            (NONE, 0x48): lambda: self.drawingPanel.shortcutSetCurrentToolByName('hand'),    # H
            (NONE, 0x52): lambda: self.drawingPanel.shortcutSetCurrentToolByName('rect'),    # R
            (NONE, 0x4A): lambda: self.drawingPanel.shortcutSetCurrentToolByName('inpaint'), # J
            (NONE, 0x42): lambda: self.drawingPanel.shortcutSetCurrentToolByName('pen'),     # B
            (NONE, 0xDB): self.drawingPanel.on_decre_pensize,               # [
            (NONE, 0xDD): self.drawingPanel.on_incre_pensize,               # ]
            (NONE, 0x20): self.shortcutSpace,                               # Space
            (NONE, 0x1B): self.shortcutEscape,                              # Esc
            (NONE, 0x2E): self.shortcutDelete,                              # Delete

            # Ctrl chords
            (CTRL, 0x41): self.shortcutSelectAll,                           # Ctrl+A
            (CTRL, 0x42): self.shortcutBold,                                # Ctrl+B
            (CTRL, 0x44): self.shortcutCtrlD,                               # Ctrl+D
            (CTRL, 0x45): self.shortcutOCR,                                 # Ctrl+E
            (CTRL, 0x46): self.on_page_search,                              # Ctrl+F
            (CTRL, 0x47): self.on_global_search,                            # Ctrl+G
            (CTRL, 0x49): self.shortcutItalic,                              # Ctrl+I
            (CTRL, 0x4A): self.shortcutQuickReorder,                        # Ctrl+J
            (CTRL, 0x4F): self.leftBar.onOpenFolder,                        # Ctrl+O
            (CTRL, 0x52): self.reloadCurrentProject,                        # Ctrl+R
            (CTRL, 0x53): self.manual_save,                                 # Ctrl+S
            (CTRL, 0x55): self.shortcutUnderline,                           # Ctrl+U
            (CTRL, 0x59): self.on_redo,                                     # Ctrl+Y
            (CTRL, 0x5A): self.on_undo,                                     # Ctrl+Z
            (CTRL, 0xBB): scale_up,                                         # Ctrl+=
            (CTRL, 0xBD): scale_down,                                       # Ctrl+-

            # Ctrl+Shift chords. Ctrl++ (Shift+= on US) also zooms in.
            (CTRL_SHIFT, 0xBB): scale_up,                                   # Ctrl++
            (CTRL_SHIFT, 0x4D): self.on_open_merge_tool,                    # Ctrl+Shift+M
            (CTRL_SHIFT, 0x52): self.shortcutAutoSortReadingOrder,          # Ctrl+Shift+R

            # Alt chords (navigation keys are layout-independent anyway,
            # but routing through the same table keeps behaviour uniform).
            (ALT, 0x26): self.shortcutMoveBlockUp,                          # Alt+Up
            (ALT, 0x28): self.shortcutMoveBlockDown,                        # Alt+Down
            (ALT, 0x24): self.shortcutMoveBlockTop,                         # Alt+Home
            (ALT, 0x23): self.shortcutMoveBlockBottom,                      # Alt+End
        }

    def _dispatch_shortcut_by_vk(self, event) -> bool:
        # Layout-independent shortcut dispatch. Invoked from the
        # QApplication-level eventFilter so it runs BEFORE Qt's character-
        # based QShortcut matching -- which is what breaks on non-Latin
        # layouts (Thai 'ก' produced by physical D never matches "Ctrl+D").
        # Returns True when the event has been consumed.
        fw = QApplication.focusWidget()
        if self._focus_widget_is_text_input(fw):
            return False

        table = getattr(self, '_vk_shortcut_table', None)
        if table is None:
            return False

        vk = event.nativeVirtualKey()
        if vk == 0:
            return False

        mods = _modint(event.modifiers()) & SHORTCUT_MOD_MASK
        handler = table.get((mods, vk))
        if handler is None:
            return False
        try:
            handler()
        except Exception:
            LOGGER.exception('shortcut handler failed for vk=0x%X mods=0x%X', vk, mods)
            return False
        return True

    def keyPressEvent(self, event):
        # Belt-and-braces fallback for the rare case where the
        # QApplication-level eventFilter didn't see this KeyPress (e.g. a
        # custom widget that intercepts and re-posts events). Same table,
        # same dispatch — see _dispatch_shortcut_by_vk for the full
        # rationale on why we route by nativeVirtualKey().
        try:
            if self._dispatch_shortcut_by_vk(event):
                event.accept()
                return
        except Exception:
            pass
        return super().keyPressEvent(event)

    def retranslateUI(self):
        # according to https://stackoverflow.com/questions/27635068/how-to-retranslate-dynamically-created-widgets
        # we got to do it manually ... I'd rather restart the program
        msg = QMessageBox()
        msg.setText(self.tr('Restart to apply changes? \n'))
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        ret = msg.exec_()
        if ret == QMessageBox.StandardButton.Yes:
            self.restart_signal.emit()

    def save_config(self):
        save_config()

    def onHideCanvas(self):
        self.canvas.clearToolStates()

    def conditional_save(self, keep_exist_as_backup=False):
        if not self.opening_dir:
            update_scene_text = True
            save_proj = True
            save_rst_only = False
            self.saveCurrentPage(update_scene_text, save_proj, restore_interface=True, save_rst_only=save_rst_only, keep_exist_as_backup=keep_exist_as_backup)

    def pageListCurrentItemChanged(self):
        item = self.pageList.currentItem()
        self.page_changing = True
        if item is not None:
            if self.save_on_page_changed:
                self.conditional_save()
            self.imgtrans_proj.set_current_img(item.text())
            self.canvas.clear_undostack(update_saved_step=True)
            self.canvas.updateCanvas()
            # Scroll to top when changing page
            self.canvas.gv.verticalScrollBar().setValue(0)
            self.canvas.gv.horizontalScrollBar().setValue(0)
            self.st_manager.updateSceneTextitems()
            # Restore text block mode after updating scene items
            if self.bottomBar.textblockChecker.isChecked() or pcfg.imgtrans_textblock:
                self.setTextBlockMode()
            # Restore canvas focus after page change. Idle-checked so it does
            # not yank focus out of a text editor on programmatic page changes
            # (e.g. global search jump while typing in the search bar).
            QTimer.singleShot(100, self._restore_canvas_focus_if_idle)
            self.titleBar.setTitleContent(page_name=self.imgtrans_proj.current_img)
            self.module_manager.handle_page_changed()
            self.drawingPanel.handle_page_changed()
            
        self.page_changing = False

    def setupShortcuts(self):
        self.titleBar.nextpage_trigger.connect(self.shortcutNext)
        self.titleBar.prevpage_trigger.connect(self.shortcutBefore)
        self.titleBar.textedit_trigger.connect(self.shortcutTextedit)
        self.titleBar.drawboard_trigger.connect(self.shortcutDrawboard)
        self.titleBar.redo_trigger.connect(self.on_redo)
        self.titleBar.undo_trigger.connect(self.on_undo)
        self.titleBar.page_search_trigger.connect(self.on_page_search)
        self.titleBar.global_search_trigger.connect(self.on_global_search)
        self.titleBar.replacePreMTkeyword_trigger.connect(self.show_pre_MT_keyword_window)
        self.titleBar.replaceMTkeyword_trigger.connect(self.show_MT_keyword_window)
        self.titleBar.replaceOCRkeyword_trigger.connect(self.show_OCR_keyword_window)
        self.titleBar.run_trigger.connect(self.leftBar.runImgtransBtn.click)
        self.titleBar.run_woupdate_textstyle_trigger.connect(self.run_imgtrans_wo_textstyle_update)
        self.titleBar.translate_page_trigger.connect(self.on_transpagebtn_pressed)
        self.titleBar.enable_module.connect(self.on_enable_module)
        self.titleBar.importtstyle_trigger.connect(self.import_tstyles)
        self.titleBar.exporttstyle_trigger.connect(self.export_tstyles)
        self.titleBar.darkmode_trigger.connect(self.on_darkmode_triggered)
        self.titleBar.merge_tool_trigger.connect(self.on_open_merge_tool)

        # Thai-keyboard fallback table. Qt's QShortcut("A") matches by produced
        # character, not the physical key; on a Thai (Kedmanee) layout the same
        # physical keys produce different characters and the English bindings
        # never fire while Thai input is active. Registering parallel shortcuts
        # for the Thai equivalents lets users operate the app without flipping
        # IME state.
        #
        # Each physical English key gets BOTH the unshifted and the shifted
        # Kedmanee variant where they differ -- some users hold shift purely
        # to access symbols (e.g. '[' shifted) without realising the layout
        # remap also moves the underlying character. We register every variant
        # because handler bodies are idempotent and Qt deduplicates by sequence.
        self._thai_keymap = {
            'A': ['ฟ'],
            'D': ['ก'],
            'W': ['ไ'],
            'N': ['ื'],
            'H': ['้'],
            'R': ['พ'],
            'B': ['ิ'],
            'J': ['่'],
            '[': ['บ', 'ฃ'],        # unshifted then shifted Kedmanee
            ']': ['ล', 'ฯ'],
            'T': ['ะ'],
            'P': ['ย'],
            'E': ['ำ'],
            'F': ['ด'],
            'G': ['เ'],
            'M': ['ท'],
        }

        # Helper: register one or more shortcut sequences against a single
        # handler. Works for plain keys (T) and for chord keys (Ctrl+J,
        # Ctrl+Shift+R). Qt routes activated() back through self.sender() in
        # downstream handlers, so we attach .key() metadata via the sequence
        # string -- not via a partial -- to keep signal introspection working.
        def _register_shortcuts(sequences, handler):
            for seq in sequences:
                sc = QShortcut(QKeySequence(seq), self)
                sc.activated.connect(handler)
            return None

        # Build (eng_key, [eng+thai sequences]) pairs once so all the Ctrl/Alt
        # combos that include a letter automatically gain Thai equivalents too.
        def _seqs_for(*english_keys):
            # english_keys: a list of plain keys (e.g. 'A', 'Ctrl+J', 'Ctrl+Shift+R').
            # For each, also emit the Thai-equivalent variants if a single
            # letter or symbol is present in the chord. Returns the original
            # list plus the Thai duplicates -- the original always comes first
            # so English-keyboard users hit the same QShortcut path.
            out = []
            for eng in english_keys:
                out.append(eng)
                # Operate on the trailing chord token so we only swap the
                # final key, not modifier names. e.g. "Ctrl+Shift+R" -> "R".
                parts = eng.split('+')
                tail = parts[-1]
                lookup_key = tail.upper() if (len(tail) == 1 and tail.isalpha()) else tail
                thai_variants = self._thai_keymap.get(lookup_key)
                if not thai_variants:
                    continue
                prefix = parts[:-1]
                for thai in thai_variants:
                    if prefix:
                        out.append('+'.join(prefix + [thai]))
                    else:
                        out.append(thai)
            return out

        _register_shortcuts(_seqs_for('A'), self.shortcutBefore)
        shortcutPageUp = QShortcut(QKeySequence(QKeySequence.StandardKey.MoveToPreviousPage), self)
        shortcutPageUp.activated.connect(self.shortcutBefore)

        _register_shortcuts(_seqs_for('D'), self.shortcutNext)
        shortcutPageDown = QShortcut(QKeySequence(QKeySequence.StandardKey.MoveToNextPage), self)
        shortcutPageDown.activated.connect(self.shortcutNext)

        _register_shortcuts(_seqs_for('W'), self.shortcutTextblock)
        shortcutZoomIn = QShortcut(QKeySequence.StandardKey.ZoomIn, self)
        shortcutZoomIn.activated.connect(self.canvas.gv.scale_up_signal)
        shortcutZoomOut = QShortcut(QKeySequence.StandardKey.ZoomOut, self)
        shortcutZoomOut.activated.connect(self.canvas.gv.scale_down_signal)
        # Ctrl+D needs Thai variant: Ctrl+ก produced by physical D on Kedmanee.
        _register_shortcuts(_seqs_for('Ctrl+D'), self.shortcutCtrlD)
        shortcutSpace = QShortcut(QKeySequence("Space"), self)
        shortcutSpace.activated.connect(self.shortcutSpace)
        shortcutSelectAll = QShortcut(QKeySequence.StandardKey.SelectAll, self)
        shortcutSelectAll.activated.connect(self.shortcutSelectAll)

        shortcutEscape = QShortcut(QKeySequence("Escape"), self)
        shortcutEscape.activated.connect(self.shortcutEscape)

        shortcutBold = QShortcut(QKeySequence.StandardKey.Bold, self)
        shortcutBold.activated.connect(self.shortcutBold)
        shortcutItalic = QShortcut(QKeySequence.StandardKey.Italic, self)
        shortcutItalic.activated.connect(self.shortcutItalic)
        shortcutUnderline = QShortcut(QKeySequence.StandardKey.Underline, self)
        shortcutUnderline.activated.connect(self.shortcutUnderline)

        shortcutDelete = QShortcut(QKeySequence.StandardKey.Delete, self)
        shortcutDelete.activated.connect(self.shortcutDelete)

        drawpanel_shortcuts = {'hand': 'H', 'rect': 'R', 'inpaint': 'J', 'pen': 'B'}
        for tool_name, shortcut_key in drawpanel_shortcuts.items():
            for seq in _seqs_for(shortcut_key):
                shortcut = QShortcut(QKeySequence(seq), self)
                shortcut.activated.connect(partial(self.drawingPanel.shortcutSetCurrentToolByName, tool_name))
            # Tooltip still shows English key (the canonical hint to users).
            self.drawingPanel.setShortcutTip(tool_name, shortcut_key)

        # Brush size: [ and ]. Thai equivalents from _thai_keymap.
        _register_shortcuts(_seqs_for('['), self.drawingPanel.on_decre_pensize)
        _register_shortcuts(_seqs_for(']'), self.drawingPanel.on_incre_pensize)

        # Ctrl+E for OCR
        _register_shortcuts(_seqs_for('Ctrl+E'), self.shortcutOCR)

        # N: toggle the floating per-block number badges on the canvas.
        # Persisted in pcfg so the choice survives restart.
        _register_shortcuts(_seqs_for('N'), self.shortcutToggleNumberBadge)

        # Ctrl+Shift+R: auto-sort blocks by manhwa reading order (top-to-bottom,
        # left-to-right within rows). Pushes a single undoable RearrangeBlksCommand.
        _register_shortcuts(_seqs_for('Ctrl+Shift+R'), self.shortcutAutoSortReadingOrder)

        # Ctrl+J: jump-to-position quick reorder. Spawns a small numeric input
        # popup over the selected block's badge so the user can type a 1-based
        # target position and press Enter. Replaces drag-precision pain when
        # there are 30+ badges packed together.
        # NOTE on key choice: Ctrl+G is already bound to global search
        # (mainwindowbars.py:349). Ctrl+J ("jump") is unused and keeps the
        # mnemonic.
        _register_shortcuts(_seqs_for('Ctrl+J'), self.shortcutQuickReorder)

        # Block reorder shortcuts mirror the right-click context menu:
        # Alt+Up/Down nudge by one slot; Alt+Home/End jump to top/bottom.
        # All require a selected block in text edit mode.
        # Arrow keys / Home / End are layout-independent (their character output
        # does not change on Thai keyboards), so no Thai fallback is needed.
        shortcutMoveBlockUp = QShortcut(QKeySequence("Alt+Up"), self)
        shortcutMoveBlockUp.activated.connect(self.shortcutMoveBlockUp)
        shortcutMoveBlockDown = QShortcut(QKeySequence("Alt+Down"), self)
        shortcutMoveBlockDown.activated.connect(self.shortcutMoveBlockDown)
        shortcutMoveBlockTop = QShortcut(QKeySequence("Alt+Home"), self)
        shortcutMoveBlockTop.activated.connect(self.shortcutMoveBlockTop)
        shortcutMoveBlockBottom = QShortcut(QKeySequence("Alt+End"), self)
        shortcutMoveBlockBottom.activated.connect(self.shortcutMoveBlockBottom)

    def shortcutNext(self):

        sender: QShortcut = self.sender()
        if isinstance(sender, QShortcut):
            # Bypass page-nav when editing a text block. The original check
            # only matched the English D shortcut; the parallel Thai shortcut
            # ('ก') would otherwise leak through and flip pages while the
            # user typed. We compare the key sequence text instead so any
            # bare-letter binding (English or Thai) is recognised.
            seq_txt = sender.key().toString() if hasattr(sender.key(), 'toString') else ''
            is_letter_only = len(seq_txt) == 1 and not seq_txt.isspace()
            if sender.key() == QKEY.Key_D or is_letter_only:
                if self.canvas.editing_textblkitem is not None:
                    return
        if self.centralStackWidget.currentIndex() == 0:
            focus_widget = self.app.focusWidget()
            if self.st_manager.is_editting():
                self.st_manager.on_switch_textitem(1)
            elif isinstance(focus_widget, (SourceTextEdit, TransTextEdit)):
                self.st_manager.on_switch_textitem(1, current_editing_widget=focus_widget)
            else:
                index = self.pageList.currentIndex()
                page_count = self.pageList.count()
                if index.isValid():
                    row = index.row()
                    row = (row + 1) % page_count
                    self.pageList.setCurrentRow(row)

    def shortcutBefore(self):

        sender: QShortcut = self.sender()
        if isinstance(sender, QShortcut):
            # Same rationale as shortcutNext: gate on "any single letter"
            # shortcut so the Thai equivalent ('ฟ') also yields to text edit.
            seq_txt = sender.key().toString() if hasattr(sender.key(), 'toString') else ''
            is_letter_only = len(seq_txt) == 1 and not seq_txt.isspace()
            if sender.key() == QKEY.Key_A or is_letter_only:
                if self.canvas.editing_textblkitem is not None:
                    return
        if self.centralStackWidget.currentIndex() == 0:
            focus_widget = self.app.focusWidget()
            if self.st_manager.is_editting():
                self.st_manager.on_switch_textitem(-1)
            elif isinstance(focus_widget, (SourceTextEdit, TransTextEdit)):
                self.st_manager.on_switch_textitem(-1, current_editing_widget=focus_widget)
            else:
                index = self.pageList.currentIndex()
                page_count = self.pageList.count()
                if index.isValid():
                    row = index.row()
                    row = (row - 1 + page_count) % page_count
                    self.pageList.setCurrentRow(row)

    def shortcutTextedit(self):
        if self.centralStackWidget.currentIndex() == 0:
            self.bottomBar.texteditChecker.click()

    def shortcutTextblock(self):
        if self.centralStackWidget.currentIndex() == 0:
            if self.bottomBar.texteditChecker.isChecked():
                self.bottomBar.textblockChecker.click()

    def shortcutDrawboard(self):
        if self.centralStackWidget.currentIndex() == 0:
            self.bottomBar.paintChecker.click()

    def shortcutCtrlD(self):
        if self.centralStackWidget.currentIndex() == 0:
            if self.drawingPanel.isVisible():
                if self.drawingPanel.currentTool == self.drawingPanel.rectTool:
                    self.drawingPanel.rectPanel.delete_btn.click()
            elif self.canvas.textEditMode():
                self.canvas.delete_textblks.emit(0)

    def shortcutSelectAll(self):
        
        # Select all text blocks when in main view (index 0) and text panel is active (index 1)
        if self.centralStackWidget.currentIndex() == 0:
            # Check if textPanel is the current widget in rightComicTransStackPanel (index 1)
            if hasattr(self, 'rightComicTransStackPanel') and self.rightComicTransStackPanel.currentIndex() == 1:
                self.st_manager.set_blkitems_selection(True)
            # Also allow if textPanel is visible (fallback check)
            elif hasattr(self, 'textPanel') and self.textPanel.isVisible():
                self.st_manager.set_blkitems_selection(True)

    def shortcutSpace(self):
        if self.centralStackWidget.currentIndex() == 0:
            if self.drawingPanel.isVisible():
                if self.drawingPanel.currentTool == self.drawingPanel.rectTool:
                    self.drawingPanel.rectPanel.inpaint_btn.click()

    def shortcutBold(self):
        if self.textPanel.formatpanel.isVisible():
            self.textPanel.formatpanel.formatBtnGroup.boldBtn.click()

    def shortcutDelete(self):
        if self.canvas.gv.isVisible():
            self.canvas.delete_textblks.emit(1)

    def shortcutItalic(self):
        if self.textPanel.formatpanel.isVisible():
            self.textPanel.formatpanel.formatBtnGroup.italicBtn.click()

    def shortcutUnderline(self):
        if self.textPanel.formatpanel.isVisible():
            self.textPanel.formatpanel.formatBtnGroup.underlineBtn.click()

    def shortcutOCR(self):
        """Ctrl+E: Run OCR on selected text blocks."""
        if self.canvas.textEditMode():
            blkitem_list = self.canvas.selected_text_items()
            if len(blkitem_list) > 0:
                self.translateBlkitemList(blkitem_list, 0)  # mode 0 = OCR only

    def on_redo(self):
        self.canvas.redo()

    def on_undo(self):
        self.canvas.undo()

    def on_page_search(self):
        if self.canvas.gv.isVisible():
            fo = self.app.focusObject()
            sel_text = ''
            tgt_edit = None
            blkitem = self.canvas.editing_textblkitem
            if fo == self.canvas.gv and blkitem is not None:
                sel_text = blkitem.textCursor().selectedText()
                tgt_edit = self.st_manager.pairwidget_list[blkitem.idx].e_trans
            elif isinstance(fo, QTextEdit) or isinstance(fo, QPlainTextEdit):
                sel_text = fo.textCursor().selectedText()
                if isinstance(fo, SourceTextEdit):
                    tgt_edit = fo
            se = self.canvas.search_widget.search_editor
            se.setFocus()
            if sel_text != '':
                se.setPlainText(sel_text)
                cursor = se.textCursor()
                cursor.select(QTextCursor.SelectionType.Document)
                se.setTextCursor(cursor)

            if self.canvas.search_widget.isHidden():
                self.canvas.search_widget.show()
            self.canvas.search_widget.setCurrentEditor(tgt_edit)

    def on_global_search(self):
        if self.canvas.gv.isVisible():
            if not self.leftBar.globalSearchChecker.isChecked():
                self.leftBar.globalSearchChecker.click()
            fo = self.app.focusObject()
            sel_text = ''
            blkitem = self.canvas.editing_textblkitem
            if fo == self.canvas.gv and blkitem is not None:
                sel_text = blkitem.textCursor().selectedText()
            elif isinstance(fo, QTextEdit) or isinstance(fo, QPlainTextEdit):
                sel_text = fo.textCursor().selectedText()
            se = self.global_search_widget.search_editor
            se.setFocus()
            if sel_text != '':
                se.setPlainText(sel_text)
                cursor = se.textCursor()
                cursor.select(QTextCursor.SelectionType.Document)
                se.setTextCursor(cursor)
                
                self.global_search_widget.commit_search()

    def show_pre_MT_keyword_window(self):
        self.mtPreSubWidget.show()

    def show_MT_keyword_window(self):
        self.mtSubWidget.show()

    def show_OCR_keyword_window(self):
        self.ocrSubWidget.show()

    def on_open_merge_tool(self):
        """打开区域合并工具对话框"""
        if not hasattr(self, 'merge_dialog') or self.merge_dialog is None:
            from .merge_dialog import MergeDialog
            from qtpy.QtCore import QThread
            from qtpy.QtWidgets import QProgressDialog
            from utils import merger
            
            self.merge_dialog = MergeDialog(self)
            self.merge_dialog.run_current_clicked.connect(lambda: self.run_merge_task(on_current=True))
            self.merge_dialog.run_all_clicked.connect(lambda: self.run_merge_task(on_current=False))
        
        if self.merge_dialog.isVisible():
            self.merge_dialog.raise_()
            self.merge_dialog.activateWindow()
        else:
            self.merge_dialog.show()

    def run_merge_task(self, on_current=False):
        """执行区域合并任务"""
        from utils import merger
        from qtpy.QtWidgets import QMessageBox
        
        if self.imgtrans_proj.is_empty:
            QMessageBox.warning(self, "警告", "请先打开一个项目")
            return
        
        config = self.merge_dialog.get_config()
        
        if on_current:
            # 对当前文件运行 - 直接在内存中操作，不读写文件
            from utils.textblock import TextBlock
            
            current_img = self.imgtrans_proj.current_img
            if not current_img:
                QMessageBox.warning(self, "警告", "没有当前文件")
                return
            
            # 直接从内存获取当前页面的文本框
            if current_img not in self.imgtrans_proj.pages:
                QMessageBox.warning(self, "警告", "当前页面数据不存在")
                return
            
            textblocks = self.imgtrans_proj.pages[current_img]
            if not textblocks:
                QMessageBox.warning(self, "提示", "当前页面没有文本框")
                return
            
            # 将 TextBlock 对象转换为字典格式（merger 需要字典）
            initial_shapes = [blk.to_dict() for blk in textblocks]
            
            initial_count = len(initial_shapes)
            mode = config.get("MERGE_MODE", "NONE")
            total_merged = 0
            
            # 在内存中执行合并
            if mode == "VERTICAL":
                final_shapes, count = merger.perform_merge(initial_shapes, "VERTICAL", config)
                total_merged += count
            elif mode == "HORIZONTAL":
                final_shapes, count = merger.perform_merge(initial_shapes, "HORIZONTAL", config)
                total_merged += count
            elif mode == "VERTICAL_THEN_HORIZONTAL":
                temp, count1 = merger.perform_merge(initial_shapes, "VERTICAL", config)
                final_shapes, count2 = merger.perform_merge(temp, "HORIZONTAL", config)
                total_merged += (count1 + count2)
            elif mode == "HORIZONTAL_THEN_VERTICAL":
                temp, count1 = merger.perform_merge(initial_shapes, "HORIZONTAL", config)
                final_shapes, count2 = merger.perform_merge(temp, "VERTICAL", config)
                total_merged += (count1 + count2)
            else:
                final_shapes = initial_shapes
            
            if total_merged > 0:
                # 将字典转回 TextBlock 对象并更新内存
                self.imgtrans_proj.pages[current_img] = [TextBlock(**blk_dict) for blk_dict in final_shapes]
                # 刷新画布
                self.canvas.updateCanvas()
                self.st_manager.updateSceneTextitems()
                final_count = len(final_shapes)
                QMessageBox.information(self, "成功", f"合并完成: 框数 {initial_count} -> {final_count} (减少了 {initial_count - final_count} 个)")
            else:
                # 提供更详细的提示
                labels = set(s.get('label', '') for s in initial_shapes)
                detail_msg = f"未发生任何合并。\n共有 {initial_count} 个文本框。\n标签类型: {', '.join(labels) or '无'}\n\n"
                detail_msg += "建议：\n"
                detail_msg += "1. 尝试增大最大间隙值（如 100-200）\n"
                detail_msg += "2. 降低最小重叠比例（如 50-70%）\n"
                detail_msg += "3. 取消勾选'启用排除合并的标签'\n"
                detail_msg += "4. 检查标签是否在黑名单中"
                QMessageBox.warning(self, "提示", detail_msg)
        else:
            # 对所有文件运行
            img_list = list(self.imgtrans_proj.pages.keys())
            if not img_list:
                QMessageBox.warning(self, "警告", "项目中没有图片")
                return
            
            # 使用项目的 JSON 文件路径
            json_path = self.imgtrans_proj.proj_path
            if not json_path or not osp.exists(json_path):
                QMessageBox.warning(self, "警告", f"找不到项目 JSON 文件: {json_path}")
                return
            
            # 使用后台线程执行合并
            self.run_merge_all_async(json_path, img_list, config)
    
    def run_merge_all_async(self, json_path, img_list, config):
        """异步执行所有文件的合并"""
        from .io_thread import MergeThread
        
        # 创建合并线程（如果不存在）
        if not hasattr(self, 'merge_thread'):
            self.merge_thread = MergeThread()
            self.merge_thread.progress_changed.connect(self.on_merge_progress)
            self.merge_thread.merge_finished.connect(self.on_merge_finished)
            self.merge_thread.progress_bar.stop_clicked.connect(self.on_merge_stop)
        
        # 启动合并
        if self.merge_thread.runMerge(json_path, img_list, config):
            # 显示进度对话框
            self.merge_thread.progress_bar.zero_progress()
            self.merge_thread.progress_bar.show()
    
    def on_merge_progress(self, current, total):
        """合并进度更新"""
        progress = int(current / total * 100)
        self.merge_thread.progress_bar.updateTaskProgress(progress, f' {current}/{total}')
    
    def on_merge_stop(self):
        """停止合并"""
        if hasattr(self, 'merge_thread'):
            self.merge_thread.requestStop()
            self.merge_thread.progress_bar.hide()
    
    def on_merge_finished(self, success_count, fail_count):
        """合并完成"""
        self.merge_thread.progress_bar.hide()
        
        # Reload the whole project after the region-merge tool finishes.
        # A failure here leaves UI state pointing at pre-merge data, so we
        # log + surface it instead of swallowing.
        try:
            json_path = self.imgtrans_proj.proj_path
            current_img = self.imgtrans_proj.current_img
            self.imgtrans_proj.load_from_json(json_path)
            if current_img and current_img in self.imgtrans_proj.pages:
                self.imgtrans_proj.set_current_img(current_img)
                self.canvas.updateCanvas()
                self.st_manager.updateSceneTextitems()
        except Exception as e:
            LOGGER.exception('Failed to reload project after merge tool')
            create_error_dialog(e, self.tr('Failed to reload project after merge.'), 'MergeReloadFailed')
        
        # 显示结果
        total = success_count + fail_count
        QMessageBox.information(self, "完成", f"区域合并完成\n成功: {success_count}/{total}\n失败: {fail_count}/{total}")

    def on_req_update_pagetext(self):
        if self.canvas.text_change_unsaved():
            self.st_manager.updateTextBlkList()

    def on_req_move_page(self, page_name: str, force_save=False):
        ori_save = self.save_on_page_changed
        self.save_on_page_changed = False
        current_img = self.imgtrans_proj.current_img
        if current_img == page_name and not force_save:
            return
        if current_img not in self.global_search_widget.page_set:
            if self.canvas.projstate_unsaved: 
                self.saveCurrentPage()
        else:
            self.saveCurrentPage(save_rst_only=True)
        self.pageList.setCurrentRow(self.imgtrans_proj.pagename2idx(page_name))
        self.save_on_page_changed = ori_save

    def on_search_result_item_clicked(self, pagename: str, blk_idx: int, is_src: bool, start: int, end: int):
        idx = self.imgtrans_proj.pagename2idx(pagename)
        self.pageList.setCurrentRow(idx)
        pw = self.st_manager.pairwidget_list[blk_idx]
        edit = pw.e_source if is_src else pw.e_trans
        edit.setFocus()
        edit.ensure_scene_visible.emit()
        cursor = QTextCursor(edit.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        edit.setTextCursor(cursor)

    def shortcutEscape(self):
        if self.canvas.search_widget.isVisible():
            self.canvas.search_widget.hide()
        elif self.canvas.editing_textblkitem is not None and self.canvas.editing_textblkitem.isEditing():
            self.canvas.editing_textblkitem.endEdit()
        # Cancel any in-progress badge drag-reorder gesture. Cheap no-op if
        # nothing is being dragged.
        if hasattr(self, 'st_manager'):
            self.st_manager.cancel_badge_drag()

    def shortcutToggleNumberBadge(self):
        # Skip when focus is on any text input (translation/source fields,
        # search bars, font-family combobox, font-size spinbox, etc.) so
        # typing the letter "n" never silently toggles badges. Uses the same
        # exclusion list as the focus-restore path for consistency.
        focus_widget = self.app.focusWidget()
        if self._focus_widget_is_text_input(focus_widget):
            return
        # Only toggle while the canvas is the active page; on config/module pages
        # the badges aren't visible anyway.
        if self.centralStackWidget.currentIndex() != 0:
            return
        new_visible = not getattr(pcfg, 'show_textblock_number', True)
        pcfg.show_textblock_number = new_visible
        if hasattr(self, 'st_manager'):
            self.st_manager.set_numbers_visible(new_visible)

    def shortcutAutoSortReadingOrder(self):
        # Only meaningful in text edit mode where the undo stack accepts our
        # RearrangeBlksCommand. Show a hint instead of silent-failing so users
        # don't think the shortcut is broken.
        if not self.canvas.textEditMode():
            create_info_dialog(self.tr('Switch to text edit mode to auto-sort blocks.'))
            return
        if hasattr(self, 'st_manager'):
            self.st_manager.auto_sort_reading_order()

    def shortcutQuickReorder(self):
        # Ctrl+J handler: spawn the quick-reorder numeric popup over the
        # currently focused block's badge. Pre-conditions mirror
        # shortcutAutoSortReadingOrder so the two stay consistent:
        #   * must be in text edit mode (RearrangeBlksCommand depends on the
        #     text undo stack being live)
        #   * a block must be identifiable -- via canvas.selected_text_items()
        #     first, falling back to the shape-control bound block. If neither
        #     exists, prompt the user instead of silently no-oping.
        if shared.HEADLESS:
            return
        if not self.canvas.textEditMode():
            create_info_dialog(self.tr('Switch to text edit mode to reorder blocks.'))
            return
        if not hasattr(self, 'st_manager'):
            return

        # Multi-select policy: when more than one block is selected we use the
        # FIRST in scene order (selected_text_items returns sorted by idx by
        # default). Reordering only one of N selected blocks is more
        # predictable than batch-moving all of them; users can still drag-
        # select+Move-to-top from the context menu if they need batch ops.
        target_blk = None
        sel = self.canvas.selected_text_items()
        if sel:
            target_blk = sel[0]
        elif self.canvas.txtblkShapeControl.blk_item is not None:
            target_blk = self.canvas.txtblkShapeControl.blk_item

        if target_blk is None:
            create_info_dialog(self.tr('Select a text block first.'))
            return

        idx = getattr(target_blk, 'idx', None)
        if idx is None:
            return
        self.st_manager.open_quick_reorder_popup(idx)

    def _move_selected_block(self, target_provider):
        # Shared body for Alt+Up/Down/Home/End shortcuts. target_provider is a
        # callable (current_idx, n) -> desired_final_idx so each shortcut just
        # specifies "where this block should land". Bounds checking lives in
        # st_manager.move_block_to_position so we can pass any int.
        if shared.HEADLESS:
            return
        if not self.canvas.textEditMode():
            return
        if not hasattr(self, 'st_manager'):
            return
        target_blk = None
        sel = self.canvas.selected_text_items()
        if sel:
            target_blk = sel[0]
        elif self.canvas.txtblkShapeControl.blk_item is not None:
            target_blk = self.canvas.txtblkShapeControl.blk_item
        if target_blk is None:
            return
        idx = getattr(target_blk, 'idx', None)
        if idx is None:
            return
        n = len(self.st_manager.textblk_item_list)
        if n < 2:
            return
        target = target_provider(idx, n)
        self.st_manager.move_block_to_position(idx, target)

    def shortcutMoveBlockUp(self):
        self._move_selected_block(lambda idx, n: idx - 1)

    def shortcutMoveBlockDown(self):
        self._move_selected_block(lambda idx, n: idx + 1)

    def shortcutMoveBlockTop(self):
        self._move_selected_block(lambda idx, n: 0)

    def shortcutMoveBlockBottom(self):
        self._move_selected_block(lambda idx, n: n - 1)

    def setPaintMode(self):
        if self.bottomBar.paintChecker.isChecked():
            if self.rightComicTransStackPanel.isHidden():
                self.rightComicTransStackPanel.show()
            self.rightComicTransStackPanel.setCurrentIndex(0)
            self.canvas.setPaintMode(True)
            self.bottomBar.originalSlider.show()
            self.bottomBar.textlayerSlider.show()
            self.bottomBar.textblockChecker.hide()
            # Force the brush/cross cursor to refresh once the panel and its
            # parent stack are unambiguously visible. Without this the very
            # first time the user enters paint mode (drawingPanel had never
            # been shown before, so its showEvent fires while the parent
            # stack is still in transition) the canvas keeps the previous
            # ScrollHandCursor and the brush circle never appears on hover.
            # Deferring to the next event-loop tick gives Qt time to flip
            # the visibility chain so isVisible() reports True downstream.
            from qtpy.QtCore import QTimer as _QTimer
            _QTimer.singleShot(0, self.drawingPanel.refreshCurrentToolCursor)
        else:
            self.canvas.setPaintMode(False)
            self.rightComicTransStackPanel.setHidden(True)
        self.st_manager.setTextEditMode(False)

    def setTextEditMode(self):
        if self.bottomBar.texteditChecker.isChecked():
            if self.rightComicTransStackPanel.isHidden():
                self.rightComicTransStackPanel.show()
            self.bottomBar.textblockChecker.show()
            self.rightComicTransStackPanel.setCurrentIndex(1)
            self.st_manager.setTextEditMode(True)
            self.setTextBlockMode()
        else:
            self.bottomBar.textblockChecker.hide()
            self.rightComicTransStackPanel.setHidden(True)
            self.st_manager.setTextEditMode(False)
        self.canvas.setPaintMode(False)

    def setTextBlockMode(self):
        mode = self.bottomBar.textblockChecker.isChecked()
        self.canvas.setTextBlockMode(mode)
        pcfg.imgtrans_textblock = mode
        self.st_manager.showTextblkItemRect(mode)

    def manual_save(self):
        if self.leftBar.imgTransChecker.isChecked()\
            and self.imgtrans_proj.directory is not None:
            LOGGER.debug('Manually saving...')
            self.saveCurrentPage(update_scene_text=True, save_proj=True, restore_interface=True, save_rst_only=False)

    def saveAllPages(self):
        if self.pageList.count() == 0:
            return
        original_idx = self.pageList.currentRow()
        original_save_on_page_changed = self.save_on_page_changed
        self.save_on_page_changed = False  # Disable auto-save during batch save to avoid double calls
        
        progress = QProgressDialog(self.tr("Saving all pages..."), self.tr("Cancel"), 0, self.pageList.count(), self)
        progress.setModal(True)
        progress.show()

        for i in range(self.pageList.count()):
            progress.setValue(i)
            if progress.wasCanceled():
                break
            self.pageList.setCurrentRow(i)
            # Process events to ensure UI updates and canvas is ready
            self.app.processEvents()
            self.saveCurrentPage(update_scene_text=True, save_proj=True)
            
        progress.setValue(self.pageList.count())
        self.pageList.setCurrentRow(original_idx)
        self.save_on_page_changed = original_save_on_page_changed

    def applyFontToAllPagesAndSave(self):
        if self.pageList.count() == 0:
            return
        original_idx = self.pageList.currentRow()
        original_save_on_page_changed = self.save_on_page_changed
        self.save_on_page_changed = False

        n_pages = self.pageList.count()
        progress = QProgressDialog(
            self.tr("Applying font style to all pages..."),
            self.tr("Cancel"), 0, n_pages, self
        )
        progress.setWindowTitle(self.tr("Apply Font Style to All Pages"))
        progress.setMinimumDuration(0)
        progress.setModal(True)
        progress.show()

        for i in range(n_pages):
            if progress.wasCanceled():
                break
            progress.setValue(i)
            progress.setLabelText(self.tr("Saving page {} / {}...").format(i + 1, n_pages))
            self.pageList.setCurrentRow(i)
            self.app.processEvents()
            self.saveCurrentPage(update_scene_text=True, save_proj=True)

        progress.setValue(n_pages)
        self.pageList.setCurrentRow(original_idx)
        self.app.processEvents()
        self.save_on_page_changed = original_save_on_page_changed

    def saveCurrentPage(self, update_scene_text=True, save_proj=True, restore_interface=False, save_rst_only=False, keep_exist_as_backup=False, _is_autosave=False):
        # _is_autosave is internal: when True, queued image writes are tagged so the
        # autosave in-flight guard can track them. Manual saves leave it False so they
        # behave exactly as before (no behavior change for Ctrl+S / save button).
        if not self.imgtrans_proj.img_valid:
            return
        
        if restore_interface:
            set_canvas_focus = self.canvas.hasFocus()
            sel_textitem = self.canvas.selected_text_items()
            n_sel_textitems = len(sel_textitem)
            editing_textitem = None
            if n_sel_textitems == 1 and sel_textitem[0].isEditing():
                editing_textitem = sel_textitem[0]
        
        if update_scene_text or self.canvas.text_change_unsaved():
            self.st_manager.updateTextBlkList()
        
        if self.rightComicTransStackPanel.isHidden():
            self.bottomBar.texteditChecker.click()

        # DO NOT touch checkbox state - only user can control it via W key or clicking
        # Checkbox should only be changed by:
        # 1. User pressing W key (shortcutTextblock)
        # 2. User clicking the checkbox directly
        # No other code should modify checkbox state

        hide_tsc = False
        if self.st_manager.txtblkShapeControl.isVisible():
            hide_tsc = True
            self.st_manager.txtblkShapeControl.hide()

        if not osp.exists(self.imgtrans_proj.result_dir()):
            os.makedirs(self.imgtrans_proj.result_dir())

        if save_proj:
            try:
                self.imgtrans_proj.save(keep_exist_as_backup=keep_exist_as_backup)
                save_draw_outputs = (not save_rst_only) and (self.canvas.draw_change_unsaved() or self.canvas.drawingLayer.drawed())
                if save_draw_outputs:
                    self.imgtrans_proj.cleanup_stale_output_files(self.imgtrans_proj.current_img)
                    mask_path = self.imgtrans_proj.get_mask_path()
                    mask_array = self.imgtrans_proj.mask_array
                    if mask_array is not None:
                        # Increment AFTER successful enqueue so an exception cannot leak the counter and stall future autosaves
                        self.imsave_thread.saveImg(mask_path, mask_array, save_params={'ext': pcfg.imgsave_ext, 'quality': pcfg.imgsave_quality}, is_autosave=_is_autosave)
                        if _is_autosave:
                            self._autosave_pending += 1
                    inpainted_path = self.imgtrans_proj.get_inpainted_path()
                    if self.canvas.drawingLayer.drawed():
                        inpainted = self.canvas.base_pixmap.copy()
                        painter = QPainter(inpainted)
                        try:
                            painter.drawPixmap(0, 0, self.canvas.drawingLayer.get_drawed_pixmap())
                        finally:
                            painter.end()
                    else:
                        inpainted = self.imgtrans_proj.inpainted_array
                    if inpainted is not None:
                        self.imsave_thread.saveImg(inpainted_path, inpainted, save_params={'ext': pcfg.imgsave_ext, 'quality': pcfg.imgsave_quality}, keep_alpha=self.imgtrans_proj.current_has_alpha(), is_autosave=_is_autosave)
                        if _is_autosave:
                            self._autosave_pending += 1
            except Exception as e:
                LOGGER.error(f"Failed to save project files: {e}")

        # Render the final result image properly
        # For autosave, preserve selection state (autosave should not affect selection)
        preserve_selection = not restore_interface  # Preserve selection during autosave
        try:
            img = self.canvas.render_result_img(preserve_selection=preserve_selection)
            imsave_path = self.imgtrans_proj.get_result_path(self.imgtrans_proj.current_img)
            self.imgtrans_proj.cleanup_stale_output_files(self.imgtrans_proj.current_img, targets={'result'})
            self.imsave_thread.saveImg(imsave_path, img, self.imgtrans_proj.current_img, save_params={'ext': pcfg.imgsave_ext, 'quality': pcfg.imgsave_quality}, keep_alpha=self.imgtrans_proj.current_has_alpha(), is_autosave=_is_autosave)
            if _is_autosave:
                self._autosave_pending += 1
        except Exception as e:
            LOGGER.error(f"Failed to render and save result image: {e}")
        
        # Restore text block display state after render_result_img (which calls clearSelection)
        # This is especially important for autosave which doesn't restore interface
        # For autosave, ALWAYS restore the text block display state if it was enabled
        if not restore_interface:
            # Autosave: restore text block display state to match user preference
            if getattr(pcfg, 'imgtrans_textblock', False):
                self.st_manager.showTextblkItemRect(True)
                # Also ensure canvas textblock_mode matches
                if not self.canvas.textblock_mode:
                    self.canvas.textblock_mode = True
            
        self.canvas.setProjSaveState(False)
        self.canvas.update_saved_undostep()

        if restore_interface:
            # DO NOT touch checkbox state - only user can control it via W key or clicking
            if hide_tsc:
                self.st_manager.txtblkShapeControl.show()
            if set_canvas_focus:
                self.canvas.setFocus()
            if n_sel_textitems > 0:
                self.canvas.block_selection_signal = True
                for blk in sel_textitem:
                    blk.setSelected(True)
                self.st_manager.on_incanvas_selection_changed()
                self.canvas.block_selection_signal = False
            if editing_textitem is not None:
                editing_textitem.startEdit()
        
    def to_trans_config(self):
        self.leftBar.configChecker.setChecked(True)
        self.configPanel.focusOnTranslator()

    def to_inpaint_config(self):
        self.leftBar.configChecker.setChecked(True)
        self.configPanel.focusOnInpaint()

    def to_ocr_config(self):
        self.leftBar.configChecker.setChecked(True)
        self.configPanel.focusOnOCR()

    def to_detect_config(self):
        self.leftBar.configChecker.setChecked(True)
        self.configPanel.focusOnDetect()

    def on_textdet_changed(self):
        module = self.bottomBar.textdet_selector.selector.currentText()
        tgt_selector = self.configPanel.detect_config_panel.module_combobox
        if tgt_selector.currentText() != module and module in GET_VALID_TEXTDETECTORS():
            tgt_selector.setCurrentText(module)

    def on_ocr_changed(self):
        module = self.bottomBar.ocr_selector.selector.currentText()
        tgt_selector = self.configPanel.ocr_config_panel.module_combobox
        if tgt_selector.currentText() != module and module in GET_VALID_OCR():
            tgt_selector.setCurrentText(module)

    def on_trans_changed(self):
        module = self.bottomBar.trans_selector.selector.currentText()
        tgt_selector = self.configPanel.trans_config_panel.module_combobox
        if tgt_selector.currentText() != module and module in GET_VALID_TRANSLATORS():
            tgt_selector.setCurrentText(module)

    def on_trans_src_changed(self):
        sender = self.sender()
        text = sender.currentText()
        translator = self.module_manager.translator
        if translator is not None:
            translator.set_source(text)
        pcfg.module.translate_source = text
        combobox = self.configPanel.trans_config_panel.source_combobox
        if sender != combobox:
            combobox.blockSignals(True)
            combobox.setCurrentText(text)
            combobox.blockSignals(False)
        combobox = self.bottomBar.trans_selector.src_selector
        if sender != combobox:
            combobox.blockSignals(True)
            combobox.setCurrentText(text)
            combobox.blockSignals(False)

    def on_trans_tgt_changed(self):
        sender = self.sender()
        text = sender.currentText()
        translator = self.module_manager.translator
        if translator is not None:
            translator.set_target(text)
        pcfg.module.translate_target = text
        combobox = self.configPanel.trans_config_panel.target_combobox
        if sender != combobox:
            combobox.blockSignals(True)
            combobox.setCurrentText(text)
            combobox.blockSignals(False)
        combobox = self.bottomBar.trans_selector.tgt_selector
        if sender != combobox:
            combobox.blockSignals(True)
            combobox.setCurrentText(text)
            combobox.blockSignals(False)

    def on_inpaint_changed(self):
        module = self.bottomBar.inpaint_selector.selector.currentText()
        tgt_selector = self.configPanel.inpaint_config_panel.module_combobox
        if tgt_selector.currentText() != module and module in GET_VALID_INPAINTERS():
            tgt_selector.setCurrentText(module)

    def on_transpagebtn_pressed(self, run_target: bool):
        page_key = self.imgtrans_proj.current_img
        if page_key is None:
            return

        blkitem_list = self.st_manager.textblk_item_list

        if len(blkitem_list) < 1:
            return
        
        self.translateBlkitemList(blkitem_list, -1)

    def translateBlkitemList(self, blkitem_list: List, mode: int) -> bool:

        tgt_img = self.imgtrans_proj.img_array
        if tgt_img is None:
            return False
        tgt_mask = self.imgtrans_proj.mask_array
        
        if len(blkitem_list) < 1:
            return False
        
        self.global_search_widget.set_document_edited()
        
        im_h, im_w = tgt_img.shape[:2]

        blk_list, blk_ids = [], []
        for blkitem in blkitem_list:
            blk: TextBlock = blkitem.blk
            blk._bounding_rect = blkitem.absBoundingRect()
            blk.text = self.st_manager.pairwidget_list[blkitem.idx].e_source.toPlainText()
            blk_ids.append(blkitem.idx)
            blk.set_lines_by_xywh(blk._bounding_rect, angle=-blk.angle, x_range=[0, im_w-1], y_range=[0, im_h-1], adjust_bbox=True)
            blk_list.append(blk)

        self.module_manager.runBlktransPipeline(blk_list, tgt_img, mode, blk_ids, tgt_mask = tgt_mask)
        return True

    def finishTranslatePage(self, page_key):
        if page_key == self.imgtrans_proj.current_img:
            self.st_manager.updateTranslation()

    def on_imgtrans_pipeline_finished(self):
        
        self.backup_blkstyles.clear()
        self._run_imgtrans_wo_textstyle_update = False
        self.postprocess_mt_toggle = True
        if pcfg.module.empty_runcache and not shared.HEADLESS:
            self.module_manager.unload_all_models()
        if shared.args.export_translation_txt:
            self.on_export_txt('translation')
        if shared.args.export_source_txt:
            self.on_export_txt('source')
        if shared.HEADLESS:
            self.run_next_dir()
        else:
            # Reset angle for ALL textblocks in ALL pages after OCR finished
            # Reset angles directly in project data for all pages
            LOGGER.info('[Reset Angle] Resetting angles for all textblocks in all pages after OCR completion')
            
            # Reset angle for all text blocks in all pages
            for page_name in self.imgtrans_proj.pages:
                for blk in self.imgtrans_proj.pages[page_name]:
                    blk.angle = 0
            
            # Reset angle for current page UI items if they exist
            all_text_items = self.st_manager.textblk_item_list
            if len(all_text_items) > 0:
                # Use the reset angle function with all text items directly
                # This ensures undo/redo support and proper UI updates
                self.st_manager.onResetAngle(reset_all=True, items=all_text_items)
            
            
            # IMPORTANT: Save project immediately after resetting angles
            # Update current page text block list first
            if len(all_text_items) > 0:
                self.st_manager.updateTextBlkList()
            # Save current page and entire project
            self.saveCurrentPage(update_scene_text=False, save_proj=True, restore_interface=False, save_rst_only=False)
            
            self.activateWindow()
            # Restore canvas focus after the pipeline finishes so A/D/N/W still
            # work. Spread retries cover the progress-dialog hide window and
            # any late activation events; each retry is idle-checked so we
            # never yank focus out of a translation field.
            self._restore_canvas_focus_if_idle()
            def force_canvas_focus_final():
                self.activateWindow()
                QApplication.setActiveWindow(self)
                self._restore_canvas_focus_if_idle()
            QTimer.singleShot(50, force_canvas_focus_final)
            QTimer.singleShot(300, force_canvas_focus_final)
            QTimer.singleShot(1000, self._restore_canvas_focus_if_idle)

    def postprocess_translations(self, blk_list: List[TextBlock]) -> None:
        src_is_cjk = is_cjk(pcfg.module.translate_source)
        tgt_is_cjk = is_cjk(pcfg.module.translate_target)
        if tgt_is_cjk:
            for blk in blk_list:
                if src_is_cjk:
                    blk.translation = full_len(blk.translation)
                else:
                    blk.translation = half_len(blk.translation)
                    blk.translation = re.sub(r'([?.!"])\s+', r'\1', blk.translation)    # remove spaces following punctuations
        else:
            for blk in blk_list:
                if blk.vertical:
                    blk.alignment = TextAlignment.Center
                blk.translation = half_len(blk.translation)
                blk.vertical = False

        for blk in blk_list:
            blk.translation = self.mtSubWidget.sub_text(blk.translation)
            if pcfg.let_uppercase_flag:
                blk.translation = blk.translation.upper()

    def on_pagtrans_finished(self, page_index: int):
        blk_list = self.imgtrans_proj.get_blklist_byidx(page_index)
        ffmt_list = None
        if len(self.backup_blkstyles) == self.imgtrans_proj.num_pages and len(self.backup_blkstyles[page_index]) == len(blk_list):
            ffmt_list: List[FontFormat] = self.backup_blkstyles[page_index]

        self.postprocess_translations(blk_list)
                
        # override font format if necessary
        override_fnt_size = pcfg.let_fntsize_flag == 1
        override_fnt_stroke = pcfg.let_fntstroke_flag == 1
        override_fnt_color = pcfg.let_fntcolor_flag == 1
        override_fnt_scolor = pcfg.let_fnt_scolor_flag == 1
        override_alignment = pcfg.let_alignment_flag == 1
        override_effect = pcfg.let_fnteffect_flag == 1
        override_writing_mode = pcfg.let_writing_mode_flag == 1
        override_font_family = pcfg.let_family_flag == 1
        gf = self.textPanel.formatpanel.global_format

        inpaint_only = pcfg.module.enable_inpaint
        inpaint_only = inpaint_only and not (pcfg.module.enable_detect or pcfg.module.enable_ocr or pcfg.module.enable_translate)
        
        if not inpaint_only:
            for ii, blk in enumerate(blk_list):
                if self._run_imgtrans_wo_textstyle_update and ffmt_list is not None:
                    blk.fontformat.merge(ffmt_list[ii])
                else:
                    if override_fnt_size or \
                        blk.font_size < 0:  # fall back to global font size if font size is not valid, it will be set to -1 for detected blocks
                        blk.font_size = gf.font_size
                    elif blk._detected_font_size > 0 and not pcfg.module.enable_detect:
                        blk.font_size = blk._detected_font_size
                    if override_fnt_stroke:
                        blk.stroke_width = gf.stroke_width
                    elif pcfg.module.enable_ocr:
                        blk.recalulate_stroke_width()
                    if override_fnt_color:
                        blk.set_font_colors(fg_colors=gf.frgb)
                    if override_fnt_scolor:
                        blk.set_font_colors(bg_colors=gf.srgb)
                    if override_alignment:
                        blk.alignment = gf.alignment
                    elif pcfg.module.enable_detect and not blk.src_is_vertical:
                        blk.recalulate_alignment()
                    if override_effect:
                        blk.opacity = gf.opacity
                        blk.shadow_color = gf.shadow_color
                        blk.shadow_radius = gf.shadow_radius
                        blk.shadow_strength = gf.shadow_strength
                        blk.shadow_offset = gf.shadow_offset
                    if override_writing_mode:
                        blk.vertical = gf.vertical
                    if override_font_family or blk.font_family is None:
                        blk.font_family = gf.font_family
                        if blk.rich_text:
                            blk.rich_text = set_html_family(blk.rich_text, gf.font_family)
                    
                    blk.line_spacing = gf.line_spacing
                    blk.letter_spacing = gf.letter_spacing
                    blk.italic = gf.italic
                    blk.bold = gf.bold
                    blk.underline = gf.underline
                    sw = blk.stroke_width
                    if sw > 0 and pcfg.module.enable_ocr and pcfg.module.enable_detect and not override_fnt_size:
                        blk.font_size = blk.font_size / (1 + sw)
                    
                    # Apply fixed font only when the option is enabled during detection-driven runs.
                    if pcfg.fixed_font_enabled and pcfg.module.enable_detect:
                        blk.font_size = pcfg.fixed_font_size
                        blk.font_family = pcfg.fixed_font_family
                        if blk.rich_text:
                            blk.rich_text = set_html_family(blk.rich_text, pcfg.fixed_font_family)
                    

            self.st_manager.auto_textlayout_flag = pcfg.let_autolayout_flag and \
                (pcfg.module.enable_detect or pcfg.module.enable_translate)
        
        
        if page_index != self.pageList.currentIndex().row():
            self.pageList.setCurrentRow(page_index)
        else:
            self.imgtrans_proj.set_current_img_byidx(page_index)
            self.canvas.updateCanvas()
            self.st_manager.updateSceneTextitems()

        if not pcfg.module.enable_detect and pcfg.module.enable_translate:
            for blkitem in self.st_manager.textblk_item_list:
                blkitem.squeezeBoundingRect()

        if page_index + 1 == self.imgtrans_proj.num_pages:
            self.st_manager.auto_textlayout_flag = False

        # save proj file on page trans finished
        self.imgtrans_proj.save()

        self.saveCurrentPage(False, False)

    def on_savestate_changed(self, unsaved: bool):
        save_state = self.tr('unsaved') if unsaved else self.tr('saved')
        self.titleBar.setTitleContent(save_state=save_state)
    
    def on_projstate_changed_for_autosave(self, unsaved: bool):
        """Restart autosave timer when project state changes"""
        if unsaved and not self.page_changing and not self.opening_dir:
            # Restart timer when there are unsaved changes
            self.autosave_timer.stop()
            self.autosave_timer.start()
    
    def on_autosave_timeout(self):
        # Dirty-flag guard: canvas.projstate_unsaved already tracks whether anything has
        # changed since the last successful save, so if it's clean we can skip the entire
        # render+encode pipeline and avoid pointless work.
        if not self.canvas.projstate_unsaved or self.page_changing or self.opening_dir:
            return
        if not self.imgtrans_proj.img_valid:
            return
        # In-flight guard: a previous autosave round is still draining through imsave_thread.
        # Skipping here prevents queue pile-up that would otherwise compound UI hitches and
        # can also reorder writes across pages. The next save_state change will reschedule us.
        if self._autosave_pending > 0 or self._autosave_running:
            return

        self._autosave_running = True
        # Defer the heavy main-thread work (scene render in saveCurrentPage) to the next
        # event-loop tick. The timer's timeout slot returns immediately, letting Qt flush
        # any queued paint events from the user's last interaction before we monopolise
        # the main thread for the render. Encode+write itself is already offloaded to
        # imsave_thread, so the only remaining synchronous cost is the unavoidable
        # QGraphicsScene.render() (Qt requires this on the main thread).
        QTimer.singleShot(0, self._run_autosave_now)

    def _run_autosave_now(self):
        try:
            # Re-check guards: state may have changed between scheduling and firing
            # (e.g. user started navigating to another page).
            if self.page_changing or self.opening_dir or not self.imgtrans_proj.img_valid:
                return
            if not self.canvas.projstate_unsaved:
                return

            should_show_rect = getattr(pcfg, 'imgtrans_textblock', False)
            selected_text_items = self.canvas.selected_text_items()
            selected_item_ids = [item.idx for item in selected_text_items]
            shape_control_visible = self.st_manager.txtblkShapeControl.isVisible()
            shape_control_blk_item_id = None
            if shape_control_visible and self.st_manager.txtblkShapeControl.blk_item is not None:
                shape_control_blk_item_id = self.st_manager.txtblkShapeControl.blk_item.idx

            self.saveCurrentPage(update_scene_text=True, save_proj=True, restore_interface=False, save_rst_only=False, _is_autosave=True)

            if len(selected_item_ids) > 0:
                self.canvas.block_selection_signal = True
                for blk_item in self.st_manager.textblk_item_list:
                    if blk_item.idx in selected_item_ids:
                        blk_item.setSelected(True)
                self.canvas.block_selection_signal = False
                self.st_manager.textEditList.set_selected_list(selected_item_ids)
            # Use idle-checked restore so autosave does not yank focus out of
            # a translation/source field the user is actively typing in.
            self._restore_canvas_focus_if_idle()

            if shape_control_visible and shape_control_blk_item_id is not None:
                for blk_item in self.st_manager.textblk_item_list:
                    if blk_item.idx == shape_control_blk_item_id:
                        self.st_manager.txtblkShapeControl.setBlkItem(blk_item)
                        break
            elif shape_control_visible:
                self.st_manager.txtblkShapeControl.show()
            if should_show_rect:
                self.st_manager.showTextblkItemRect(True)
                if not self.canvas.textblock_mode:
                    self.canvas.textblock_mode = True
            self._restore_canvas_focus_if_idle()
        finally:
            # Always release the running flag even if saveCurrentPage raised; the
            # in-flight pending counter is still authoritative for queued writes.
            self._autosave_running = False

    def on_textstack_changed(self):
        if not self.page_changing:
            self.global_search_widget.set_document_edited()

    def on_run_blktrans(self, mode: int):
        blkitem_list = self.canvas.selected_text_items()
        self.translateBlkitemList(blkitem_list, mode)

    def on_blktrans_finished(self, mode: int, blk_ids: List[int]):

        if len(blk_ids) < 1:
            return
        
        # Filter out invalid indices - the list may have changed while the async pipeline was running
        # (e.g., user changed pages, deleted blocks, etc.)
        max_idx = len(self.st_manager.textblk_item_list)
        valid_blk_ids = [idx for idx in blk_ids if 0 <= idx < max_idx]
        
        if len(valid_blk_ids) < 1:
            return
        
        blkitem_list = [self.st_manager.textblk_item_list[idx] for idx in valid_blk_ids]

        pairw_list = []
        for blk in blkitem_list:
            pairw_list.append(self.st_manager.pairwidget_list[blk.idx])
        self.canvas.push_undo_command(RunBlkTransCommand(self.canvas, blkitem_list, pairw_list, mode))

    def on_imgtrans_progressbox_showed(self):
        msg_size = self.module_manager.progress_msgbox.size()
        size = self.size()
        p = self.mapToGlobal(QPoint(size.width() - msg_size.width(),
                                    size.height() - msg_size.height()))
        self.module_manager.progress_msgbox.move(p)

    def on_closebtn_clicked(self):
        if self.imsave_thread.isRunning():
            self.imsave_thread.finished.connect(self.close)
            mb = FrameLessMessageBox()
            mb.setText(self.tr('Saving image...'))
            self.imsave_thread.finished.connect(mb.close)
            mb.exec()
            return
        self.close()

    def on_display_lang_changed(self, lang: str):
        if lang != pcfg.display_lang:
            pcfg.display_lang = lang
            self.set_display_lang(lang)
    
    def run_imgtrans(self):
        if not self.imgtrans_proj.is_all_pages_no_text and not pcfg.module.keep_exist_textlines:
            # 创建自定义消息框，添加"继续运行"选项
            msgBox = QMessageBox(self)
            msgBox.setIcon(QMessageBox.Question)
            msgBox.setWindowTitle(self.tr('Confirmation'))
            msgBox.setText(self.tr('\"Run\" will clear previous results, \"Continue\" will try to run from previous progress'))
            
            # 添加三个按钮（直接使用中文）
            restart_btn = msgBox.addButton(self.tr('Run'), QMessageBox.YesRole)
            continue_btn = msgBox.addButton(self.tr('Continue'), QMessageBox.AcceptRole)
            cancel_btn = msgBox.addButton(self.tr('Cancel'), QMessageBox.RejectRole)
            
            msgBox.setDefaultButton(continue_btn)
            msgBox.exec_()
            
            clicked_button = msgBox.clickedButton()
            if clicked_button == cancel_btn:
                return  # 取消，不执行任何操作
            elif clicked_button == continue_btn:
                # 继续运行：只处理没有文本的页面
                self.on_run_imgtrans(continue_mode=True)
                return
            # 如果是 restart_btn，继续执行下面的代码（重新运行）
        self.on_run_imgtrans()

    def run_imgtrans_wo_textstyle_update(self):
        self._run_imgtrans_wo_textstyle_update = True
        self.run_imgtrans()

    def on_run_imgtrans(self, continue_mode=False):
        self.backup_blkstyles.clear()

        # DO NOT touch checkbox state - only user can control it via W key or clicking
        self.postprocess_mt_toggle = False

        all_disabled = pcfg.module.all_stages_disabled()
        
        pages_to_process = []
        
        # 继续模式：先检查哪些页面需要处理
        if continue_mode:
            for page_name in self.imgtrans_proj.pages:
                if not self.imgtrans_proj.get_page_progress(page_name):
                    pages_to_process.append(page_name)
            if len(pages_to_process) == 0:
                return
        else:
            for page_name in self.imgtrans_proj.pages:
                self.imgtrans_proj.set_page_progress(page_name, 0)
        
        if pcfg.module.enable_detect:
            for page in self.imgtrans_proj.pages:
                if not pcfg.module.keep_exist_textlines:
                    if not pages_to_process:
                        # 没有指定pages_to_process，清空所有页面
                        self.imgtrans_proj.pages[page].clear()
        else:
            self.st_manager.updateTextBlkList()
            textblk: TextBlock = None
            for page_name, blklist in self.imgtrans_proj.pages.items():
                # 如果指定了pages_to_process，跳过不需要处理的页面
                if pages_to_process and page_name not in pages_to_process:
                    continue
                    
                ffmt_list = []
                self.backup_blkstyles.append(ffmt_list)
                for textblk in blklist:
                    if not pcfg.module.enable_detect:
                        ffmt_list.append(textblk.fontformat.deepcopy())
                    # 继续模式且没有指定pages_to_process时：跳过已有文本的文本块
                    if continue_mode and not pages_to_process and textblk.text and len(textblk.text) > 0:
                        continue
                    if pcfg.module.enable_ocr:
                        textblk.text = []
                        textblk.set_font_colors((0, 0, 0), (0, 0, 0))
                    if pcfg.module.enable_translate or (all_disabled and not self._run_imgtrans_wo_textstyle_update) or pcfg.module.enable_ocr:
                        textblk.rich_text = ''
                    textblk.vertical = textblk.src_is_vertical
        
        # 如果有指定pages_to_process或者是continue_mode，则传递页面列表
        self.module_manager.runImgtransPipeline(pages_to_process if (pages_to_process or continue_mode) else None)
        
        # Restore canvas focus when starting the pipeline so A/D/N/W still
        # respond once the run begins. Idle-checked so we never yank focus out
        # of a translation field if the user kicked off the run from there.
        self._restore_canvas_focus_if_idle()
        QTimer.singleShot(100, self._restore_canvas_focus_if_idle)

    def on_transpanel_changed(self):
        self.canvas.editor_index = self.rightComicTransStackPanel.currentIndex()
        if not self.canvas.textEditMode() and self.canvas.search_widget.isVisible():
            self.canvas.search_widget.hide()
        self.canvas.updateLayers()

    def import_tstyles(self):
        ddir = osp.dirname(pcfg.text_styles_path)
        p = QFileDialog.getOpenFileName(self, self.tr("Import Text Styles"), ddir, None, "(.json)")
        if not isinstance(p, str):
            p = p[0]
        if p == '':
            return
        try:
            load_textstyle_from(p, raise_exception=True)
            save_config()
            self.textPanel.formatpanel.textstyle_panel.setStyles(text_styles)
        except Exception as e:
            create_error_dialog(e, self.tr(f'Failed to load from {p}'))

    def export_tstyles(self):
        ddir = osp.dirname(pcfg.text_styles_path)
        savep = QFileDialog.getSaveFileName(self, self.tr("Save Text Styles"), ddir, None, "(.json)")
        if not isinstance(savep, str):
            savep = savep[0]
        if savep == '':
            return
        suffix = Path(savep).suffix
        if suffix != '.json':
            if suffix == '':
                savep = savep + '.json'
            else:
                savep = savep.replace(suffix, '.json')
        oldp = pcfg.text_styles_path
        try:
            pcfg.text_styles_path = savep
            save_text_styles(raise_exception=True)
            save_config()
        except Exception as e:
            create_error_dialog(e, self.tr(f'Failed save to {savep}'))
            pcfg.text_styles_path = oldp

    def fold_textarea(self, fold: bool):
        pcfg.fold_textarea = fold
        self.textPanel.textEditList.setFoldTextarea(fold)

    def show_source_text(self, show: bool):
        pcfg.show_source_text = show
        self.textPanel.textEditList.setSourceVisible(show)

    def show_trans_text(self, show: bool):
        pcfg.show_trans_text = show
        self.textPanel.textEditList.setTransVisible(show)

    def on_export_doc(self):
        if self.canvas.text_change_unsaved():
            self.st_manager.updateTextBlkList()
        self.export_doc_thread.exportAsDoc(self.imgtrans_proj)

    def on_import_doc(self):
        self.import_doc_thread.importDoc(self.imgtrans_proj)

    def save_all_pages(self):
        """Save all pages before export"""
        if not self.imgtrans_proj.img_valid:
            return
        
        current_page = self.imgtrans_proj.current_img
        original_save_on_page_changed = self.save_on_page_changed
        self.save_on_page_changed = False  # Disable auto-save during batch save
        
        try:
            # Save current page first
            if self.canvas.projstate_unsaved or self.canvas.text_change_unsaved():
                self.saveCurrentPage(update_scene_text=True, save_proj=False, restore_interface=False, save_rst_only=False)
            
            # Save all other pages
            for page_name in self.imgtrans_proj.pages:
                if page_name != current_page:
                    # Switch to page
                    self.page_changing = True
                    self.imgtrans_proj.set_current_img(page_name)
                    self.canvas.clear_undostack(update_saved_step=True)
                    self.canvas.updateCanvas()
                    self.st_manager.updateSceneTextitems()
                    self.titleBar.setTitleContent(page_name=self.imgtrans_proj.current_img)
                    self.page_changing = False
                    
                    # Check if page has unsaved changes (need to check after updateSceneTextitems)
                    # Note: text_change_unsaved() checks current page, so we need to update first
                    if self.canvas.projstate_unsaved:
                        self.saveCurrentPage(update_scene_text=True, save_proj=False, restore_interface=False, save_rst_only=False)
            
            # Restore original page
            self.page_changing = True
            self.imgtrans_proj.set_current_img(current_page)
            self.canvas.clear_undostack(update_saved_step=True)
            self.canvas.updateCanvas()
            self.st_manager.updateSceneTextitems()
            self.titleBar.setTitleContent(page_name=self.imgtrans_proj.current_img)
            self.page_changing = False
            
            # Save project file
            self.imgtrans_proj.save()
        finally:
            self.save_on_page_changed = original_save_on_page_changed
    
    def on_export_src_txt_quick(self):
        """Export source to TXT from quick menu - save all pages first"""
        try:
            # Save all pages before export
            self.save_all_pages()
            # Export
            self.on_export_txt(dump_target='source', suffix='.txt')
            QTimer.singleShot(300, self._restore_canvas_focus_if_idle)
        except Exception as e:
            create_error_dialog(e, self.tr('Failed to export source as TEXT file'))
            QTimer.singleShot(200, self._restore_canvas_focus_if_idle)

    def on_import_trans_txt_quick(self):
        """Import translation TXT from quick menu - ensure save system works"""
        try:
            # Import translation
            self.on_import_trans_txt()
            # After import, save the project to ensure changes are saved
            if self.imgtrans_proj.img_valid:
                # Save current page if there are changes
                if self.canvas.projstate_unsaved or self.canvas.text_change_unsaved():
                    self.saveCurrentPage(update_scene_text=True, save_proj=True, restore_interface=False, save_rst_only=False)
            QTimer.singleShot(300, self._restore_canvas_focus_if_idle)
        except Exception as e:
            create_error_dialog(e, self.tr('Failed to import translation from TXT file'))
            QTimer.singleShot(200, self._restore_canvas_focus_if_idle)

    def on_export_txt(self, dump_target, suffix='.txt'):
        try:
            self.imgtrans_proj.dump_txt(dump_target=dump_target, suffix=suffix)
            create_info_dialog(self.tr('Text file exported to ') + self.imgtrans_proj.dump_txt_path(dump_target, suffix))
            QTimer.singleShot(200, self._restore_canvas_focus_if_idle)
        except Exception as e:
            create_error_dialog(e, self.tr('Failed to export as TEXT file'))
            QTimer.singleShot(200, self._restore_canvas_focus_if_idle)

    def on_import_trans_txt(self):
        try:
            selected_file = ''
            dialog = QFileDialog()
            selected_file = str(dialog.getOpenFileUrl(self.parent(), self.tr('Import *.md/*.txt'), filter="*.txt *.md *.TXT *.MD")[0].toLocalFile())
            if not osp.exists(selected_file):
                return

            all_matched, match_rst = self.imgtrans_proj.load_translation_from_txt(selected_file)
            matched_pages = match_rst['matched_pages']

            if self.imgtrans_proj.current_img in matched_pages:
                self.canvas.clear_undostack(update_saved_step=True)
                self.st_manager.updateSceneTextitems()
                # Restore text block mode after updating scene items
                if self.bottomBar.textblockChecker.isChecked() or pcfg.imgtrans_textblock:
                    self.setTextBlockMode()
                # Restore canvas focus after import (idle-checked).
                QTimer.singleShot(100, self._restore_canvas_focus_if_idle)

            if all_matched:
                msg = self.tr('Translation imported and matched successfully.')
            else:
                msg = self.tr('Imported txt file not fully matched with current project, please make sure source txt file structured like results from \"export TXT/markdown\"')
                if len(match_rst['missing_pages']) > 0:
                    msg += '\n' + self.tr('Missing pages: ') + '\n'
                    msg += '\n'.join(match_rst['missing_pages'])
                if len(match_rst['unexpected_pages']) > 0:
                    msg += '\n' + self.tr('Unexpected pages: ') + '\n'
                    msg += '\n'.join(match_rst['unexpected_pages'])
                if len(match_rst['unmatched_pages']) > 0:
                    msg += '\n' + self.tr('Unmatched pages: ') + '\n'
                    msg += '\n'.join(match_rst['unmatched_pages'])
                msg = msg.strip()

            for pagename in matched_pages:
                for blk in self.imgtrans_proj.pages[pagename]:
                    blk.translation = self.mtSubWidget.sub_text(blk.translation)
            
            create_info_dialog(msg)
            QTimer.singleShot(200, self._restore_canvas_focus_if_idle)

        except Exception as e:
            create_error_dialog(e, self.tr('Failed to import translation from ') + selected_file)
            QTimer.singleShot(200, self._restore_canvas_focus_if_idle)

    def on_reveal_file(self):
        current_img_path = self.imgtrans_proj.current_img_path()
        if sys.platform == 'win32':
            # qprocess seems to fuck up with "\""
            p = "\""+str(Path(current_img_path))+"\""
            subprocess.Popen("explorer.exe /select,"+p, shell=True)
        elif sys.platform == 'darwin':
            p = "\""+current_img_path+"\""
            subprocess.Popen("open -R "+p, shell=True)

    def on_set_gsearch_widget(self):
        setup = self.leftBar.globalSearchChecker.isChecked()
        if setup:
            if self.leftStackWidget.isHidden():
                self.leftStackWidget.show()
            self.leftBar.showPageListLabel.setChecked(False)
            self.leftStackWidget.setCurrentWidget(self.global_search_widget)
        else:
            self.leftStackWidget.hide()

    def on_fin_export_doc(self):
        # Do NOT restore canvas focus before exec_(): exec_() runs a nested
        # event loop, so a pre-scheduled timer would fire while the popup is
        # still open and steal focus from its OK button, breaking Spacebar.
        msg = QMessageBox()
        msg.setText(self.tr('Export to ') + self.imgtrans_proj.doc_path())
        msg.exec_()
        QTimer.singleShot(200, self._restore_canvas_focus_if_idle)

    def on_fin_import_doc(self):
        self.st_manager.updateSceneTextitems()
        # Restore text block mode after updating scene items
        if self.bottomBar.textblockChecker.isChecked() or pcfg.imgtrans_textblock:
            self.setTextBlockMode()
        QTimer.singleShot(100, self._restore_canvas_focus_if_idle)

    def on_global_replace_finished(self):
        rt = self.global_search_widget.replace_thread
        self.canvas.push_text_command(
            GlobalRepalceAllCommand(rt.sceneitem_list, rt.background_list, rt.target_text, self.imgtrans_proj)
        )
        rt.sceneitem_list = None
        rt.background_list = None

    def on_darkmode_triggered(self):
        pcfg.darkmode = self.titleBar.darkModeAction.isChecked()
        self.resetStyleSheet(reverse_icon=True)
        self.save_config()

    def ocr_postprocess(self, textblocks: List[TextBlock], img, ocr_module=None, **kwargs):
        for blk in textblocks:
            text = blk.get_text()
            blk.text = self.ocrSubWidget.sub_text(text)

        # 字体检测：在 OCR 完成后按配置执行（按需导入以减少启动开销）
        # All errors are non-fatal -- font detect is a post-process enrichment,
        # so log + clear the per-block field rather than aborting OCR results.
        if pcfg.module.ocr_font_detect:
            try:
                from utils import font_detect
            except Exception:
                LOGGER.exception('failed to import font_detect module')
                return
            for blk in textblocks:
                try:
                    name, conf = font_detect.detect_font_from_block(img, blk)
                    blk._detected_font_name = name
                    blk._detected_font_confidence = float(conf)
                except Exception:
                    LOGGER.exception('font_detect failed on a textblock')
                    blk._detected_font_name = ''
                    blk._detected_font_confidence = 0.0

        # NOTE: Do NOT reset angle here - angle will be reset after pipeline finished in on_imgtrans_pipeline_finished()
        # This prevents angle from being reset when creating new text blocks or during OCR

    def translate_preprocess(self, translations: List[str] = None, textblocks: List[TextBlock] = None, translator = None, source_text:list = []):
        for i in range(len(source_text)):
            source_text[i] = self.mtPreSubWidget.sub_text(source_text[i])

    def translate_postprocess(self, translations: List[str] = None, textblocks: List[TextBlock] = None, translator = None):
        if not self.postprocess_mt_toggle:
            return
        
        for ii, tr in enumerate(translations):
            translations[ii] = self.mtSubWidget.sub_text(tr)

    def on_copy_src(self):
        blks = self.canvas.selected_text_items()
        if len(blks) == 0:
            return
        
        if isinstance(self.module_manager.translator, GPTTranslator):
            src_list = [self.st_manager.pairwidget_list[blk.idx].e_source.toPlainText() for blk in blks]
            src_txt = ''
            for (prompt, num_src) in self.module_manager.translator._assemble_prompts(src_list, max_tokens=4294967295):
                src_txt += prompt
            src_txt = src_txt.strip()
        else:
            src_list = [self.st_manager.pairwidget_list[blk.idx].e_source.toPlainText().strip().replace('\n', ' ') for blk in blks]
            src_txt = '\n'.join(src_list)

        self.st_manager.app_clipborad.setText(src_txt, QClipboard.Mode.Clipboard)

    def on_paste_src(self):
        blks = self.canvas.selected_text_items()
        if len(blks) == 0:
            return

        src_widget_list = [self.st_manager.pairwidget_list[blk.idx].e_source for blk in blks]
        text_list = self.st_manager.app_clipborad.text().split('\n')
        
        n_paragraph = min(len(src_widget_list), len(text_list))
        if n_paragraph < 1:
            return
        
        src_widget_list = src_widget_list[:n_paragraph]
        text_list = text_list[:n_paragraph]

        self.canvas.push_undo_command(PasteSrcItemsCommand(src_widget_list, text_list))
    
    def run_batch(self, exec_dirs: Union[List, str], **kwargs):
        if not isinstance(exec_dirs, List):
            exec_dirs = exec_dirs.split(',')
        valid_dirs = []
        for d in exec_dirs:
            if osp.exists(d):
                valid_dirs.append(d)
            else:
                LOGGER.warning(f'target directory {d} does not exist.')
        self.exec_dirs = valid_dirs
        self.run_next_dir()

    def run_next_dir(self):
        if len(self.exec_dirs) == 0:
            # Wait for queued saves before quitting (headless batch mode).
            # QThread.wait yields on an OS primitive instead of a 100ms poll.
            if self.imsave_thread.isRunning():
                self.imsave_thread.wait(30000)
            LOGGER.info(f'finished translating all dirs, quit app...')
            self.app.quit()
            return
        d = self.exec_dirs.pop(0)
        
        LOGGER.info(f'translating {d} ...')
        self.openDir(d)
        shared.pbar = {}
        npages = len(self.imgtrans_proj.pages)
        if npages > 0:
            if pcfg.module.enable_detect:
                shared.pbar['detect'] = tqdm(range(npages), desc="Text Detection")
            if pcfg.module.enable_ocr:
                shared.pbar['ocr'] = tqdm(range(npages), desc="OCR")
            if pcfg.module.enable_translate:
                shared.pbar['translate'] = tqdm(range(npages), desc="Translation")
            if pcfg.module.enable_inpaint:
                shared.pbar['inpaint'] = tqdm(range(npages), desc="Inpaint")
        self.on_run_imgtrans()

    def on_create_errdialog(self, error_msg: str, detail_traceback: str = '', exception_type: str = ''):
        try:
            if exception_type != '':
                shared.showed_exception.add(exception_type)
            err = QMessageBox()
            err.setText(error_msg)
            err.setDetailedText(detail_traceback)
            err.exec()
            if exception_type != '':
                shared.showed_exception.remove(exception_type)
        except Exception:
            if exception_type in shared.showed_exception:
                shared.showed_exception.remove(exception_type)
            LOGGER.error('Failed to create error dialog')
            LOGGER.error(traceback.format_exc())

    def on_create_infodialog(self, info_dict: dict):
        QMessageBox.StandardButton.NoButton
        dialog = MessageBox(**info_dict)
        dialog.show()   # exec_ will block main thread

    def setupRegisterWidget(self):
        self.titleBar.viewMenu.addSeparator()
        for cfg_name in shared.config_name_to_view_widget:
            d = shared.config_name_to_view_widget[cfg_name]
            widget: ViewWidget = d['widget']
            action = QAction(widget.action_name, self.titleBar)
            action.setCheckable(True)
            visible = getattr(pcfg, cfg_name)
            action.setChecked(visible)
            action.triggered.connect(self.action_set_view_visible)
            self.titleBar.viewMenu.addAction(action)
            d['action'] = action
            shared.action_to_view_config_name[action] = cfg_name
            widget.set_expend_area(expend=getattr(pcfg, widget.config_expand_name), set_config=False)
            widget.view_hide_btn_clicked.connect(self.on_hide_view_widget)
            widget.setVisible(visible)

    def register_view_widget(self, widget: ViewWidget):
        assert widget.config_name not in shared.config_name_to_view_widget
        d = {'widget': widget}
        shared.config_name_to_view_widget[widget.config_name] = d

    def action_set_view_visible(self):
        action: QAction = self.sender()
        show = action.isChecked()
        cfg_name = shared.action_to_view_config_name[action]
        widget: ViewWidget = shared.config_name_to_view_widget[cfg_name]['widget']
        widget.setVisible(show)
        setattr(pcfg, cfg_name, show)

    def on_hide_view_widget(self, cfg_name: str):
        d = shared.config_name_to_view_widget[cfg_name]
        widget: ViewWidget = d['widget']
        widget.setVisible(False)
        action: QAction = d['action']
        action.setChecked(False)
        setattr(pcfg, cfg_name, False)
