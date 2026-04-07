# video_download
video download by yt-dlp and playwright 

# 简易视频下载器
pyside6开发，使用yt-dlp结合playwright开发，支持youtube、抖音、B站等大部分视频网站，可以手动设置cookie采集需要登录的站点。


## 安装
pip install -r requirements.txt

playwright install chromium

必须安装
ffmpeg（用于音视频合并和视频播放）

有些站点设置了 Cookie验证可以手动设置 Cookie

## 运行
python main.py

## 依赖许可证
PySide6: LGPL

Playwright: MIT

yt-dlp: Unlicense

requests: Apache2
