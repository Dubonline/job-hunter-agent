"""按上海自然日汇总额度和客户简报；运行日志是唯一动作事实源。"""

import fcntl
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from job_hunter.settings import ConfigurationError

DAILY_LIMIT = 150  # 用户指定的本机每日沟通硬上限，不代表平台实时剩余额度。
TIMEZONE = ZoneInfo("Asia/Shanghai")
BRIEF_DIRECTORY = (
    Path(__file__).resolve().parents[3] / "daily_briefs"
)  # 项目根目录下的客户简报。


def today():
    """返回上海自然日；跨午夜后发送按新的日期计数。"""
    return datetime.now(TIMEZONE).date().isoformat()


def events(directory):
    """读取最小事件；日志损坏时拒绝猜测剩余额度。"""
    for path in sorted(Path(directory).glob("*.jsonl")):
        try:
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if not isinstance(row, dict) or "event" not in row:
                    raise ValueError("invalid event")
                yield row
        except OSError, ValueError:
            raise ConfigurationError(
                "DAILY_LOG_UNREADABLE：无法安全核对今日额度"
            ) from None


def event_day(row):
    """将带时区的日志时间换算为上海日期；缺失时间不能安全推算额度。"""
    try:
        stamp = datetime.fromisoformat(row["at"])
        if stamp.tzinfo is None:
            raise ValueError("missing timezone")
        return stamp.astimezone(TIMEZONE).date().isoformat()
    except KeyError, TypeError, ValueError:
        raise ConfigurationError("DAILY_LOG_DATE_INVALID") from None


