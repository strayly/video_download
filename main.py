import asyncio
import time
from datetime import datetime
import shutil
import sys
import os
from playwright.async_api import async_playwright
import subprocess

from playwright.sync_api import sync_playwright

import yt_dlp
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLineEdit, QPushButton, QFileDialog, QListWidget, QListWidgetItem,
    QProgressBar, QLabel, QMenu, QDialog, QGridLayout, QTextEdit,
    QListWidget, QMessageBox, QDialogButtonBox
)
from PySide6.QtGui import QAction
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import Qt, QThread, Signal, QUrl, QSize, QtMsgType,qInstallMessageHandler
import requests
from urllib.parse import urlparse
import logging


# ==================== 解决 Qt + Playwright 冲突（必须加在这里）====================
#os.environ["QT_QPA_PLATFORM"] = "windows"
#os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "0"

# ====================== 日志系统（自动输出到控制台 + 文件）======================
def setup_logger():
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_file = os.path.join(log_dir, f"run_{datetime.now().strftime('%Y%m%d')}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

setup_logger()
log = logging.getLogger(__name__)

# 创建cookie文件夹（如果不存在）
COOKIE_DIR = os.path.join(os.getcwd(), "cookie")
if not os.path.exists(COOKIE_DIR):
    os.makedirs(COOKIE_DIR)

PWDownDomain = ["douyin.com","kuaishou.com"]


def read_cookie_from_folder(url, cookie_folder="cookie"):
    """
    根据URL读取对应域名的cookie文本
    :param url: 目标URL，例如"https://www.douyin.com/video/123456"
    :param cookie_folder: cookie文件夹路径，默认为当前目录下的"cookie"文件夹
    :return: 匹配到的cookie文本（字符串），无匹配则返回空字符串
    """
    # 处理空URL
    if not url.strip():
        return ""

    # 提取域名（处理无协议的URL，补充默认http协议）
    try:
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        parsed_url = urlparse(url)
        domain = parsed_url.netloc  # 提取域名（如www.douyin.com）
        # 去除www前缀（可选，根据实际cookie文件命名调整）
        if domain.startswith("www."):
            domain = domain[4:]
    except Exception:
        return ""

    # 检查cookie文件夹是否存在
    if not os.path.exists(cookie_folder):
        return ""

    # 遍历cookie文件夹，匹配域名对应的文本文件
    cookie_content = ""
    for filename in os.listdir(cookie_folder):
        # 匹配规则：文件名包含域名（如douyin.com.txt 或 douyin.com）
        file_domain = os.path.splitext(filename)[0]  # 去除文件扩展名
        if domain in file_domain or file_domain in domain:
            file_path = os.path.join(cookie_folder, filename)
            try:
                # 读取cookie文件内容
                with open(file_path, "r", encoding="utf-8") as f:
                    cookie_content = f.read().strip()
                break  # 找到第一个匹配的文件即退出
            except (UnicodeDecodeError, IOError):
                # 处理编码错误或文件读取错误
                continue

    return cookie_content

# ====================== 下载线程 ======================
class DownloadThread(QThread):
    progress_update = Signal(str, int)  # 文件名, 进度
    finished_signal = Signal(str, str)  # 文件名, 路径
    error_signal = Signal(str)

    def __init__(self, url, save_path, filename):
        super().__init__()
        self.url = url
        self.save_path = save_path
        self.filename = filename

    def run(self):
        try:
            # 输出文件模板
            outtmpl = os.path.join(self.save_path, f"{self.filename}.%(ext)s")
            cookie_str = read_cookie_from_folder(self.url)
            # yt-dlp 核心配置
            ydl_opts = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4',
                'outtmpl': outtmpl,
                'quiet': False,
                'noplaylist': True,
                'progress_hooks': [self.progress_hook],
                'http_headers': {
                    'Cookie': cookie_str,
                    'User-Agent': 'Mozilla/5.0'
                }
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
                final_path = ydl.prepare_filename(info)

            self.finished_signal.emit(self.filename, final_path)

        except Exception as e:
            self.error_signal.emit(f"下载失败：{str(e)}")

    # 进度回调
    def progress_hook(self, d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes', 1)
            downloaded = d.get('downloaded_bytes', 0)
            if total > 0:
                pct = int(downloaded / total * 100)
                self.progress_update.emit(self.filename, pct)


# ====================== 专用下载线程（Playwright抓取音视频+合并） ======================
class PWDownloadThread(QThread):
    progress_update = Signal(str, int)
    finished_signal = Signal(str, str)
    error_signal = Signal(str)

    def __init__(self, url, save_path, filename):
        super().__init__()
        self.url = url
        self.save_path = save_path
        self.filename = filename

        self.video_url = None
        self.audio_url = None

    def run(self):
        try:
            asyncio.run(self.capture_media_url())
        except Exception as e:
            self.error_signal.emit(str(e))

    def download_file(self, url, filename):
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": url,
        }

        with requests.get(url, headers=headers, stream=True) as r:
            r.raise_for_status()
            with open(filename, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)

    async def capture_media_url(self):
        async with async_playwright() as p:
            # browser = await p.chromium.launch(headless=False)
            browser = await p.chromium.launch(
                headless=False,  #
                slow_mo=200,  # 模拟人操作延迟
                args=[
                    "--disable-blink-features=AutomationControlled",  # 关闭webdriver标记
                    "--start-maximized",  # 全屏模拟真人
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-popup-blocking",
                    "--disable-default-apps",
                    "--mute-audio"
                ]
            )
            # 创建上下文（伪造浏览器指纹 + 真实UA）
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                ignore_https_errors=True
            )

            # ====================== 【粘贴这里：加载整段Cookie到Playwright】 ======================
            cookie_str = read_cookie_from_folder(self.url)
            if cookie_str:
                cookies = []
                for part in cookie_str.split(";"):
                    part = part.strip()
                    if not part or "=" not in part:
                        continue
                    k, v = part.split("=", 1)
                    cookies.append({
                        "name": k.strip(),
                        "value": v.strip(),
                        "domain": "." + urlparse(self.url).netloc.replace("www.", ""),
                        "path": "/",
                        "httpOnly": False,
                        "secure": True
                    })
                await context.add_cookies(cookies)

            # 关键：移除 playwright 自动化痕迹
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.navigator.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
            """)
            page = await context.new_page()
            self.video_url = None
            self.audio_url = None

            # 捕获网络请求
            async def capture_response(response):
                url = response.url
                if "media-video-avc1" in url and "video/mp4" in response.headers.get("content-type", ""):
                    self.video_url = url
                elif "media-audio-und-mp4a" in url and "video/mp4" in response.headers.get("content-type", ""):
                    self.audio_url = url
                elif "video/mp4" in response.headers.get("content-type", ""):
                    self.video_url = url

            page.on("response", capture_response)

            await page.goto(self.url)
            await page.reload()
            await asyncio.sleep(5)
            await browser.close()

            if not self.video_url:
                self.error_signal.emit("未抓取到视频流")
                return

            # 下载
            video_path = os.path.join(self.save_path, f"{self.filename}_video.tmp")
            audio_path = os.path.join(self.save_path, f"{self.filename}_audio.tmp")
            final_path = os.path.join(self.save_path, f"{self.filename}.mp4")

            self.progress_update.emit(self.filename, 20)

            self.download_file(self.video_url, video_path)
            self.progress_update.emit(self.filename, 50)
            if not self.audio_url:
                shutil.copyfile(video_path, final_path)
                self.progress_update.emit(self.filename, 70)

            else:
                self.download_file(self.audio_url, audio_path)
                self.progress_update.emit(self.filename, 70)

                # 合并
                subprocess.run([
                    'ffmpeg', '-i', video_path, '-i', audio_path,
                    '-c:v', 'copy', '-c:a', 'aac', final_path, '-y'
                ], creationflags=0x08000000)
                os.remove(audio_path)
            # 删除临时文件
            os.remove(video_path)
            self.progress_update.emit(self.filename, 100)
            self.finished_signal.emit(self.filename, final_path)

class CookieFetcher(QThread):
    cookie_updated = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                page = browser.new_page()
                page.goto(self.url)

                # 存储上次发送的cookie字符串，避免重复发送相同内容
                last_cookie_str = ""

                # 监听网络请求，实时获取cookie变化
                def on_response(response):
                    nonlocal last_cookie_str
                    try:
                        cookies = page.context.cookies()
                        cookie_items = [f"{c['name']}={c['value']}" for c in cookies]
                        cookie_str = ";".join(sorted(cookie_items))  # 排序确保一致性

                        # 只有当cookie发生变化时才发送信号
                        if cookie_str != last_cookie_str:
                            last_cookie_str = cookie_str
                            self.cookie_updated.emit(cookie_str or "[空]")
                    except:
                        pass  # 忽略可能的异常

                page.on("response", on_response)

                # 持续监听直到用户手动关闭浏览器
                while True:
                    try:
                        # 每隔一段时间主动检查一次cookies
                        time.sleep(2)
                        cookies = page.context.cookies()
                        cookie_items = [f"{c['name']}={c['value']}" for c in cookies]
                        cookie_str = "\n".join(sorted(cookie_items))

                        if cookie_str != last_cookie_str:
                            last_cookie_str = cookie_str
                            self.cookie_updated.emit(cookie_str or "[空]")

                        # 检查页面是否仍然存在
                        page.title()
                    except:
                        break  # 页面关闭或出错时退出循环

                browser.close()
                #self.cookie_updated.emit("[浏览器已关闭]")  # 发送浏览器关闭通知
        except Exception as e:
            self.error_occurred.emit(str(e))

# ====================== Cookie设置弹窗 ======================
class CookieSettingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cookie设置")
        self.setModal(True)
        self.setMinimumSize(600, 400)

        # 主布局
        main_layout = QVBoxLayout(self)

        # Cookie列表
        list_layout = QVBoxLayout()
        list_layout.addWidget(QLabel("已保存的Cookie列表："))
        self.cookie_list = QListWidget()
        self.cookie_list.itemClicked.connect(self.on_cookie_item_click)
        list_layout.addWidget(self.cookie_list)

        # 操作按钮（删除）
        del_btn = QPushButton("删除选中Cookie")
        del_btn.clicked.connect(self.delete_selected_cookie)
        list_layout.addWidget(del_btn)
        main_layout.addLayout(list_layout)

        # 分割线
        line = QWidget()
        line.setFixedHeight(2)
        line.setStyleSheet("background-color: #cccccc;")
        main_layout.addWidget(line)

        # 新增/编辑Cookie区域
        edit_layout = QGridLayout()

        edit_layout.addWidget(QLabel("域名："), 0, 0)
        self.domain_input = QLineEdit()
        self.domain_input.setPlaceholderText("例如：douyin.com")
        edit_layout.addWidget(self.domain_input, 0, 1)

        edit_layout.addWidget(QLabel("Cookie内容："), 1, 0)
        self.cookie_input = QTextEdit()
        self.cookie_input.setPlaceholderText("请输入完整的Cookie字符串")
        edit_layout.addWidget(self.cookie_input, 1, 1)

        main_layout.addLayout(edit_layout)

        # 保存按钮
        btn_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.save_cookie)
        btn_box.rejected.connect(self.reject)
        main_layout.addWidget(btn_box)

        # 加载已保存的Cookie
        self.load_cookie_list()

    def load_cookie_list(self):
        """加载cookie文件夹下的所有Cookie文件到列表"""
        self.cookie_list.clear()
        if not os.path.exists(COOKIE_DIR):
            return

        # 遍历cookie文件夹下的所有txt文件
        for filename in os.listdir(COOKIE_DIR):
            if filename.endswith(".txt"):
                domain = filename[:-4]  # 去掉.txt后缀
                self.cookie_list.addItem(domain)

    def on_cookie_item_click(self, item):
        """点击列表项时加载对应的Cookie内容"""
        domain = item.text()
        cookie_file = os.path.join(COOKIE_DIR, f"{domain}.txt")

        if os.path.exists(cookie_file):
            with open(cookie_file, "r", encoding="utf-8") as f:
                cookie_content = f.read()
            self.domain_input.setText(domain)
            self.cookie_input.setText(cookie_content)

    def delete_selected_cookie(self):
        """删除选中的Cookie文件"""
        selected_items = self.cookie_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "警告", "请先选中要删除的Cookie项")
            return

        for item in selected_items:
            domain = item.text()
            cookie_file = os.path.join(COOKIE_DIR, f"{domain}.txt")

            if os.path.exists(cookie_file):
                try:
                    os.remove(cookie_file)
                    self.cookie_list.takeItem(self.cookie_list.row(item))
                    # 清空输入框
                    self.domain_input.clear()
                    self.cookie_input.clear()
                    QMessageBox.information(self, "成功", f"已删除 {domain} 的Cookie")
                except Exception as e:
                    QMessageBox.critical(self, "错误", f"删除失败：{str(e)}")

    def save_cookie(self):
        """保存Cookie到文件"""
        domain = self.domain_input.text().strip()
        cookie_content = self.cookie_input.toPlainText().strip()

        if not domain:
            QMessageBox.warning(self, "警告", "请输入域名")
            return

        if not cookie_content:
            QMessageBox.warning(self, "警告", "请输入Cookie内容")
            return

        # 保存到文件
        cookie_file = os.path.join(COOKIE_DIR, f"{domain}.txt")
        try:
            with open(cookie_file, "w", encoding="utf-8") as f:
                f.write(cookie_content)
            QMessageBox.information(self, "成功", f"已保存 {domain} 的Cookie")
            self.load_cookie_list()  # 刷新列表
            self.domain_input.clear()  # 清空输入框
            self.cookie_input.clear()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存失败：{str(e)}")


# ====================== 主窗口 ======================
class VideoDownloader(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("视频下载器")
        self.setGeometry(100, 100, 1200, 700)
        self.save_path = os.getcwd()
        self.download_tasks = {}
        self.cookie_fetcher = None

        # 主布局
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        left_layout = QVBoxLayout()
        right_layout = QVBoxLayout()

        # 顶部工具栏（新增Cookie按钮）
        top_tool_layout = QHBoxLayout()

        # 地址栏
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("输入视频链接...")
        self.start_btn = QPushButton("开始下载")
        self.start_btn.clicked.connect(self.start_download)

        top_tool_layout.addWidget(self.url_input)
        top_tool_layout.addWidget(self.start_btn)
        left_layout.addLayout(top_tool_layout)

        # 顶部工具栏（新增Cookie按钮）
        top_cookie_layout = QHBoxLayout()

        # 获取cookie
        self.cookie_input = QTextEdit()
        self.cookie_input.setFixedSize(500, 30)
        self.get_cookie_btn = QPushButton("获取Cookie")
        self.get_cookie_btn.setFixedWidth(100)
        self.get_cookie_btn.clicked.connect(self.on_fetch_cookie)

        # 获取cookie
        self.save_cookie_button = QPushButton("保存")
        self.save_cookie_button.setFixedWidth(100)
        self.save_cookie_button.clicked.connect(self.on_save_cookie)

        top_cookie_layout.addWidget(self.cookie_input)
        top_cookie_layout.addWidget(self.get_cookie_btn)
        top_cookie_layout.addWidget(self.save_cookie_button)
        top_cookie_layout.addStretch()
        left_layout.addLayout(top_cookie_layout)


        # 网页预览
        self.web_view = QWebEngineView()
        left_layout.addWidget(self.web_view)

        # 保存路径
        path_layout = QHBoxLayout()
        self.path_label = QLabel(f"保存路径：{self.save_path}")
        self.path_btn = QPushButton("选择路径")
        self.path_btn.clicked.connect(self.select_save_path)
        path_layout.addWidget(self.path_label)
        path_layout.addWidget(self.path_btn)
        path_layout.addStretch()
        left_layout.addLayout(path_layout)

        # 设置cookie
        cookie_layout = QHBoxLayout()
        self.cookie_btn = QPushButton("Cookie设置")
        self.cookie_btn.setFixedWidth(100)
        self.cookie_btn.clicked.connect(self.open_cookie_setting)
        cookie_layout.addWidget(self.cookie_btn)
        cookie_layout.addStretch()
        left_layout.addLayout(cookie_layout)


        # 下载列表
        right_layout.addWidget(QLabel("下载列表"))
        self.download_list = QListWidget()
        self.download_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.download_list.customContextMenuRequested.connect(self.show_right_menu)
        self.download_list.itemDoubleClicked.connect(self.play_video_by_item)
        right_layout.addWidget(self.download_list)

        main_layout.addLayout(left_layout, stretch=3)
        main_layout.addLayout(right_layout, stretch=1)

    def on_fetch_cookie(self):
        url = self.url_input.text().strip()
        if not url:
            self.cookie_input.setText("[错误] 请先填写有效的URL地址！")
            return

        self.cookie_input.setText("正在启动浏览器并获取Cookie，请稍候...")
        self.cookie_fetcher = CookieFetcher(url)
        self.cookie_fetcher.cookie_updated.connect(self.update_cookie_display)
        self.cookie_fetcher.error_occurred.connect(self.handle_error)
        self.cookie_fetcher.start()

    def update_cookie_display(self, cookie_str):
        # 清除旧信息，显示最新cookie
        self.cookie_input.setText(cookie_str)

    def handle_error(self, error_msg):
        self.cookie_input.setText(f"[异常发生]: {error_msg}")


    def on_save_cookie(self):
        domain = self.extract_domain_from_url(self.url_input.text())
        content = self.cookie_input.toPlainText()
        if not domain or not content.strip() or "[错误]" in content or "[异常发生]" in content or "[浏览器已关闭]" in content:
            QMessageBox.warning(self, "保存失败", "无法提取域名或无有效内容可保存")
            return
        try:
            #file_path = f"{domain}.txt"
            file_path = os.path.join(COOKIE_DIR, f"{domain}.txt")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            QMessageBox.information(self, "保存成功", f"已将Cookie写入文件: {file_path}")
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"保存文件时发生错误: {str(e)}")

    @staticmethod
    def extract_domain_from_url(url):
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            netloc = parsed.netloc
            if ':' in netloc:
                netloc = netloc.split(':')[0]
            return netloc.replace("www.", "") if netloc else ""
        except:
            return ""

    def open_cookie_setting(self):
        """打开Cookie设置弹窗"""
        dialog = CookieSettingDialog(self)
        dialog.exec()

    def select_save_path(self):
        path = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if path:
            self.save_path = path
            self.path_label.setText(f"保存路径：{path}")

    def check_is_pw(self, url):
        for k in PWDownDomain:
            if k in url:
                return True
        return False

    def start_download(self):
        url = self.url_input.text().strip()
        if not url:
            return

        self.web_view.load(QUrl(url))
        formatted_now = datetime.now().strftime('%Y-%m-%d-%H%M%S')
        filename = formatted_now + f"-{len(self.download_tasks) + 1}"

        # 创建列表项
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, {"path": "", "filename": filename})
        item.setSizeHint(QSize(0, 50))
        self.download_list.addItem(item)

        widget = QWidget()
        layout = QVBoxLayout(widget)
        name_label = QLabel(filename + ".mp4")
        bar = QProgressBar()
        layout.addWidget(name_label)
        layout.addWidget(bar)
        self.download_list.setItemWidget(item, widget)

        # 启动下载
        if self.check_is_pw(url):
            t = PWDownloadThread(url, self.save_path, filename)
        else:
            t = DownloadThread(url, self.save_path, filename)
        t.progress_update.connect(lambda n, v: self.update_progress(item, v))
        t.finished_signal.connect(lambda n, p: self.done(item, n, p))
        t.error_signal.connect(lambda msg: print(msg))
        t.start()
        self.download_tasks[filename] = t

    def update_progress(self, item, v):
        w = self.download_list.itemWidget(item)
        w.findChild(QProgressBar).setValue(v)

    def done(self, item, name, path):
        w = self.download_list.itemWidget(item)
        w.findChild(QProgressBar).setValue(100)
        w.findChild(QLabel).setText(f"{name} ✅ 完成")
        item.setData(Qt.ItemDataRole.UserRole, {"path": path, "filename": name})

    def show_right_menu(self, pos):
        item = self.download_list.itemAt(pos)
        if not item:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data.get("path"):
            return

        m = QMenu()
        play = QAction("播放", self)
        play.triggered.connect(lambda: self.play(data["path"]))
        m.addAction(play)
        m.exec(self.download_list.mapToGlobal(pos))

    def play(self, path):
        if sys.platform == "win32":
            os.startfile(path)
        else:
            import webbrowser
            webbrowser.open(path)

    def play_video_by_item(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data.get("path"):
            self.play(data["path"])


# ====================== 启动 ======================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = VideoDownloader()
    win.show()
    sys.exit(app.exec())