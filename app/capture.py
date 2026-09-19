from __future__ import annotations

import subprocess
from pathlib import Path

WARMUP_FRAMES = 10


def _capture_opencv(camera_index: int, dest: Path) -> None:
    import cv2  # 延迟导入：没装也能走 ffmpeg 回退

    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"无法打开摄像头 /dev/video{camera_index}")

    try:
        # 丢弃预热帧，等自动曝光/白平衡稳定，避免拍出黑图
        for _ in range(WARMUP_FRAMES):
            cap.read()
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError("摄像头读取帧失败")
        if not cv2.imwrite(str(dest), frame, [cv2.IMWRITE_JPEG_QUALITY, 90]):
            raise RuntimeError(f"写入照片失败: {dest}")
    finally:
        cap.release()


def _capture_ffmpeg(camera_index: int, dest: Path) -> None:
    device = f"/dev/video{camera_index}"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "v4l2", "-i", device,
        "-frames:v", "1", "-y", str(dest),
    ]
    subprocess.run(cmd, check=True, timeout=20)


def capture_photo(camera_index: int, dest: Path) -> Path:
    """抓一帧存成 JPEG，优先 OpenCV，失败回退 ffmpeg。返回照片路径。"""
    dest.parent.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    for name, fn in (("opencv", _capture_opencv), ("ffmpeg", _capture_ffmpeg)):
        try:
            fn(camera_index, dest)
            if dest.exists() and dest.stat().st_size > 0:
                return dest
            raise RuntimeError("生成的文件为空")
        except Exception as exc:  # noqa: BLE001 - 逐个回退并把原因汇总
            errors.append(f"{name}: {exc}")

    raise RuntimeError("拍照失败 -> " + "; ".join(errors))