def snapshot(directory=Path(".local/runs"), day=None, locked=False):
    """读取一致的额度快照；locked只供已持有排他锁的意图写入调用，避免嵌套锁。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if locked:
        return _snapshot(directory, day)
    with (directory / ".daily.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        return _snapshot(directory, day)


def _snapshot(directory, day):
    """汇总指定自然日（默认今天），按岗位ID去重；未知意图占额，补核不算新投递。

    只有记录开关启用后直接写入成功事件的公司/岗位可进入名单；不从历史JD回填。
    """
    day = day or today()
    rows = list(events(directory))
    intents, confirmed, favorites, skipped = {}, {}, {}, set()
    modes = {}
    starts, ends, owners, skip_owners = (
        {},
        {},
        {},
        {},
    )  # 用原始运行归属重建批次，重复生成不追加假批次。
    blocked = False
    for row in rows:
        kind, job = row["event"], row.get("job_id")
        date = event_day(row)
        if kind == "run_start":
            modes[row.get("run_id")] = row.get("mode")
            starts[row.get("run_id")] = row
        if kind == "run_end":
            ends[row.get("run_id")] = row
        if kind == "communicate_intent" and job:
            intents.setdefault(job, date)
            owners.setdefault(job, row.get("run_id"))
        if (
            kind == "communicate_result"
            and row.get("status") == "confirmed_sent"
            and job
        ):
            # confirmed_sent可能由另一次会话补核；归属于原始尝试日期。
            confirmed[job] = {**confirmed.get(job, {}), **row}
        if (
            kind == "favorite_result"
            and row.get("status") == "confirmed"
            and date == day
            and job
        ):
            favorites[job] = row
        if (
            kind in {"decision", "job_result"}
            and row.get("status") in {"SKIP", "REVIEW"}
            and date == day
        ):
            if modes.get(row.get("run_id")) == "apply" and job:
                skipped.add(job)
                skip_owners.setdefault(job, row.get("run_id"))
        if kind == "platform_daily_limit" and date == day:
            blocked = True
    used = {job for job, date in intents.items() if date == day}
    sent = {
        job: row
        for job, row in confirmed.items()
        if intents.get(job, event_day(row)) == day
    }
    used.update(sent)
    batches = []
    for run_id, start in starts.items():
        if event_day(start) != day:
            continue
        batch_sent = sum(
            owners.get(job, row.get("run_id")) == run_id for job, row in sent.items()
        )
        batch_favorites = sum(row.get("run_id") == run_id for row in favorites.values())
        batch_reserved = sum(owners.get(job) == run_id for job in used - set(sent))
        # 补核消息归原始投递批次；无业务事件且未结束的旧孤立run_start不算一次投递。
        if not (
            batch_sent
            or batch_favorites
            or batch_reserved
            or (modes[run_id] == "apply" and run_id in ends)
        ):
            continue
        batches.append(
            {
                "at": start["at"],
                "sent": batch_sent,
                "favorites": batch_favorites,
                "skipped": sum(owner == run_id for owner in skip_owners.values()),
                "reserved": batch_reserved,
                "status": ends.get(run_id, {}).get("status", "running"),
            }
        )
    batches.sort(key=lambda row: row["at"])
    return {
        "day": day,
        "limit": DAILY_LIMIT,
        "sent": len(sent),
        "favorites": len(favorites),
        "skipped": len(skipped),
        "reserved": len(used - set(sent)),
        "used": len(used),
        "remaining": max(0, DAILY_LIMIT - len(used)),
        "platform_blocked": blocked,
        "batches": batches,
        "communication_list": [
            r for r in sent.values() if r.get("company") and r.get("title")
        ],
        "favorite_list": [
            r for r in favorites.values() if r.get("company") and r.get("title")
        ],
    }


def validate_target(target, state):
    """本次目标单位为沟通人数；不静默截断，0表示不启动浏览器。"""
    if (
        not isinstance(target, int)
        or isinstance(target, bool)
        or not 0 <= target <= DAILY_LIMIT
    ):
        raise ConfigurationError("本次沟通目标须为0–150之间的整数")
    if target > state["remaining"]:
        raise ConfigurationError(
            f"本次目标{target}超过今日剩余额度{state['remaining']}，请修改数量"
        )
    if target and state["platform_blocked"]:
        raise ConfigurationError("平台今日已提示沟通上限，今天停止新增沟通")


def brief(state, generated_at=None):
    """以Markdown标题、空行和列表保证换行；今日累计在前，真实批次在后。"""
    now = (generated_at or datetime.now(TIMEZONE)).astimezone(TIMEZONE)
    lines = [
        f"# {now:%m月%d日%H时%M分}投递情况",
        "",
        f"**今日累计：已沟通 {state['sent']}｜已收藏 {state['favorites']}｜已跳过 {state['skipped']}**",
        "",
        f"- 今日剩余额度 {state['remaining']}/{state['limit']}（本机记录）",
        f"- 更新时间：{now:%Y-%m-%d %H:%M}（北京时间）",
    ]
    if state["reserved"]:
        lines.append(f"- 待核实沟通 {state['reserved']}，已预占额度，禁止自动重试")
    if state["platform_blocked"]:
        lines.append("- 平台今日沟通额度已耗尽，已停止")
    if not state["favorite_list"] and not state["communication_list"]:
        lines.append("- 公司/岗位名单：未记录")
    else:
        lines.append("- 名单仅包含开启记录时的成功动作")
    if state.get("batches"):
        lines.extend(["", "## 分批投递", ""])
        for batch in state["batches"]:
            stamp = datetime.fromisoformat(batch["at"]).astimezone(TIMEZONE)
            lines.extend(
                [
                    f"### {stamp:%m月%d日%H时%M分}投递情况",
                    "",
                    f"- 已沟通 {batch['sent']}｜已收藏 {batch['favorites']}｜已跳过 {batch['skipped']}",
                ]
            )
            if batch["reserved"]:
                lines.append(f"- 待核实沟通 {batch['reserved']}，已预占额度")
            if batch["status"] == "stopped":
                lines.append("- 本批提前停止")
            elif batch["status"] == "running":
                lines.append("- 本批尚未结束")
            lines.append("")
    # 名单始终置于末尾；跳过只计数，不列公司或岗位。
    for label, key in [("已收藏", "favorite_list"), ("已沟通", "communication_list")]:
        if state[key]:
            lines.extend(
                [
                    "",
                    f"<details><summary>{label}名单（{len(state[key])}）</summary>",
                    "",
                ]
            )
            for row in state[key]:
                name = " ".join(str(row["company"]).split())
                title = " ".join(str(row["title"]).split())
                lines.append(f"- {name}｜{title}")
            lines.extend(["", "</details>"])
    return "\n".join(lines).rstrip() + "\n"


def write_brief(directory=Path(".local/runs"), output=None):
    """同日只保留一份中文简报，分钟级命名；每次从事实日志更新累计及各批次。

    文件名时间固定为首次生成的分钟，正文更新时间持续变化；旧秒级快照合并清理。
    """
    output = BRIEF_DIRECTORY if output is None else Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # 同日多个生成进程串行写入；原子替换避免读者看到半份文件。
    with (output / ".brief.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = datetime.now(TIMEZONE)
        state = snapshot(directory, day=now.date().isoformat())
        text = brief(state, generated_at=now)
        existing = sorted(output.glob(f"{now:%m-%d}日投递简报_{now:%Y-%m-%d} *.md"))
        path = (
            existing[0]
            if existing
            else output / f"{now:%m-%d}日投递简报_{now:%Y-%m-%d %H:%M}.md"
        )
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=output, prefix=".brief-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(text)
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        legacy = list(output.glob(f"今日投递简报_{now:%Y-%m-%d}_*.md"))
        for duplicate in [*existing[1:], *legacy]:
            duplicate.unlink()
    return text, path
