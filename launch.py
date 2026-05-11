from pathlib import Path
import sys
import argparse
import os.path as osp
import os
import importlib
import subprocess
import json
import hashlib
import time
import faulthandler
from platform import platform
from pathlib import Path

# Dump native stack trace on segfault / abort / Qt assertion failure.
# Without this, Qt C++ crashes exit silently with no Python traceback,
# making "process just disappeared" bugs impossible to diagnose.
faulthandler.enable()

BRANCH = 'dev'
VERSION = '1.4.0'

python = sys.executable
git = os.environ.get('GIT', "git")
skip_install = False
index_url = os.environ.get('INDEX_URL', "")
QT_APIS = ['pyqt6', 'pyside6', 'pyqt5', 'pyside2']
stored_commit_hash = None

REQ_WIN = [
    'pywin32'
]

PATH_ROOT=Path(__file__).parent
PATH_FONTS=str(PATH_ROOT/'fonts')
FONT_EXTS = {'.ttf','.otf','.ttc','.pfb'}

PATH_TMP = str(PATH_ROOT/'tmp')
PATH_GPU_CACHE = osp.join(PATH_TMP, 'gpu_cache.json')
PATH_REQS_HASH = osp.join(PATH_TMP, '.reqs_hash')
PATH_FONTS_CACHE = osp.join(PATH_TMP, 'fonts_cache.json')
GPU_CACHE_TTL = 604800  # 7 days


def _ensure_tmp_dir():
    try:
        os.makedirs(PATH_TMP, exist_ok=True)
    except Exception:
        pass

IS_WIN7 = "Windows-7" in platform()

import utils.shared as shared # Earlier import of shared to use default for config_path argument

parser = argparse.ArgumentParser()
parser.add_argument("--reinstall-torch", action='store_true', help="launch.py argument: install the appropriate version of torch even if you have some version already installed")
parser.add_argument("--proj-dir", default='', type=str, help='Open project directory on startup')
if IS_WIN7:
    parser.add_argument("--qt-api", default='pyqt5', choices=QT_APIS, help='Set qt api')
else:
    parser.add_argument("--qt-api", default='pyqt6', choices=QT_APIS, help='Set qt api')
parser.add_argument("--debug", action='store_true')
parser.add_argument("--requirements", default='requirements.txt')
parser.add_argument("--headless", action='store_true', help='run without GUI')
parser.add_argument("--exec_dirs", default='', help='translation queue (project directories) separated by comma')
parser.add_argument("--ldpi", default=None, type=float, help='logical dots perinch')
parser.add_argument("--export-translation-txt", action='store_true', help='save translation to txt file once RUN completed')
parser.add_argument("--export-source-txt", action='store_true', help='save source to txt file once RUN completed')
parser.add_argument("--frozen", action='store_true', help='run without checking requirements')
parser.add_argument("--update", action='store_true', help="Update the repository before launching") # Add argument --update
parser.add_argument("--config_path", default=shared.CONFIG_PATH, help='Config file to use for translation') # Named config_path to avoid conflict with existing name config
parser.add_argument('--nightly', action='store_true', help="Enable AMD Nightly ROCm")
args, _ = parser.parse_known_args()


def is_installed(package):
    try:
        spec = importlib.util.find_spec(package)
    except ModuleNotFoundError:
        return False

    return spec is not None


def run(command, desc=None, errdesc=None, custom_env=None, live=False):
    if desc is not None:
        print(desc)

    if live:
        result = subprocess.run(command, shell=True, env=os.environ if custom_env is None else custom_env)
        if result.returncode != 0:
            raise RuntimeError(f"""{errdesc or 'Error running command'}.
Command: {command}
Error code: {result.returncode}""")

        return ""

    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, env=os.environ if custom_env is None else custom_env)

    if result.returncode != 0:

        message = f"""{errdesc or 'Error running command'}.
Command: {command}
Error code: {result.returncode}
stdout: {result.stdout.decode(encoding="utf8", errors="ignore") if len(result.stdout)>0 else '<empty>'}
stderr: {result.stderr.decode(encoding="utf8", errors="ignore") if len(result.stderr)>0 else '<empty>'}
"""
        raise RuntimeError(message)

    return result.stdout.decode(encoding="utf8", errors="ignore")


