"""每日额度跨运行/跨日、防并发超额及可选名单的业务边界。"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from job_hunter import cli
from job_hunter.models.preferences import Candidate, Preferences
from job_hunter.settings import ConfigurationError, load_yaml
from job_hunter.storage import daily
from job_hunter.storage.run_log import RunLog


def row(kind, job=None, day="2026-11-11", **fields):
    return {
        "event": kind,
        "at": f"{day}T12:00:00+08:00",
        "run_id": "run",
        **({"job_id": job} if job else {}),
        **fields,
    }


def save(directory, rows):
    (directory / "history.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    )


def test_daily_unknown_reconcile_dates_and_dedup(tmp_path):
    save(
        tmp_path,
        [
            row("communicate_intent", "old", day="2026-11-10"),
            row("communicate_result", "old", status="confirmed_sent"),
            row("communicate_intent", "sent"),
            row("communicate_result", "sent", status="confirmed_sent"),
            row("communicate_result", "sent", status="confirmed_sent"),
            row("communicate_intent", "unknown"),
            row("run_start", mode="apply"),
            row("decision", "skip", status="SKIP"),
            row("job_result", "skip", status="SKIP"),
            row("decision", "review", status="REVIEW"),
        ],
    )
    state = daily.snapshot(tmp_path, "2026-11-11")
    assert (state["sent"], state["reserved"], state["remaining"], state["skipped"]) == (
        1,
        1,
        148,
        2,
    )
    assert daily.snapshot(tmp_path, "2026-11-10")["sent"] == 1
    assert daily.snapshot(tmp_path, "2026-11-12")["remaining"] == 150
    assert "公司/岗位名单：未记录" in daily.brief(state)


@pytest.mark.parametrize("value", [-1, 151, 1.5, True])
def test_target_outside_range_rejected(value):
    with pytest.raises(ConfigurationError):
        daily.validate_target(value, {"remaining": 150, "platform_blocked": False})


def test_target_cannot_exceed_remaining_or_platform_limit():
    with pytest.raises(ConfigurationError, match="剩余额度100"):
        daily.validate_target(101, {"remaining": 100, "platform_blocked": False})
    daily.validate_target(100, {"remaining": 100, "platform_blocked": False})
    daily.validate_target(0, {"remaining": 0, "platform_blocked": True})
    with pytest.raises(ConfigurationError, match="平台"):
        daily.validate_target(1, {"remaining": 100, "platform_blocked": True})


def test_brief_lists_only_opt_in_successes_at_end(tmp_path):
    save(
        tmp_path,
        [
            row("run_start", mode="apply"),
            row("decision", "skip", status="SKIP", company="跳过公司"),
            row(
                "favorite_result",
                "a",
                status="confirmed",
                company="收藏公司",
                title="Agent",
            ),
            row(
                "favorite_result",
                "a",
                status="confirmed",
                company="收藏公司",
                title="Agent",
            ),
            row("communicate_intent", "b"),
            row("communicate_result", "b", status="confirmed_sent"),
            row("communicate_intent", "c"),
            row(
                "communicate_result",
                "c",
                status="confirmed_sent",
                company="沟通公司",
                title="开发",
            ),
            row("communicate_result", "c", status="confirmed_sent"),
            row("favorite_result", "d", status="unknown"),
        ],
    )
    text = daily.brief(daily.snapshot(tmp_path, "2026-11-11"))
    assert "已沟通 2｜已收藏 1｜已跳过 1" in text
    assert text.count("收藏公司") == 1 and text.count("沟通公司") == 1
    assert "跳过公司" not in text
    assert text.index("今日剩余") < text.index("已收藏名单") < text.index("已沟通名单")


def test_corrupt_log_fails_closed(tmp_path):
    (tmp_path / "bad.jsonl").write_text("{")
    with pytest.raises(ConfigurationError):
        daily.snapshot(tmp_path)


def make_log(directory):
    preferences = load_yaml(Path("tests/fixtures/preferences.yaml"), Preferences)
    return RunLog(preferences, Candidate(), directory, compact_apply=True)


def test_last_slot_atomic_and_unknown_persists_across_restart(tmp_path):
    date = daily.today()
    save(tmp_path, [row("communicate_intent", str(i), day=date) for i in range(149)])
    logs = [make_log(tmp_path), make_log(tmp_path)]

    def attempt(index):
        try:
            logs[index].append(
                {"event": "communicate_intent", "job_id": "new" + str(index)}
            )
            return True
        except ConfigurationError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(attempt, range(2))) == 1
    state = daily.snapshot(tmp_path)
    assert state["used"] == 150 and state["reserved"] == 150 and state["remaining"] == 0
    with pytest.raises(ConfigurationError):
        make_log(tmp_path).append({"event": "communicate_intent", "job_id": "restart"})


def test_no_repeat_and_compact_default_does_not_store_names(tmp_path):
    log = make_log(tmp_path)
    log.append({"event": "run_start", "mode": "apply"})
    log.append(
        {
            "event": "decision",
            "job_id": "a",
            "status": "REVIEW",
            "company": "不记录公司",
            "title": "不记录岗位",
            "evidence": [{"quote": "正文"}],
        }
    )
    log.append({"event": "detail_read", "company": "不记录公司"})
    log.append({"event": "communicate_intent", "job_id": "a"})
    with pytest.raises(ConfigurationError, match="PREVIOUS_ATTEMPT"):
        make_log(tmp_path).append({"event": "communicate_intent", "job_id": "a"})
    text = log.path.read_text()
    assert "不记录" not in text and "正文" not in text
    assert '"job_id": "a"' in text


@pytest.mark.parametrize(
    ("flags", "label"),
    [([], "关闭"), (["--record-lists"], "开启"), (["--no-record-lists"], "关闭")],
)
def test_cli_zero_displays_settings_without_browser_or_model(
    tmp_path, monkeypatch, flags, label
):
    # 在隔离目录中执行真实CLI，模型构造和连接发现若发生即失败。
    settings = cli.load_settings()
    preferences, candidate = cli.load_user_config()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daily, "BRIEF_DIRECTORY", tmp_path / "daily_briefs")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_user_config", lambda: (preferences, candidate))
    monkeypatch.setattr(cli, "ModelEvaluator", lambda *a: pytest.fail("不应构造模型"))
    monkeypatch.setattr(cli, "discover_endpoint", lambda: pytest.fail("不应连接浏览器"))
    result = CliRunner().invoke(cli.app, ["apply", "--count", "0", *flags])
    assert result.exit_code == 0, result.output
    assert "目标 0" in result.output and f"名单记录 {label}" in result.output
    assert "期望月薪 20–50K" in result.output
    assert not list((tmp_path / ".local/runs").glob("*.jsonl"))


def test_cli_excess_target_rejected_before_model_or_browser(tmp_path, monkeypatch):
    preferences = load_yaml(Path("tests/fixtures/preferences.yaml"), Preferences)
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".local/runs"
    directory.mkdir(parents=True)
    save(
        directory,
        [row("communicate_intent", str(i), day=daily.today()) for i in range(51)],
    )
    monkeypatch.setattr(cli, "load_user_config", lambda: (preferences, Candidate()))
    monkeypatch.setattr(cli, "ModelEvaluator", lambda *a: pytest.fail("不应构造模型"))
    result = CliRunner().invoke(cli.app, ["apply", "--count", "100"])
    assert result.exit_code == 1 and "剩余额度99" in result.output


@pytest.mark.asyncio
async def test_apply_stops_at_communication_target_not_review_count(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from job_hunter.evaluation.rules import Decision
    from job_hunter.extraction.boss_details import JobCard, JobDetail
    from job_hunter.workflows import scan
    from job_hunter.workflows.apply import ApplyActions

    preferences = load_yaml(Path("tests/fixtures/preferences.yaml"), Preferences)
    preferences.execution.max_jobs_per_run = 10
    cards = [
        JobCard(f"/job_detail/job{i}.html", f"Agent{i}", "公司", "上海")
        for i in range(5)
    ]

    async def noop(*a, **kw):
        return {}

    monkeypatch.setattr(scan, "BossAdapter", lambda _: SimpleNamespace(search=noop))
    monkeypatch.setattr(scan, "apply_filters", noop)
    monkeypatch.setattr(scan, "current_cards", AsyncMock(return_value=cards))
    reader = AsyncMock(
        side_effect=lambda s, c: JobDetail(
            c, "职责", "上海", "", reading={"status": "DOM_COMPLETE"}
        )
    )
    monkeypatch.setattr(scan, "open_detail", reader)
    decisions = iter(
        [
            Decision("SKIP", "不符"),
            Decision("GENERAL", "通用"),
            Decision("GENERAL", "通用"),
        ]
    )
    evaluator = SimpleNamespace(
        engine="test", evaluate=AsyncMock(side_effect=lambda *a: next(decisions))
    )
    actions = ApplyActions(lambda e: None, target=2, directory=tmp_path)

    async def action(*args):
        if args[-1].status == "GENERAL":
            actions.sent += 1
        return {"communicate_attempts": int(args[-1].status == "GENERAL")}

    action.stop_reason = actions.stop_reason
    session = SimpleNamespace(
        bring_to_front=AsyncMock(), evaluate=AsyncMock(return_value={})
    )
    result = await scan.scan_in_session(
        session,
        preferences,
        Candidate(),
        lambda e: None,
        evaluator=evaluator,
        action_handler=action,
    )
    assert reader.await_count == 3 and result["stop_reason"] == "communication_target"


@pytest.mark.asyncio
@pytest.mark.parametrize("record_lists", [False, True])
async def test_action_name_recording_is_explicit_and_success_only(
    monkeypatch, tmp_path, record_lists
):
    from types import SimpleNamespace

    from job_hunter.evaluation.rules import Decision
    from job_hunter.extraction.boss_details import JobCard, JobDetail
    from job_hunter.workflows import apply as workflow

    log = make_log(tmp_path)
    log.append({"event": "run_start", "mode": "apply", "record_lists": record_lists})

    async def send(session, job, before, emit):
        emit({"event": "communicate_intent", "job_id": job})
        emit({"event": "communicate_result", "job_id": job, "status": "confirmed_sent"})
        emit({"event": "stay_on_page", "job_id": job, "status": "confirmed"})

    monkeypatch.setattr(workflow, "previous_attempt", lambda _: False)
    monkeypatch.setattr(workflow, "send_reviewed", send)
    session = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "success": False,
                "buttons": [{"text": "立即沟通", "disabled": False}],
            }
        )
    )
    detail = JobDetail(
        JobCard("/job_detail/list.html", "记录岗位", "记录公司", "上海"),
        "职责",
        "上海",
        "",
    )
    actions = workflow.ApplyActions(
        log.append, target=1, directory=tmp_path, record_lists=record_lists
    )
    await actions(session, detail, {}, Decision("GENERAL", "符合"))
    assert actions.sent == 1 and actions.stop_reason() == "communication_target"
    stored = log.path.read_text()
    assert ("记录公司" in stored) is record_lists
    assert ("记录岗位" in stored) is record_lists
    assert len(daily.snapshot(tmp_path)["communication_list"]) == int(record_lists)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_click", [False, True])
async def test_platform_limit_stops_without_retry(monkeypatch, after_click):
    from types import SimpleNamespace

    from job_hunter.browser.adapter_base import PageReadError
    from job_hunter.workflows import communicate as workflow

    monkeypatch.setattr(workflow, "previous_attempt", lambda _: False)
    monkeypatch.setattr(workflow.BossAdapter, "require_signed_in", AsyncMock())
    pointer = AsyncMock()
    monkeypatch.setattr(workflow, "point", pointer)
    before = {"id": "quota", "description": "职责"}
    allowed = {
        "success": False,
        "buttons": [{"text": "立即沟通", "disabled": False, "selector": "button"}],
    }
    denied = {"success": False, "daily_limit": True, "buttons": []}
    responses = [before, allowed, True, denied] if after_click else [before, denied]
    session = SimpleNamespace(evaluate=AsyncMock(side_effect=responses))
    events = []
    with pytest.raises(PageReadError, match="PLATFORM_DAILY_LIMIT"):
        await workflow.send_reviewed(session, "quota", before, events.append)
    assert pointer.await_count == int(after_click)
    assert sum(e["event"] == "platform_daily_limit" for e in events) == 1
    assert sum(e["event"] == "communicate_intent" for e in events) == int(after_click)
    if after_click:
        assert events[-1]["status"] == "unknown"


def test_brief_one_chinese_file_per_day_with_minute_timestamp(tmp_path):
    output = tmp_path / "daily_briefs"
    text, first = daily.write_brief(tmp_path, output)
    _, second = daily.write_brief(tmp_path, output)
    assert first.parent == output and first.name.startswith(
        daily.today()[5:] + "日投递简报_" + daily.today() + " "
    )
    assert first == second and first.exists()
    assert len(list(output.glob("*.md"))) == 1
    assert "更新时间：" + daily.today() in text and "（北京时间）" in text
    assert "\n\n**今日累计：" in first.read_text()
    assert daily.BRIEF_DIRECTORY == Path(__file__).resolve().parents[2] / "daily_briefs"


def test_daily_file_merges_batches_without_duplicate_entries_or_old_snapshots(tmp_path):
    day = daily.today()
    history = [
        row(
            "run_start",
            day=day,
            run_id="first",
            mode="apply",
            at=day + "T09:10:25+08:00",
        ),
        row("communicate_intent", "a", day=day, run_id="first"),
        row(
            "communicate_result", "a", day=day, run_id="first", status="confirmed_sent"
        ),
        row("run_end", day=day, run_id="first", status="completed"),
    ]
    save(tmp_path, history)
    output = tmp_path / "daily_briefs"
    output.mkdir()
    (output / f"今日投递简报_{day}_09-15-01-123456.md").write_text("旧快照")
    first_text, path = daily.write_brief(tmp_path, output)
    assert "已沟通 1" in first_text
    assert len(list(output.glob("*.md"))) == 1
    history += [
        row(
            "run_start",
            day=day,
            run_id="second",
            mode="apply",
            at=day + "T14:20:59+08:00",
        ),
        row(
            "decision",
            "skip",
            day=day,
            run_id="second",
            status="SKIP",
            company="不写跳过公司",
        ),
        row("run_end", day=day, run_id="second", status="stopped"),
    ]
    save(tmp_path, history)
    text, updated = daily.write_brief(tmp_path, output)
    again, same = daily.write_brief(tmp_path, output)
    assert path == updated == same
    assert len(list(output.glob("*.md"))) == 1
    assert text.count("09时10分投递情况") == 1 and text.count("14时20分投递情况") == 1
    assert again.count("09时10分投递情况") == 1
    assert "已沟通 1｜已收藏 0｜已跳过 1" in text
    assert "本批提前停止" in text and "不写跳过公司" not in text
    assert "09:10:25" not in text and "14:20:59" not in text
    assert "\n\n### " in text and "\n\n- 已沟通" in text
