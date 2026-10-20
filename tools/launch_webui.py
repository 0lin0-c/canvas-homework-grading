"""Start the local review workbench and open it in the default browser."""

from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
URL = 'http://127.0.0.1:8767/review-workbench.html'


def workbench_running():
    request = urllib.request.Request(URL)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=1) as response:
            return '作业批改 · 复核工作台'.encode('utf-8') in response.read(512)
    except urllib.error.URLError:
        return False


def main():
    if not workbench_running():
        log_path = ROOT / 'prototypes' / 'local' / 'webui.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('ab') as log:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / 'tools' / 'serve.py'), '--port', '8767'],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        for _ in range(40):
            if workbench_running():
                break
            if process.poll() is not None:
                raise SystemExit(f'页面服务启动失败，请查看 {log_path}')
            time.sleep(0.25)
        else:
            raise SystemExit(f'页面服务未在 10 秒内启动，请查看 {log_path}')
    webbrowser.open(URL)
    print(URL)


if __name__ == '__main__':
    main()
