"""sheet2music.core: 与 web 层分离的可复用转换核心。

约束：core 不依赖数据集 bundle、训练配置或 PiCoGen 逻辑，以便日后独立发布。
"""

from .models import ConvertParams, JobStatus, ValidationError
from .workspace import JobWorkspace

__all__ = ["ConvertParams", "JobStatus", "JobWorkspace", "ValidationError"]
