"""多会话管理：sessions/ 下的会话文件与索引。

文件结构：
    sessions/_index.json      会话清单（轻量，供界面列表用）
    sessions/<id>.json        每个会话的完整对话记录

索引只存「标题 / 时间 / 绑定文件」这类列表需要的字段，
真正的对话内容在各自的会话文件里，避免列表接口读一大堆数据。

会话标题的自动规则：第一次提问时用问题前 20 字做标题，
之后除非用户手动重命名，否则不再改动。
"""

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field

INDEX_FILE = "_index.json"

# 自动标题的最大长度
TITLE_MAX_LEN = 20


@dataclass
class SessionEntry:
    """索引里的一条会话记录。"""

    id: str
    title: str = "新对话"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    turns: int = 0                      # 已完成的轮数
    excel_file: str = ""                # 这个会话分析的文件（可为空）
    renamed: bool = False               # 用户是否手动改过标题

    @classmethod
    def from_dict(cls, raw: dict) -> "SessionEntry":
        """从 JSON 字典还原，忽略多余字段。"""
        return cls(
            id=raw.get("id", ""),
            title=raw.get("title", "新对话"),
            created_at=raw.get("created_at", 0.0),
            updated_at=raw.get("updated_at", 0.0),
            turns=raw.get("turns", 0),
            excel_file=raw.get("excel_file", ""),
            renamed=raw.get("renamed", False),
        )


class SessionStore:
    """会话清单的管理者，负责索引与增删改查。"""

    def __init__(self, directory: str = "sessions") -> None:
        self.directory = directory
        self._index: dict[str, SessionEntry] = {}
        self.load()

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def index_path(self) -> str:
        """索引文件路径。"""
        return os.path.join(self.directory, INDEX_FILE)

    def session_path(self, session_id: str) -> str:
        """某个会话的对话文件路径。"""
        return os.path.join(self.directory, f"{session_id}.json")

    # ------------------------------------------------------------------
    # 索引读写
    # ------------------------------------------------------------------

    def load(self) -> None:
        """读回索引；文件缺失或损坏时视为空。"""
        path = self.index_path()
        if not os.path.exists(path):
            # 兼容旧版本：没有索引时，把 sessions/*.json 里已有的会话补进来
            self._migrate_legacy_sessions()
            return

        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return

        self._index = {
            entry["id"]: SessionEntry.from_dict(entry)
            for entry in raw.get("sessions", [])
            if entry.get("id")
        }

    def save(self) -> None:
        """落盘索引。"""
        os.makedirs(self.directory, exist_ok=True)
        entries = sorted(self._index.values(), key=lambda e: e.updated_at, reverse=True)
        with open(self.index_path(), "w", encoding="utf-8") as f:
            json.dump(
                {"sessions": [asdict(entry) for entry in entries]},
                f,
                ensure_ascii=False,
                indent=2,
            )

    def _migrate_legacy_sessions(self) -> None:
        """把 sessions/*.json 里已有但不在索引中的会话补进索引。

        用于兼容单会话时期留下的 sessions/default.json。
        """
        if not os.path.isdir(self.directory):
            return

        entries: dict[str, SessionEntry] = {}
        for name in os.listdir(self.directory):
            if not name.endswith(".json") or name == INDEX_FILE:
                continue
            session_id = name[: -len(".json")]
            turns = self._count_turns(self.session_path(session_id))
            stat = os.stat(self.session_path(session_id))
            entries[session_id] = SessionEntry(
                id=session_id,
                title="历史会话" if not turns else f"历史会话（{turns} 轮）",
                created_at=stat.st_ctime,
                updated_at=stat.st_mtime,
                turns=turns,
                renamed=True,  # 迁移来的会话不改名
            )

        if entries:
            self._index = entries
            self.save()

    def _count_turns(self, path: str) -> int:
        """数一个会话文件里有多少轮对话。"""
        try:
            with open(path, encoding="utf-8") as f:
                return len(json.load(f).get("turns", []))
        except (OSError, json.JSONDecodeError):
            return 0

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[SessionEntry]:
        """按最近使用时间倒序列出所有会话。"""
        return sorted(self._index.values(), key=lambda e: e.updated_at, reverse=True)

    def get(self, session_id: str) -> SessionEntry | None:
        """取某个会话的索引条目。"""
        return self._index.get(session_id)

    def exists(self, session_id: str) -> bool:
        """会话是否存在（索引里有，或磁盘上有文件）。"""
        return session_id in self._index or os.path.exists(self.session_path(session_id))

    # ------------------------------------------------------------------
    # 增删改
    # ------------------------------------------------------------------

    def create(self, title: str = "", session_id: str = "", excel_file: str = "") -> SessionEntry:
        """新建会话并写入索引。"""
        session_id = session_id or uuid.uuid4().hex[:12]
        entry = SessionEntry(
            id=session_id,
            title=title or "新对话",
            excel_file=excel_file,
            renamed=bool(title),
        )
        self._index[session_id] = entry
        self.save()
        return entry

    def ensure(self, session_id: str) -> SessionEntry:
        """确保会话存在于索引中（不存在则补一条），返回索引条目。"""
        entry = self._index.get(session_id)
        if entry is None:
            entry = SessionEntry(id=session_id, renamed=True)
            self._index[session_id] = entry
        return entry

    def rename(self, session_id: str, title: str) -> SessionEntry | None:
        """重命名会话。"""
        entry = self._index.get(session_id)
        if entry is None:
            return None
        entry.title = title
        entry.renamed = True
        entry.updated_at = time.time()
        self.save()
        return entry

    def touch(
        self, session_id: str, turns: int = 0, first_query: str = "", excel_file: str = ""
    ) -> SessionEntry:
        """一轮对话结束后更新索引：轮数、最近时间，以及首次提问时的自动标题。"""
        entry = self.ensure(session_id)
        if turns:
            entry.turns = turns
        if excel_file:
            entry.excel_file = excel_file

        # 首次提问且用户没改过名，用问题前若干字做标题
        if first_query and not entry.renamed and entry.title in ("", "新对话"):
            entry.title = self._make_title(first_query)
            entry.renamed = True

        entry.updated_at = time.time()
        self.save()
        return entry

    @staticmethod
    def _make_title(query: str) -> str:
        """用问题生成标题：去换行、截断。"""
        text = " ".join(query.split())
        if not text:
            return "新对话"
        return text[:TITLE_MAX_LEN] + ("…" if len(text) > TITLE_MAX_LEN else "")

    def delete(self, session_id: str) -> bool:
        """删除会话（索引条目 + 对话文件）。"""
        existed = session_id in self._index
        self._index.pop(session_id, None)
        path = self.session_path(session_id)
        if os.path.exists(path):
            try:
                os.remove(path)
                existed = True
            except OSError:
                pass
        if existed:
            self.save()
        return existed
