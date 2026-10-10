from __future__ import annotations

import os

from .backends import Backend, CompositeBackend, SoftwareBackend, VideoBackend


def make_backend(shot_source: str | None = None, target_url: str | None = None,
                 token: str | None = None, video_source: str | None = None,
                 video_size: str | None = None) -> Backend:
    """根据环境变量/参数构造后端。

    SQA_TARGET_URL / SQA_TARGET_TOKEN：Target 上 agent_server 的地址与 token。
    SQA_SHOT_SOURCE：software（默认）或 video。
    SQA_VIDEO_SOURCE / SQA_VIDEO_SIZE：视频采集来源。
    """
    shot_source = shot_source or os.environ.get("SQA_SHOT_SOURCE", "software")
    target_url = target_url or os.environ.get("SQA_TARGET_URL", "http://127.0.0.1:8600")
    token = token if token is not None else os.environ.get("SQA_TARGET_TOKEN", "")
    video_source = video_source or os.environ.get("SQA_VIDEO_SOURCE", "v4l2:/dev/video0")
    video_size = video_size or os.environ.get("SQA_VIDEO_SIZE", "1920x1080")

    software = SoftwareBackend(target_url, token)
    if shot_source == "video":
        video = VideoBackend(video_source, size=video_size)
        return CompositeBackend(video, software)
    return software
