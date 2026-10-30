"""图表仓库：把 Agent 生成的统计图按「图片内容的哈希」归档到 charts/。

与 cleaned/ 的清洗表仓库同一套思路：
- 键是图片内容的哈希，所以同一张图重复保存会自动复用，不会堆出多份；
- 目录本身就是状态，不需要额外维护登记表；
- 图的元信息（标题、分类）只随事件发给界面，不落盘——
  界面刷新后从会话记录里的 Agent 卡片重新拿到。

按内容哈希归档的副作用：两张内容完全相同但标题不同的图会被视作同一张，
后保存的那次不会覆盖已存在的文件（标题以先写入的为准）。
图表基本都是数据驱动的，这种情况很少见，换来的是天然去重。
"""

import hashlib
import os
import shutil
import time
from dataclasses import asdict, dataclass

# 图表默认存放目录（相对工作目录，与 cleaned/ 同级）
CHARTS_DIR = "charts"

# 允许归档的图片后缀
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".svg")

# 超过这个大小就不归档了（防止模型误把大文件当图片保存）
MAX_CHART_BYTES = 12 * 1024 * 1024


@dataclass
class ChartEntry:
    """仓库里的一张图（只含文件本身的信息，标题由事件携带）。"""

    filename: str
    size: int
    updated_at: float

    def to_dict(self) -> dict:
        """转成前端友好的字典（补一个可读时间）。"""
        data = asdict(self)
        data["updated_at_text"] = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(self.updated_at)
        )
        return data


class ChartStore:
    """图表仓库：管理统计图的保存与查询，键为图片内容的哈希。"""

    def __init__(self, charts_dir: str = CHARTS_DIR):
        self.charts_dir = charts_dir

    # ------------------------------------------------------------------
    # 哈希与路径
    # ------------------------------------------------------------------

    @staticmethod
    def file_hash(path: str, length: int = 16) -> str:
        """按文件内容计算哈希。"""
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()[:length]

    def compute_chart_path(self, image_path: str) -> str:
        """这张图片归档后应当存放的路径：charts/<内容哈希><后缀>（不保证已存在）。"""
        suffix = os.path.splitext(image_path)[1].lower()
        return os.path.join(self.charts_dir, f"{self.file_hash(image_path)}{suffix}")

    def resolve(self, name: str) -> str | None:
        """把界面传来的文件名解析成真实路径；不合法或不存在时返回 None。

        只接受「charts/ 下的裸文件名」，挡掉路径穿越（../、绝对路径等）。
        """
        if not name or name != os.path.basename(name):
            return None
        path = os.path.join(self.charts_dir, name)
        if not os.path.isfile(path):
            return None
        return path

    def list_charts(self) -> list[dict]:
        """列出仓库里的所有图，按修改时间倒序。"""
        if not os.path.isdir(self.charts_dir):
            return []

        entries: list[ChartEntry] = []
        for name in os.listdir(self.charts_dir):
            if os.path.splitext(name)[1].lower() not in IMAGE_SUFFIXES:
                continue
            path = os.path.join(self.charts_dir, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            entries.append(
                ChartEntry(filename=name, size=stat.st_size, updated_at=stat.st_mtime)
            )

        entries.sort(key=lambda e: e.updated_at, reverse=True)
        return [entry.to_dict() for entry in entries]

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------

    def save(self, image_path: str) -> str:
        """把图片归档进仓库，返回归档后的路径。

        图片已经在仓库里（且路径一致）时不做任何移动。
        """
        if not os.path.isfile(image_path):
            raise FileNotFoundError(f"找不到图片文件：{image_path}")

        suffix = os.path.splitext(image_path)[1].lower()
        if suffix not in IMAGE_SUFFIXES:
            raise ValueError(
                f"不支持的图片格式 {suffix or '（无后缀）'}，"
                f"只接受 {'、'.join(IMAGE_SUFFIXES)}"
            )

        size = os.path.getsize(image_path)
        if size > MAX_CHART_BYTES:
            raise ValueError(
                f"图片过大（{size / 1024 / 1024:.1f} MB），"
                f"超过上限 {MAX_CHART_BYTES // 1024 // 1024} MB，请降低 dpi 或尺寸"
            )

        saved_path = self.compute_chart_path(image_path)
        os.makedirs(self.charts_dir, exist_ok=True)

        if os.path.abspath(image_path) != os.path.abspath(saved_path):
            if os.path.exists(saved_path):
                # 同一张图已经归档过：删掉临时文件，复用已有归档
                try:
                    os.remove(image_path)
                except OSError:
                    pass
            else:
                shutil.move(image_path, saved_path)

        return saved_path
