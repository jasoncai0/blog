#!/usr/bin/env python3
"""
agent-memory.py — 长期记忆的参考实现（只依赖标准库）

演示四件事：
1. 写入判定：重要性、置信度、否决项、候选晋升
2. 冲突处理：same / complement / contradict / unrelated
3. 汰换：有效期、衰减、预算、软删除
4. 检索资格门：过期记忆不进上下文

运行：python3 agent-memory.py demo
"""

from __future__ import annotations

import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

# ---------------------------------------------------------------- 策略常量

# 半衰期按谓词给，不搞全局一刀切。单位：天。
HALF_LIFE = {
    "allergy": 3650,   # 过敏这类硬事实，十年也不该自己消失
    "residence": 365,
    "likes": 180,      # 偏好会变，但变得慢
    "note": 365,
    "reminder": 1,     # 任务状态本来就不该进长期库
    "event": 30,
}

# 写入阈值。低于 DROP 直接丢，低于 ACTIVE 进候选。
DROP_BELOW = 4
ACTIVE_AT = 6

# 预算：active 条目超过这个数，按效用从低到高下沉。
MAX_ACTIVE = 6

# 检索最低相关度。低于它直接不返回，避免“重要性高但答非所问”。
MIN_RELEVANCE = 0.2

# 查询词到谓词的映射。演示用；生产换成 embedding + 交叉编码器。
QUERY_HINTS = {
    "住": "residence", "地址": "residence", "城市": "residence",
    "过敏": "allergy", "吃": "allergy",
    "咖啡": "likes", "喜欢": "likes", "喝": "likes",
    "工位": "note", "座位": "note",
}


def iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return value


def days_between(start: str, end: str) -> float:
    a = datetime.fromisoformat(start)
    b = datetime.fromisoformat(end)
    return max(0.0, (b - a).total_seconds() / 86400)


def normalize(text: str) -> str:
    text = re.sub(r"[\s，。！？、,.!?]", "", text)
    return text.rstrip("了")


