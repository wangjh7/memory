"""最小的 Write → Reconcile → Retrieve 闭环。

只保留三件事：
  Write      : 来源门禁（UNTRUSTED 不进库）
  Reconcile  : 同槽位按策略处理新旧事实（LWW 覆盖 / MERGE 并存），旧事实进 history
  Retrieve   : 渲染成可放进 Prompt 的文本（只含生效事实，顺序确定）
"""

import time
from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum


# ── 数据模型 ─────────────────────────────────────────────────────────────
class Trust(IntEnum):
    TRUSTED = 3      # 用户亲口说的 / 权威接口
    CONDITIONAL = 2  # Agent 自己推断 / 可信工具返回
    UNTRUSTED = 1    # 邮件、网页、第三方文本


class Policy(Enum):
    LWW = "lww"      # 单值：新的覆盖旧的
    MERGE = "merge"  # 多值：互补并存


@dataclass
class Fact:
    slot: str        # 如 "user.lang_pref"
    value: str
    source: str      # 如 "user_msg" / "email"
    trust: Trust
    ts: float = field(default_factory=time.time)


class Reason(Enum):
    STORED = "stored"
    SUPERSEDED = "superseded"
    UPGRADED = "upgraded"
    UNCHANGED = "unchanged"
    REJECTED_UNTRUSTED = "rejected_untrusted"
    REJECTED_LOWER_TRUST = "rejected_lower_trust"


@dataclass
class WriteResult:
    accepted: bool
    reason: Reason
    message: str = ""


# ── 存储 ─────────────────────────────────────────────────────────────────
class MemoryStore:
    def __init__(self):
        # 每个槽位对应一个列表：LWW 槽位最多 1 项，MERGE 槽位可有多项
        self.facts: dict[str, list[Fact]] = {}
        self.history: list[Fact] = []  # 被取代的旧事实（不物理删除）
        self.policies: dict[str, Policy] = {}

    def register(self, slot: str, policy: Policy) -> None:
        self.policies[slot] = policy

    # ── Write + Reconcile ────────────────────────────────────────────────
    def write(self, fact: Fact) -> WriteResult:
        # 1) 门禁：不可信来源一律不进库
        if fact.trust == Trust.UNTRUSTED:
            return WriteResult(False, Reason.REJECTED_UNTRUSTED,
                               f"'{fact.slot}' 来自不可信来源，已拒绝")

        policy = self.policies.get(fact.slot, Policy.LWW)
        current = self.facts.setdefault(fact.slot, [])

        # 2) Reconcile（a）：同值 —— 来源更高就升级，否则不动
        for i, old in enumerate(current):
            if old.value == fact.value:
                if fact.trust > old.trust:
                    self._retire(old)
                    current[i] = replace(fact)
                    return WriteResult(True, Reason.UPGRADED)
                return WriteResult(True, Reason.UNCHANGED)

        # 2) Reconcile（b）：不同值 —— 按槽位策略处理
        if policy is Policy.MERGE:
            current.append(replace(fact))          # 互补：直接并存
            return WriteResult(True, Reason.STORED)

        # LWW
        if not current:
            current.append(replace(fact))
            return WriteResult(True, Reason.STORED)
        if current[0].trust > fact.trust:          # 低可信度不能覆盖高可信度
            return WriteResult(False, Reason.REJECTED_LOWER_TRUST,
                               f"'{fact.slot}' 新值可信度低于旧值，已拒绝覆盖")
        self._retire(current[0])
        current[0] = replace(fact)
        return WriteResult(True, Reason.SUPERSEDED)

    def _retire(self, old: Fact) -> None:
        self.history.append(old)  # 旧事实留痕

    # ── Retrieve ─────────────────────────────────────────────────────────
    def get(self, slot: str) -> list[str]:
        return [f.value for f in self.facts.get(slot, [])]

    def render(self, min_trust: Trust = Trust.CONDITIONAL) -> str:
        """渲染成 Prompt 片段。顺序 = 槽位首次出现的顺序，所以：
        新增槽位只会追加在末尾，已有部分的前缀保持不变（利于缓存）。"""
        lines = []
        for slot, facts in self.facts.items():
            values = [f.value for f in facts if f.trust >= min_trust]
            if values:
                lines.append(f"- {slot}: {'；'.join(values)}\n")
        return "".join(lines)


# ── 演示 + 断言 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    s = MemoryStore()
    s.register("user.forbidden", Policy.MERGE)

    # Write：正常写入
    r = s.write(Fact("user.lang_pref", "Python", "user_msg", Trust.TRUSTED))
    assert r.reason == Reason.STORED

    # Reconcile：LWW 覆盖，旧值进 history
    r = s.write(Fact("user.lang_pref", "TypeScript", "user_msg", Trust.TRUSTED))
    assert r.reason == Reason.SUPERSEDED
    assert s.get("user.lang_pref") == ["TypeScript"]
    assert [f.value for f in s.history] == ["Python"]

    # Reconcile：低可信度不能覆盖高可信度
    r = s.write(Fact("user.lang_pref", "Go", "agent_guess", Trust.CONDITIONAL))
    assert not r.accepted and r.reason == Reason.REJECTED_LOWER_TRUST

    # Reconcile：MERGE 并存，来源各自保留
    s.write(Fact("user.forbidden", "不要用 emoji", "agent_guess", Trust.CONDITIONAL))
    s.write(Fact("user.forbidden", "输出不要太长", "user_msg", Trust.TRUSTED))
    assert set(s.get("user.forbidden")) == {"不要用 emoji", "输出不要太长"}

    # Write：不可信来源被拦
    r = s.write(Fact("user.cc", "x@example.com", "email", Trust.UNTRUSTED))
    assert not r.accepted and r.reason == Reason.REJECTED_UNTRUSTED
    assert s.get("user.cc") == []

    # Retrieve：渲染 + 按可信度过滤 + 确定性
    out = s.render()
    print(out)
    assert out == s.render()
    assert "不要用 emoji" in out
    assert "不要用 emoji" not in s.render(min_trust=Trust.TRUSTED)
    assert "x@example.com" not in out

    # Retrieve：新增槽位不改动已有前缀
    before = s.render()
    s.write(Fact("user.tz", "Asia/Singapore", "user_msg", Trust.TRUSTED))
    assert s.render().startswith(before)

    print("all assertions passed")