def run_pip(args, desc=None):
    if skip_install:
        return

    index_url_line = f' --index-url {index_url}' if index_url != '' else ''
    return run(f'"{python}" -m pip {args} --prefer-binary{index_url_line} --disable-pip-version-check --no-warn-script-location', desc=f"Installing {desc}", errdesc=f"Couldn't install {desc}", live=True)


def commit_hash():
    global stored_commit_hash

    if stored_commit_hash is not None:
        return stored_commit_hash

    try:
        stored_commit_hash = run(f"{git} rev-parse HEAD").strip()
    except Exception:
        stored_commit_hash = "<none>"

    return stored_commit_hash


def current_branch():
    try:
        branch = run(f"{git} branch --show-current").strip()
        if branch:
            return branch
    except Exception:
        pass
    return BRANCH


def remote_ref_exists(remote_name: str, branch_name: str) -> bool:
    try:
        run(f"{git} ls-remote --exit-code --heads {remote_name} {branch_name}", errdesc=f"Failed to query {remote_name}/{branch_name}")
        return True
    except Exception:
        return False


def update_repository():
    branch = current_branch()
    remote = 'origin'
    target_branch = branch if remote_ref_exists(remote, branch) else BRANCH
    target_ref = f"{remote}/{target_branch}"

    print(f'Checking for updates on {target_ref}...')

    current_commit = commit_hash()
    run(f"{git} fetch --depth 1 {remote} {target_branch}", desc=f"Fetching updates from {target_ref}...", errdesc=f"Failed to fetch updates from {target_ref}.")
    latest_commit = run(f"{git} rev-parse {target_ref}").strip()

    if current_commit == latest_commit:
        print("No updates found.")
        return

    print(f"New updates found on {target_ref}. Updating repository...")
    run(f"{git} pull --ff-only {remote} {target_branch}", desc=f"Updating repository from {target_ref}...", errdesc=f"Failed to update repository from {target_ref}.")
    print("Repository updated. Restarting to apply updates...")
    restart()


BT = None
APP = None

def restart():
    global BT
    print('restarting...\n')
    if BT:
        BT.close()
    os.execv(sys.executable, ['python'] + sys.argv)


def setup_locks():
    from utils.lock import RUNTIME_LOCKS
    from qtpy.QtCore import QMutex
    RUNTIME_LOCKS['model_loading'] = QMutex()


