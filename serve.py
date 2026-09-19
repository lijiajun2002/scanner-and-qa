from __future__ import annotations

import os
import sys

from webapp import capture_api


def main() -> int:
    host = os.environ.get("CAPTURE_API_HOST", "0.0.0.0")
    port = int(os.environ.get("CAPTURE_API_PORT", "8503"))
    token = os.environ.get("B_API_TOKEN", "")

    # 先常驻启动采集 API，保证容器重启后 B 无需等网页打开即可连接
    capture_api.ensure_started(host=host, port=port)
    capture_api.set_token(token)
    if not token:
        sys.stderr.write("[serve] 警告: 未设置 B_API_TOKEN，8503 接口无鉴权\n")

    from streamlit.web import cli as stcli

    sys.argv = [
        "streamlit", "run", "streamlit_app.py",
        "--server.address=0.0.0.0",
        "--server.port=8501",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]
    return stcli.main()


if __name__ == "__main__":
    raise SystemExit(main())
