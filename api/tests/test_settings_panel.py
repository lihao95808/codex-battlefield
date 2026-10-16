"""运行时配置面板的判据（P2-2）。

这个面板最容易出的不是崩溃，而是**改了一个不生效的项**，或者**手滑把权限放开了**。
所以本文件绝大部分在钉"拒绝"与"说清代价"：

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| C1 | 不在登记表里的键必须拒 | 静默忽略 → 用户以为改成功了 |
| C2 | 危险项不带确认必须拒（409） | 点一下下拉框就把沙箱降级，事后无法归因 |
| C3 | 非法取值必须拒，且说清允许什么 | "我改成 flase 了怎么不生效" |
| C4 | 一项非法则**整批不写** | 留下"一半改了、一半没改"，而用户只看到一条错误 |
| C5 | 必须汇报"要不要重启" | 用户以为保存即生效 |
| C6 | 必须汇报**来源**（被环境变量压着） | 用户反复保存、反复重启、反复疑惑 |

C5/C6 是"诚实性"判据：这个工具里最容易误判的不是失败，而是**静默地做了一半**。
"""
from __future__ import annotations

import pathlib
import tomllib

import pytest
from fastapi.testclient import TestClient

from app import settings_registry


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """隔离的用户配置文件 —— **不能**写开发机真实的 `~/.codex-battlefield.toml`。"""
    config = tmp_path / "cfg.toml"
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(config))
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def config_path(tmp_path) -> pathlib.Path:
    return tmp_path / "cfg.toml"


# ---------- C1 键必须在登记表里 ----------


def test_c1_unknown_key_is_refused(client: TestClient, config_path: pathlib.Path) -> None:
    """未知键必须 400，**不能**静默忽略。"""
    response = client.post("/api/settings", json={"values": {"definitely_not_a_key": 1}})
    assert response.status_code == 400, response.text
    assert "definitely_not_a_key" in response.json()["detail"]
    assert not config_path.exists(), "被拒的请求不该写文件"


def test_c1_every_registered_key_is_readable_from_config() -> None:
    """登记表里的每一项都必须是 `_env_or_config` 真正会读的键。

    这条防的是"登记了一个只认环境变量的项"——那种项写进文件没人读，
    表现是"我改了、保存了、重启了，它还是旧的"，而每一步看起来都成功了。

    判据用**源码级**核对：字段默认值里必须出现 `_env_or_config(..., "<key>")`。
    """
    import inspect

    from app.config import Settings

    source = inspect.getsource(Settings)
    for item in settings_registry.SETTINGS:
        assert f'"{item.key}"' in source, (
            f"{item.key} 登记为可编辑，但 Settings 里没有按 config key 读它"
        )


# ---------- C2 危险项二次确认 ----------


def test_c2_risky_change_without_confirmation_is_refused(
    client: TestClient, config_path: pathlib.Path
) -> None:
    """危险项不带 `confirm_risky` 必须 409，并且**不写文件**。"""
    response = client.post("/api/settings", json={"values": {"steward_read_only": False}})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "管家只读" in detail, f"没说清是哪一项危险：{detail}"
    assert not config_path.exists(), "被拒的危险改动不该落盘"


def test_c2_risky_change_with_confirmation_is_saved(
    client: TestClient, config_path: pathlib.Path
) -> None:
    """对照：带了确认就真的写下去（证明上一条不是恒真）。"""
    response = client.post(
        "/api/settings",
        json={"values": {"steward_read_only": False}, "confirm_risky": True},
    )
    assert response.status_code == 200, response.text
    written = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert written["steward_read_only"] is False


def test_c2_non_risky_change_needs_no_confirmation(client: TestClient) -> None:
    """非危险项不该被迫二次确认 —— 否则确认会变成无脑点"是"。"""
    response = client.post(
        "/api/settings", json={"values": {"steward_push_throttle_seconds": 45}}
    )
    assert response.status_code == 200, response.text


# ---------- C3 取值校验 ----------


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("steward_push_min_level", "nonsense"),
        ("steward_push_throttle_seconds", -5),
        ("steward_proactive_enabled", "perhaps"),
        ("runner_mode", "telepathy"),
    ],
)
def test_c3_invalid_values_are_refused_with_the_allowed_set(
    client: TestClient, key: str, bad: object
) -> None:
    """非法值必须 400，且错误里要**说清允许什么**，否则用户没法纠正。"""
    response = client.post("/api/settings", json={"values": {key: bad}})
    assert response.status_code == 400, f"{key}={bad!r} 被放行了：{response.text}"
    detail = response.json()["detail"]
    assert detail, "拒绝时必须有原因"


def test_c3_error_names_the_allowed_choices(client: TestClient) -> None:
    """枚举类错误要把可选值摆出来。"""
    response = client.post(
        "/api/settings", json={"values": {"steward_push_min_level": "nonsense"}}
    )
    detail = response.json()["detail"]
    for choice in ("info", "warning", "action"):
        assert choice in detail, f"错误里没列可选值 {choice}：{detail}"


# ---------- C4 整批不写 ----------