def main():

    if args.debug:
        os.environ['BALLOONTRANS_DEBUG'] = '1'

    os.environ['QT_API'] = args.qt_api

    commit = commit_hash()

    print('Python version: ', sys.version)
    print('Python executable: ', sys.executable)
    print(f'Version: {VERSION}')
    print(f'Branch: {current_branch()}')
    print(f"Commit hash: {commit}")

    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    os.chdir(APP_DIR)

    prepare_environment()

    from utils.zluda_config import enable_zluda_config
    enable_zluda_config()

    if args.update:
        if getattr(sys, 'frozen', False):
            print('Running as app, skipping update.')
        else:
            try:
                update_repository()
            except Exception as e:
                print(f"Update check failed: {e}")
                print("Continuing with the current version.")


    from utils.logger import setup_logging, logger as LOGGER
    from utils.io_utils import find_all_files_recursive
    from utils import config as program_config

    from qtpy.QtCore import QTranslator, QLocale, Qt
    shared.args = args
    shared.DEFAULT_DISPLAY_LANG = QLocale.system().name().replace('en_CN', 'zh_CN')
    shared.HEADLESS = args.headless
    shared.load_cache()
    program_config.load_config(args.config_path)
    config = program_config.pcfg

    if args.headless:
        config.module.load_model_on_demand = True
        config.module.empty_runcache = False

    if sys.platform == 'win32':
        import ctypes
        myappid = u'BalloonsTranslator' # arbitrary string
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

    import qtpy
    from qtpy.QtWidgets import QApplication
    from qtpy.QtGui import QIcon, QFontDatabase, QGuiApplication, QFont
    from qtpy import API, QT_VERSION

    LOGGER.info(f'QT_API: {API}, QT Version: {QT_VERSION}')

    shared.DEBUG = args.debug
    shared.USE_PYSIDE6 = API == 'pyside6'
    if qtpy.API_NAME[-1] == '6':
        shared.FLAG_QT6 = True
    else:
        shared.FLAG_QT6 = False
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True) #enable high dpi scaling
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True) #use high dpi icons
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    os.chdir(shared.PROGRAM_PATH)

    setup_logging(shared.LOGGING_PATH)

    # Increase QImage allocation limit to support large images (default is 256MB)
    # Set to 0 to disable limit, or set a higher value in MB
    # This allows loading images larger than 256MB without rejection
    if 'QT_IMAGEIO_MAXALLOC' not in os.environ:
        os.environ['QT_IMAGEIO_MAXALLOC'] = '0'  # 0 = no limit
    if 'QT_IMAGEIO_MAXALLOC_MB' not in os.environ:
        os.environ['QT_IMAGEIO_MAXALLOC_MB'] = '0'  # 0 = no limit

    app_args = sys.argv
    if args.headless:
        app_args = sys.argv + ['-platform', 'offscreen']
    app = QApplication(app_args)
    app.setApplicationName('BalloonsTranslator')
    app.setApplicationVersion(VERSION)

    # Splash screen — shown ASAP after QApplication so users see feedback during the
    # multi-second module/UI init that follows. Skipped in headless (offscreen platform).
    splash = None
    if not args.headless:
        try:
            from qtpy.QtWidgets import QSplashScreen
            from qtpy.QtGui import QPixmap, QPainter, QColor, QFont as _QFont
            from qtpy.QtCore import Qt as _Qt

            pixmap = None
            icon_path = getattr(shared, 'ICON_PATH', None)
            if icon_path and osp.exists(icon_path):
                # Compose dark splash with embedded icon + title rather than raw icon
                # so users see "BalloonsTranslator vX.Y.Z" instead of just an icon.
                base = QPixmap(480, 270)
                base.fill(QColor('#1e1e2e'))
                icon_pm = QPixmap(icon_path)
                if not icon_pm.isNull():
                    icon_scaled = icon_pm.scaled(
                        96, 96,
                        _Qt.AspectRatioMode.KeepAspectRatio,
                        _Qt.TransformationMode.SmoothTransformation,
                    )
                    painter = QPainter(base)
                    painter.drawPixmap(
                        (base.width() - icon_scaled.width()) // 2, 50,
                        icon_scaled,
                    )
                    painter.setPen(QColor('#ffffff'))
                    title_font = _QFont()
                    title_font.setPointSize(16)
                    title_font.setBold(True)
                    painter.setFont(title_font)
                    painter.drawText(
                        0, 160, base.width(), 30,
                        int(_Qt.AlignmentFlag.AlignHCenter | _Qt.AlignmentFlag.AlignVCenter),
                        'BalloonsTranslator',
                    )
                    ver_font = _QFont()
                    ver_font.setPointSize(10)
                    painter.setFont(ver_font)
                    painter.setPen(QColor('#a0a0b0'))
                    painter.drawText(
                        0, 195, base.width(), 20,
                        int(_Qt.AlignmentFlag.AlignHCenter | _Qt.AlignmentFlag.AlignVCenter),
                        f'v{VERSION}',
                    )
                    painter.end()
                    pixmap = base

            if pixmap is None:
                # Fallback when icon is missing: pure painted splash, same dimensions.
                pixmap = QPixmap(480, 270)
                pixmap.fill(QColor('#1e1e2e'))
                painter = QPainter(pixmap)
                painter.setPen(QColor('#ffffff'))
                title_font = _QFont()
                title_font.setPointSize(18)
                title_font.setBold(True)
                painter.setFont(title_font)
                painter.drawText(
                    0, 110, pixmap.width(), 40,
                    int(_Qt.AlignmentFlag.AlignHCenter | _Qt.AlignmentFlag.AlignVCenter),
                    'BalloonsTranslator',
                )
                ver_font = _QFont()
                ver_font.setPointSize(11)
                painter.setFont(ver_font)
                painter.setPen(QColor('#a0a0b0'))
                painter.drawText(
                    0, 155, pixmap.width(), 24,
                    int(_Qt.AlignmentFlag.AlignHCenter | _Qt.AlignmentFlag.AlignVCenter),
                    f'v{VERSION}',
                )
                painter.end()

            splash = QSplashScreen(pixmap)
            splash.show()
            app.processEvents()
        except Exception:
            splash = None

    def _splash_msg(text: str):
        # Centralised so we never crash startup if Qt drawing fails mid-init.
        if splash is None:
            return
        try:
            from qtpy.QtCore import Qt as _Qt
            from qtpy.QtGui import QColor
            splash.showMessage(
                text,
                int(_Qt.AlignmentFlag.AlignBottom | _Qt.AlignmentFlag.AlignHCenter),
                QColor('#e0e0e0'),
            )
            app.processEvents()
        except Exception:
            pass

    # import msl.loadlib (required by translators/trans_eztrans) before init QApplication
    # yield QWindowsContext: OleInitialize() failed on py3.10,
    from modules.base import init_module_registries
    from modules.prepare_local_files import prepare_local_files_forall
    _splash_msg('Loading modules...')
    init_module_registries()
    prepare_local_files_forall()

    if not args.headless:
        ps = QGuiApplication.primaryScreen()
        shared.LDPI = ps.logicalDotsPerInch()
        shared.SCREEN_W = ps.geometry().width()
        shared.SCREEN_H = ps.geometry().height()

    # Force English language to avoid file lookup overhead
    lang = 'English'
    LOGGER.info(f'set display language to {lang}')

    # Fonts
    # Load custom fonts if they exist
    _splash_msg('Loading fonts...')
    if osp.exists(PATH_FONTS):
        font_files = None
        try:
            current_root_mtime = os.path.getmtime(PATH_FONTS)
        except Exception:
            current_root_mtime = None

        scan_root_abs = osp.abspath(PATH_FONTS)
        cached_payload = None
        if osp.exists(PATH_FONTS_CACHE):
            try:
                with open(PATH_FONTS_CACHE, 'r', encoding='utf-8') as f:
                    cached_payload = json.load(f)
            except Exception:
                try:
                    os.remove(PATH_FONTS_CACHE)
                except Exception:
                    pass
                cached_payload = None

        # Reuse cached file list when fonts/ root mtime is unchanged.
        # Note: on Windows folder mtime won't always reflect changes inside subdirectories — accepted
        # as a trade-off; users adding fonts can delete tmp/fonts_cache.json to force rebuild.
        if (cached_payload
                and cached_payload.get('scan_root') == scan_root_abs
                and current_root_mtime is not None
                and float(cached_payload.get('root_mtime', -1)) == float(current_root_mtime)):
            try:
                font_files = [entry['path'] for entry in cached_payload.get('files', [])
                              if osp.exists(entry.get('path', ''))]
            except Exception:
                font_files = None

        if font_files is None:
            font_files = list(find_all_files_recursive(PATH_FONTS, FONT_EXTS))
            try:
                _ensure_tmp_dir()
                entries = []
                for fp in font_files:
                    try:
                        entries.append({"path": fp, "mtime": os.path.getmtime(fp)})
                    except Exception:
                        entries.append({"path": fp, "mtime": 0})
                payload = {
                    "scan_root": scan_root_abs,
                    "root_mtime": current_root_mtime if current_root_mtime is not None else 0,
                    "files": entries,
                }
                with open(PATH_FONTS_CACHE, 'w', encoding='utf-8') as f:
                    json.dump(payload, f)
            except Exception:
                pass

        for fp in font_files:
            fnt_idx = QFontDatabase.addApplicationFont(fp)
            if fnt_idx >= 0:
                shared.CUSTOM_FONTS.append(QFontDatabase.applicationFontFamilies(fnt_idx)[0])

    if sys.platform == 'win32' and args.headless:
        # font database does not initialise on windows with qpa -offscreen:
        # whttps://github.com/dmMaze/BallonsTranslator/issues/519
        from qtpy.QtCore import QStandardPaths
        font_dir_list = QStandardPaths.standardLocations(QStandardPaths.StandardLocation.FontsLocation)
        for fd in font_dir_list:
            fp_list = find_all_files_recursive(fd, FONT_EXTS)
            for fp in fp_list:
                fnt_idx = QFontDatabase.addApplicationFont(fp)

    if shared.FLAG_QT6:
        shared.FONT_FAMILIES = set(f for f in QFontDatabase.families())
    else:
        fdb = QFontDatabase()
        shared.FONT_FAMILIES = set(fdb.families())

    app_font = QFont('Microsoft YaHei UI')
    if not app_font.exactMatch() or sys.platform == 'darwin':
        app_font = app.font()
    app_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.NoSubpixelAntialias)
    QGuiApplication.setFont(app_font)
    shared.DEFAULT_FONT_FAMILY = app_font.family()
    shared.APP_DEFAULT_FONT = app_font.family()
    
    if args.ldpi:
        shared.LDPI = args.ldpi

    setup_locks()

    _splash_msg('Initializing UI...')
    from ui.mainwindow import MainWindow
    ballontrans = MainWindow(app, config, open_dir=args.proj_dir, **vars(args))
    global BT
    BT = ballontrans
    BT.restart_signal.connect(restart)

    if not args.headless:
        if shared.SCREEN_W > 1707 and sys.platform == 'win32':   # higher than 2560 (1440p) / 1.5
            # https://github.com/dmMaze/BallonsTranslator/issues/220
            BT.comicTransSplitter.setHandleWidth(7)

        ballontrans.setWindowIcon(QIcon(shared.ICON_PATH))
        ballontrans.show()
        if splash is not None:
            try:
                splash.finish(ballontrans)
            except Exception:
                pass
        ballontrans.resetStyleSheet()
    sys.exit(app.exec())