def tokens(text: str) -> set[str]:
    """中文按字、英文按词。演示够用，生产换 embedding。"""
    return set(re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", text.lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------- 数据与模型


@dataclass
class Claim:
    kind: str
    subject: str
    predicate: str
    object: str
    text: str
    confidence: float = 0.8
    sensitivity: str = "normal"


class RuleLLM:
    """替身：接口和真实 LLM 一致，输出确定，方便跑测试。

    生产里把 extract / relate / importance 换成模型调用，
    但不要让它决定写不写、删不删——那是 Policy 的事。
    """

    def extract(self, text: str) -> list[Claim]:
        out: list[Claim] = []
        for sent in re.split(r"[。！？\n]", text):
            sent = sent.strip(" \t，,")
            if not sent:
                continue

            if m := re.search(r"提醒我(.+)", sent):
                out.append(Claim("task_state", "user", "reminder", m.group(1), sent, 0.9))
                continue

            if m := re.search(r"我(?:对)([^，。！？\s]+?)过敏", sent):
                out.append(Claim("fact", "user", "allergy", normalize(m.group(1)), sent, 0.8))
                continue

            if m := re.search(r"我(?:住在|现在住在|搬到了|搬到)([^，。！？\s]+)", sent):
                out.append(Claim("fact", "user", "residence", normalize(m.group(1)), sent, 0.8))
                continue

            if m := re.search(r"我(?:现在)?(?:喜欢|偏好|爱喝|爱)([^，。！？\s]+)", sent):
                out.append(Claim("preference", "user", "likes", normalize(m.group(1)), sent, 0.8))
                continue

            if m := re.search(r"记住[：:，,]?\s*(.+)", sent):
                out.append(Claim("fact", "user", "note", normalize(m.group(1)), sent, 0.95))
        return out

    def relate(self, new: Claim, old: sqlite3.Row) -> str:
        if new.predicate != old["predicate"]:
            return "unrelated"
        a, b = normalize(new.object), normalize(old["object"])
        if a == b:
            return "same"
        if new.predicate == "residence":
            return "contradict"
        if new.predicate == "likes" and ("现在" in new.text or "不" in new.text):
            return "contradict"
        return "complement"

    def importance(self, claim: Claim) -> float:
        return {
            "allergy": 9,
            "residence": 7,
            "likes": 5,
            "note": 6,
            "reminder": 2,
        }.get(claim.predicate, 5)


class MemoryStore:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.llm = RuleLLM()
        self._schema()

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id             TEXT PRIMARY KEY,
                kind           TEXT NOT NULL,
                subject        TEXT NOT NULL,
                predicate      TEXT NOT NULL,
                object         TEXT NOT NULL,
                text           TEXT NOT NULL,
                valid_from     TEXT NOT NULL,
                valid_to       TEXT,
                recorded_at    TEXT NOT NULL,
                confidence     REAL NOT NULL,
                importance     REAL NOT NULL,
                sensitivity    TEXT NOT NULL DEFAULT 'normal',
                status         TEXT NOT NULL,
                supersedes     TEXT,
                source         TEXT NOT NULL,
                last_used_at   TEXT,
                used_count     INTEGER NOT NULL DEFAULT 0,
                access_count   INTEGER NOT NULL DEFAULT 0,
                evidence_count INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_mem_lookup
                ON memories(subject, predicate, status);
            """
        )

    # ------------------------------------------------------------ 写入

    def write(self, text: str, now: datetime | str, source: str = "user") -> list[dict]:
        now = iso(now)
        decisions: list[dict] = []

        for claim in self.llm.extract(text):
            if claim.kind == "task_state":
                decisions.append({"action": "reject", "reason": "task_state", "text": claim.text})
                continue
            if claim.sensitivity == "secret":
                decisions.append({"action": "reject", "reason": "secret", "text": claim.text})
                continue

            importance = self.llm.importance(claim)
            if importance < DROP_BELOW:
                decisions.append({"action": "drop", "reason": "low_importance", "text": claim.text})
                continue

            target = None
            relation = "unrelated"
            for row in self._similar(claim):
                rel = self.llm.relate(claim, row)
                if rel != "unrelated":
                    target, relation = row, rel
                    break

            if relation == "same":
                decisions.append(self._merge(target, now))
            elif relation == "contradict":
                decisions.append(self._supersede(target, claim, importance, now))
            elif relation == "complement":
                status = "active" if importance >= ACTIVE_AT and claim.confidence >= 0.6 else "candidate"
                decisions.append(self._insert(claim, importance, status, now, source))
            else:
                status = "active" if importance >= ACTIVE_AT and claim.confidence >= 0.6 else "candidate"
                decisions.append(self._insert(claim, importance, status, now, source))
        return decisions

    def _similar(self, claim: Claim) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM memories
            WHERE subject = ? AND predicate = ?
              AND status IN ('candidate', 'active', 'disputed', 'warm')
            ORDER BY recorded_at DESC
            LIMIT 5
            """,
            (claim.subject, claim.predicate),
        ).fetchall()

    def _insert(
        self,
        claim: Claim,
        importance: float,
        status: str,
        now: str,
        source: str,
        supersedes: str | None = None,
    ) -> dict:
        mid = "mem_" + uuid.uuid4().hex[:8]
        self.conn.execute(
            """
            INSERT INTO memories
              (id, kind, subject, predicate, object, text, valid_from, recorded_at,
               confidence, importance, sensitivity, status, supersedes, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mid, claim.kind, claim.subject, claim.predicate, claim.object,
                claim.text, now, now, claim.confidence, importance,
                claim.sensitivity, status, supersedes, source,
            ),
        )
        self.conn.commit()
        return {
            "action": "add", "id": mid, "status": status,
            "predicate": claim.predicate, "object": claim.object,
        }

    def _merge(self, old: sqlite3.Row, now: str) -> dict:
        evidence = old["evidence_count"] + 1
        confidence = min(0.99, old["confidence"] + 0.1)
        status = "active" if old["status"] == "candidate" and evidence >= 2 else old["status"]
        self.conn.execute(
            "UPDATE memories SET evidence_count = ?, confidence = ?, status = ? WHERE id = ?",
            (evidence, confidence, status, old["id"]),
        )
        self.conn.commit()
        return {
            "action": "merge", "id": old["id"], "status": status,
            "predicate": old["predicate"], "object": old["object"],
            "evidence": evidence,
        }

    def _supersede(self, old: sqlite3.Row, claim: Claim, importance: float, now: str) -> dict:
        # 只有新证据足够硬才自动替换；否则双方都留着，标 disputed。
        if importance >= old["importance"] and claim.confidence >= 0.6:
            self.conn.execute(
                "UPDATE memories SET status = 'superseded', valid_to = ? WHERE id = ?",
                (now, old["id"]),
            )
            new = self._insert(claim, importance, "active", now, "user", supersedes=old["id"])
            self.conn.commit()
            new.update({"action": "supersede", "old": old["id"]})
            return new

        new = self._insert(claim, importance, "disputed", now, "user", supersedes=old["id"])
        new.update({"action": "disputed", "old": old["id"]})
        return new

    # ------------------------------------------------------------ 读取

    def recall(self, query: str, now: datetime | str, k: int = 5) -> list[dict]:
        now = iso(now)
        rows = self.conn.execute(
            """
            SELECT * FROM memories
            WHERE status IN ('active', 'warm')
              AND (valid_to IS NULL OR valid_to > ?)
            """,
            (now,),
        ).fetchall()

        q = tokens(query)
        hint_predicate = next(
            (pred for word, pred in QUERY_HINTS.items() if word in query), None
        )
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            sim = jaccard(q, tokens(row["text"] + " " + row["object"]))
            if hint_predicate and row["predicate"] == hint_predicate:
                sim = max(sim, 0.75)
            if sim < MIN_RELEVANCE:
                continue
            half_life = HALF_LIFE.get(row["predicate"], 180)
            recency = 0.5 ** (days_between(row["recorded_at"], now) / half_life)
            score = 0.55 * sim + 0.25 * (row["importance"] / 10) + 0.20 * recency
            scored.append((score, row))

        scored.sort(key=lambda pair: -pair[0])
        top = scored[:k]
        for _, row in top:
            self.conn.execute(
                "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
                (row["id"],),
            )
        self.conn.commit()
        return [
            {**dict(row), "score": round(score, 3)}
            for score, row in top
        ]

    def credit(self, ids: list[str], now: datetime | str) -> None:
        """只给真正参与生成回答的记忆加分。检索命中不算。"""
        now = iso(now)
        for mid in ids:
            self.conn.execute(
                """
                UPDATE memories
                SET used_count = used_count + 1,
                    last_used_at = ?,
                    confidence = MIN(0.99, confidence + 0.02)
                WHERE id = ?
                """,
                (now, mid),
            )
        self.conn.commit()

    def forget(self, mid: str, now: datetime | str) -> None:
        self.conn.execute(
            "UPDATE memories SET status = 'tombstone', valid_to = ? WHERE id = ?",
            (iso(now), mid),
        )
        self.conn.commit()

    # ------------------------------------------------------------ 维护

    def maintain(self, now: datetime | str, max_active: int = MAX_ACTIVE) -> list[dict]:
        now = iso(now)
        actions: list[dict] = []

        # 1. 有效期到了，下沉到 cold；不物理删除。
        for row in self.conn.execute(
            "SELECT * FROM memories WHERE status = 'active' AND valid_to IS NOT NULL AND valid_to <= ?",
            (now,),
        ).fetchall():
            self.conn.execute("UPDATE memories SET status = 'cold' WHERE id = ?", (row["id"],))
            actions.append({"action": "expire", "id": row["id"], "object": row["object"]})

        # 2. 衰减。active 低于 0.25 转 warm，warm 低于 0.08 转 cold。
        for row in self.conn.execute(
            "SELECT * FROM memories WHERE status IN ('active', 'warm')"
        ).fetchall():
            last = row["last_used_at"] or row["recorded_at"]
            half_life = HALF_LIFE.get(row["predicate"], 180)
            retention = 0.5 ** (days_between(last, now) / half_life)
            if row["status"] == "active" and retention < 0.25:
                self.conn.execute("UPDATE memories SET status = 'warm' WHERE id = ?", (row["id"],))
                actions.append({"action": "demote_warm", "id": row["id"], "object": row["object"]})
            elif row["status"] == "warm" and retention < 0.08:
                self.conn.execute("UPDATE memories SET status = 'cold' WHERE id = ?", (row["id"],))
                actions.append({"action": "demote_cold", "id": row["id"], "object": row["object"]})

        # 3. 预算：active 太多时按效用淘汰，而不是按时间。
        active = self.conn.execute("SELECT * FROM memories WHERE status = 'active'").fetchall()
        active = sorted(active, key=lambda row: self._utility(row, now))
        while len(active) > max_active:
            row = active.pop(0)
            self.conn.execute("UPDATE memories SET status = 'warm' WHERE id = ?", (row["id"],))
            actions.append({"action": "budget_demote", "id": row["id"], "object": row["object"]})

        self.conn.commit()
        return actions

    def _utility(self, row: sqlite3.Row, now: str) -> float:
        last = row["last_used_at"] or row["recorded_at"]
        half_life = HALF_LIFE.get(row["predicate"], 180)
        retention = 0.5 ** (days_between(last, now) / half_life)
        return (row["importance"] / 10) * retention * (1 + 0.1 * row["used_count"])

    # ------------------------------------------------------------ 观测

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM memories GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def dump(self) -> None:
        rows = self.conn.execute(
            "SELECT status, predicate, object, importance, confidence, used_count "
            "FROM memories ORDER BY recorded_at"
        ).fetchall()
        print(f"  {'status':12} {'predicate':10} {'object':18} {'imp':>4} {'conf':>5} {'used':>4}")
        for row in rows:
            print(
                f"  {row['status']:12} {row['predicate']:10} {row['object']:18} "
                f"{row['importance']:>4.0f} {row['confidence']:>5.2f} {row['used_count']:>4}"
            )


# ---------------------------------------------------------------- 演示


def demo() -> None:
    store = MemoryStore()
    day0 = datetime(2026, 1, 1, 9, 0, 0)

    turns = [
        "我对花生过敏",
        "我住在杭州",
        "提醒我明天下午三点开会",
        "我搬到上海了",
        "我喜欢喝美式",
        "我喜欢喝美式",
        "我现在喜欢拿铁了",
        "记住：我的工位在 3 楼靠窗",
    ]

    print("== 写入 ==")
    for i, text in enumerate(turns):
        now = day0 + timedelta(days=i)
        print(f"\n[turn {i}] {text}")
        for decision in store.write(text, now):
            print("  ", decision)

    now = day0 + timedelta(days=10)
    print("\n== 检索：我住哪 ==")
    for row in store.recall("我住哪", now):
        print(f"   {row['status']:8} {row['object']:10} score={row['score']}")
        assert row["object"] == "上海", "旧地址不应被召回"

    print("\n== 检索：花生 ==")
    allergy = None
    for row in store.recall("花生", now):
        print(f"   {row['status']:8} {row['object']:10} score={row['score']}")
        if row["predicate"] == "allergy":
            allergy = row["id"]

    assert allergy, "过敏事实应能被召回"
    store.credit([allergy], now)

    print("\n== 400 天后维护（max_active=2）==")
    for action in store.maintain(day0 + timedelta(days=400), max_active=2):
        print("  ", action)

    print("\n== 维护后检索：花生 ==")
    for row in store.recall("花生", day0 + timedelta(days=400)):
        print(f"   {row['status']:8} {row['object']:10} score={row['score']}")

    print("\n== 800 天后维护 ==")
    for action in store.maintain(day0 + timedelta(days=800), max_active=2):
        print("  ", action)

    print("\n== 全表 ==")
    store.dump()
    print("\n状态计数：", store.stats())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    else:
        print("用法：python3 agent-memory.py demo")
