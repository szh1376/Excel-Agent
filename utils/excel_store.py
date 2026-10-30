"""清洗仓库：把清洗后的表格按「原始表格内容的哈希」归档，并提供查询。

用途：
- Loader 清洗完表格后，用 save_cleaned_file() 保存到 cleaned/<哈希>.csv；
- Master（服务器侧）用 find_cleaned_file() 判断某张 Excel 是否已清洗过，并拿到清洗表路径。

哈希取自**整张表的数据内容**（而不是文件字节）：
- 表里的数据一变，哈希就变 → 会被判定为"没清洗过"，重新清洗；
- 把同一份数据另存为别的格式、或重新保存一遍（只改了元数据），哈希不变 → 直接复用旧清洗表。

所以「是否已清洗过」这个状态就存在磁盘上（cleaned/ 目录即状态），
不需要再额外维护一份“加载过哪些表”的内存登记表。
"""

import hashlib
import os
import shutil

# 清洗结果默认存放目录（相对工作目录）
CLEANED_DIR = "cleaned"


class ExcelStore:
    """清洗仓库：管理「清洗后表格」的保存与查询，键为原始表格内容的哈希。"""

    def __init__(self, cleaned_dir: str = CLEANED_DIR):
        self.cleaned_dir = cleaned_dir

    # ------------------------------------------------------------------
    # 哈希与路径
    # ------------------------------------------------------------------

    def table_hash(self, excel_path: str, length: int = 16) -> str:
        """按整张表的数据内容计算哈希（只看表里的数据，与文件格式、元数据无关）。"""
        import pandas as pd

        sheets = pd.read_excel(excel_path, sheet_name=None, header=None, dtype=str)

        digest = hashlib.sha256()
        for sheet_name, frame in sheets.items():
            digest.update(f"#sheet:{sheet_name}\n".encode("utf-8"))
            digest.update(
                frame.fillna("").to_csv(index=False, header=False).encode("utf-8")
            )
        return digest.hexdigest()[:length]

    def compute_cleaned_file_path(self, excel_path: str) -> str:
        """这张 Excel 清洗后应当存放的路径：cleaned/<内容哈希>.csv（不保证文件已存在）。"""
        return os.path.join(self.cleaned_dir, f"{self.table_hash(excel_path)}.csv")

    # ------------------------------------------------------------------
    # 查询：这张表是否已经清洗过
    # ------------------------------------------------------------------

    def find_cleaned_file(self, excel_path: str) -> str | None:
        """查找这张 Excel 已经保存的清洗表路径；从未清洗过则返回 None。"""
        try:
            saved_path = self.compute_cleaned_file_path(excel_path)
        except OSError:
            return None
        return saved_path if os.path.exists(saved_path) else None

    # ------------------------------------------------------------------
    # 保存：把清洗结果放进仓库
    # ------------------------------------------------------------------

    def save_cleaned_file(self, excel_path: str, cleaned_file: str) -> str:
        """保存清洗后的表格"""
        saved_path = self.compute_cleaned_file_path(excel_path)
        os.makedirs(self.cleaned_dir, exist_ok=True)

        if os.path.abspath(cleaned_file) != os.path.abspath(saved_path):
            shutil.move(cleaned_file, saved_path)

        return saved_path