def _detect_gpu_names_win():
    # Prefer PowerShell Get-CimInstance over deprecated wmic; ~10x faster startup.
    cmd = ['powershell', '-NoProfile', '-Command',
           'Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name']
    output = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    return [line.strip() for line in output.splitlines() if line.strip()]


def _classify_amd_nightly(gpu_names):
    joined = '\n'.join(gpu_names)
    if any(keyword in joined for keyword in
           ["RX 7900", "RX 7800", "RX 7700", "RX 7600", "PRO W7900", "PRO W7800", "PRO W7700"]):
        return "RDNA3"
    if any(keyword in joined for keyword in ["RX 9070", "RX 9060"]):
        return "RDNA4"
    return "None"


def _get_gpu_info():
    """Returns dict {gpu_names, is_amd, amd_nightly}. Cached for GPU_CACHE_TTL seconds."""
    if sys.platform != 'win32':
        return {"gpu_names": [], "is_amd": False, "amd_nightly": "None"}

    now = time.time()

    # Try cache first
    if osp.exists(PATH_GPU_CACHE):
        try:
            with open(PATH_GPU_CACHE, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if now - float(cached.get('cached_at', 0)) < GPU_CACHE_TTL:
                return {
                    "gpu_names": cached.get('gpu_names', []),
                    "is_amd": bool(cached.get('is_amd', False)),
                    "amd_nightly": cached.get('amd_nightly', 'None'),
                }
        except Exception:
            try:
                os.remove(PATH_GPU_CACHE)
            except Exception:
                pass

    try:
        gpu_names = _detect_gpu_names_win()
    except Exception:
        return {"gpu_names": [], "is_amd": False, "amd_nightly": "None"}

    joined = '\n'.join(gpu_names)
    is_amd = any(keyword in joined for keyword in ["AMD", "Radeon"])
    amd_nightly = _classify_amd_nightly(gpu_names) if is_amd else "None"

    info = {"gpu_names": gpu_names, "is_amd": is_amd, "amd_nightly": amd_nightly}

    try:
        _ensure_tmp_dir()
        payload = {"cached_at": now, **info}
        with open(PATH_GPU_CACHE, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
    except Exception:
        pass

    return info


def is_amd_gpu():
    try:
        return _get_gpu_info()["is_amd"]
    except Exception:
        return False


def supported_amd_nightly_gpu():
    try:
        if sys.platform != 'win32':
            return "None"
        return _get_gpu_info().get("amd_nightly", "None")
    except Exception:
        return "None"

def prepare_environment():

    try:
        import packaging
    except ModuleNotFoundError:
        run_pip(f"install packaging", "install packaging")

    from utils.package import check_req_file, check_reqs

    if getattr(sys, 'frozen', False):
        print('Running as app, skip dependency installation')
        return

    if args.frozen:
        return

    req_updated = False
    if sys.platform == 'win32':
        for req in REQ_WIN:
            if not check_reqs([req]):
                run_pip(f"install {req}", req)
                req_updated = True

    if is_amd_gpu():
        print('AMD GPU: Yes')
        if args.nightly:
            amd_nightly_gpu = supported_amd_nightly_gpu()
            if amd_nightly_gpu == "None":
                Exception("No AMD Nightly GPU supported")
            if amd_nightly_gpu == "RDNA3":
                torch_command = os.environ.get('TORCH_COMMAND',
                                               "pip install rocm==7.0.0rc20250818 rocm-sdk-core==7.0.0rc20250818 rocm-sdk-libraries-gfx110X-dgpu==7.0.0rc20250818 torch==2.9.0a0+rocm7.0.0rc20250818 torchvision==0.24.0a0+rocm7.0.0rc20250818 --index-url https://d2awnip2yjpvqn.cloudfront.net/v2/gfx110X-dgpu/ intel-openmp==2025.1.1 --extra-index-url https://pypi.org/simple --disable-pip-version-check")
            if amd_nightly_gpu == "RDNA4":
                torch_command = os.environ.get('TORCH_COMMAND',
                                               "pip install rocm==7.0.0rc20250817 rocm-sdk-core==7.0.0rc20250817 rocm-sdk-libraries-gfx120X-all==7.0.0rc20250817 torch==2.9.0a0+rocm7.0.0rc20250817 torchvision==0.24.0a0+rocm7.0.0rc20250817 --index-url https://d2awnip2yjpvqn.cloudfront.net/v2/gfx120X-all/ intel-openmp==2025.1.1 --extra-index-url https://pypi.org/simple --disable-pip-version-check")
        else:
            # AMD GPU: Cuda 11.8, Pytorch 2.2.2
            torch_command = os.environ.get('TORCH_COMMAND', "pip install torch==2.2.2 torchvision==0.17.2 torchaudio==2.2.2 --index-url https://download.pytorch.org/whl/cu118 --disable-pip-version-check")
    else:
        torch_command = os.environ.get('TORCH_COMMAND', "pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128 --disable-pip-version-check")
    if args.reinstall_torch or not is_installed("torch") or not is_installed("torchvision"):
        run(f'"{python}" -m {torch_command}', "Installing torch and torchvision", "Couldn't install torch", live=True)
        req_updated = True

    # Skip pip install -r when requirements file content + python version unchanged since last
    # successful install. Avoids the slow check_req_file pass on every launch.
    reqs_hash_current = None
    reqs_hash_cached = None
    try:
        with open(args.requirements, 'rb') as f:
            req_bytes = f.read()
        py_tag = '.'.join(str(v) for v in sys.version_info[:3]).encode('utf-8')
        h = hashlib.sha256()
        h.update(req_bytes)
        h.update(b'\n--py--\n')
        h.update(py_tag)
        reqs_hash_current = h.hexdigest()
    except Exception:
        reqs_hash_current = None

    if reqs_hash_current is not None and not args.reinstall_torch:
        try:
            if osp.exists(PATH_REQS_HASH):
                with open(PATH_REQS_HASH, 'r', encoding='utf-8') as f:
                    reqs_hash_cached = f.read().strip()
        except Exception:
            reqs_hash_cached = None

    if reqs_hash_current is not None and reqs_hash_cached == reqs_hash_current:
        pass
    else:
        if not check_req_file(args.requirements):
            run_pip(f"install -r {args.requirements}", "requirements")
            req_updated = True

    if req_updated:
        import site
        importlib.reload(site)
        if reqs_hash_current is not None:
            try:
                _ensure_tmp_dir()
                with open(PATH_REQS_HASH, 'w', encoding='utf-8') as f:
                    f.write(reqs_hash_current)
            except Exception:
                pass





if __name__ == '__main__':
    main()