def test_c4_one_bad_value_means_nothing_is_written(
    client: TestClient, config_path: pathlib.Path
) -> None:
    """一批里有一项非法 → **整批不写**。

    分批写会留下"一半改了、一半没改"，而用户只看到一条错误 ——
    那种状态比干脆全不写更难排查。
    """
    response = client.post(
        "/api/settings",
        json={
            "values": {
                "steward_push_throttle_seconds": 45,
                "steward_push_min_level": "nonsense",
            }
        },
    )
    assert response.status_code == 400
    assert not config_path.exists(), "有一项非法，另一项却被写下去了"


# ---------- C5 说清要不要重启 ----------


def test_c5_response_declares_restart_required(client: TestClient) -> None:
    """保存成功必须明确说"要重启才生效"，而不是让用户自己猜。"""
    body = client.post(
        "/api/settings", json={"values": {"steward_push_throttle_seconds": 30}}
    ).json()
    assert body["restart_required"] is True
    assert "重启" in body["message"], f"没提重启：{body['message']}"


def test_c5_catalog_declares_restart_required(client: TestClient) -> None:
    """读取接口也要带上这一项 —— 界面在**保存前**就该告诉用户。"""
    body = client.get("/api/settings").json()
    assert body["restart_required"] is True
    assert body["config_file"]


# ---------- C6 说清来源 ----------


def test_c6_catalog_reports_value_sources(client: TestClient) -> None:
    """每项都要报"当前值 + 来源"三个字段。"""
    body = client.get("/api/settings").json()
    current = body["current"]
    assert current, "没有回报任何当前值"
    for key, entry in current.items():
        assert set(entry) >= {"value", "source", "env_override"}, f"{key} 缺字段：{entry}"
        assert entry["source"] in {"env", "config", "default"}


def test_c6_env_override_is_reported_not_hidden(
    client: TestClient, monkeypatch
) -> None:
    """被环境变量压着时必须标出来 —— 这是最容易让用户白忙一场的状态。

    优先级是「环境变量 > 文件 > 默认」，所以这类项在界面上改**不会有任何效果**。
    不报的话，用户会反复保存、反复重启、反复疑惑。
    """
    monkeypatch.setenv("CODEX_BATTLEFIELD_STEWARD_PUSH_THROTTLE_SECONDS", "77")
    body = client.get("/api/settings").json()
    entry = body["current"]["steward_push_throttle_seconds"]
    assert entry["env_override"] is True, "被环境变量压着却没标出来"
    assert entry["source"] == "env"


def test_c6_no_override_reports_config_source(client: TestClient, config_path: pathlib.Path) -> None:
    """对照：没被环境变量压着、且文件里写了 → 来源是 config。"""
    config_path.write_text("steward_push_throttle_seconds = 12\n", encoding="utf-8")
    body = client.get("/api/settings").json()
    entry = body["current"]["steward_push_throttle_seconds"]
    assert entry["source"] == "config"
    assert entry["env_override"] is False


# ---------- 默认值（安全相关的默认值不能静默翻转） ----------


def test_steward_read_only_defaults_to_true(tmp_path, monkeypatch) -> None:
    """**默认必须只读。**

    回归（2026-10-16 实测）：把 `steward_read_only` 从"只认环境变量"改成
    "也读配置文件"时，我顺手用了 `_env_truthy`（默认**关**的语义），
    于是未配置时它变成 `False` —— **静默把只读关掉了**，
    三个只读判据（`test_s1_*`、`test_a3_*`）当场变红。

    这条判据单独钉"默认值"本身，而不是只依赖那些行为判据：
    行为判据报的是"能力被放行了"，读起来像功能问题；而这是**权限默认值翻转**，
    属安全问题，值得有一条直说它的用例。
    """
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.delenv("CODEX_BATTLEFIELD_STEWARD_READ_ONLY", raising=False)
    from app.config import get_settings

    assert get_settings().steward_read_only is True, (
        "管家默认不再是只读 —— 未配置时它就该是只读，这是用户有意选的权限取舍"
    )


def test_proactive_defaults_to_off(tmp_path, monkeypatch) -> None:
    """主动汇报默认**关**：它会花钱、会打扰，该由用户显式打开。"""
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.delenv("CODEX_BATTLEFIELD_STEWARD_PROACTIVE_ENABLED", raising=False)
    from app.config import get_settings

    assert get_settings().steward_proactive_enabled is False


def test_push_min_level_defaults_to_action(tmp_path, monkeypatch) -> None:
    """推送阈值默认 `action` —— 唯一需要用户动手的那一类。"""
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.delenv("CODEX_BATTLEFIELD_STEWARD_PUSH_MIN_LEVEL", raising=False)
    from app.config import get_settings

    assert get_settings().steward_push_min_level == "action"


# ---------- 登记表自身的一致性 ----------


def test_catalog_covers_every_group_declared() -> None:
    """登记表里的分组必须都在 GROUPS 里，否则界面上会少一块。"""
    declared = {group for group, _ in settings_registry.GROUPS}
    used = {item.group for item in settings_registry.SETTINGS}
    assert used <= declared, f"这些分组没在 GROUPS 里声明：{sorted(used - declared)}"


def test_catalog_orders_by_groups() -> None:
    """界面顺序按 GROUPS，不按声明顺序 —— 日常会调的该排前面。"""
    order = [group for group, _ in settings_registry.GROUPS]
    seen = [item["group"] for item in settings_registry.catalog()]
    ranks = [order.index(group) for group in seen]
    assert ranks == sorted(ranks), f"分组顺序没按 GROUPS 排：{seen}"
