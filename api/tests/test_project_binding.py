"""工程绑定端点的判据（P1-5：首次运行引导的后端）。

界面上"绑定工程"这个按钮背后有两条容易出错的边界，本文件钉它们：

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| B1 | 目录**不存在**时必须 400 | 打错一个字母会静默绑成一个永远跑不起来的工程（实测踩到过） |
| B2 | 不能把**状态根**（或它下面）当工程 | 状态会被卷进"工程材料"，worktree/runs/registry 全乱 |
| B3 | 绑定后 `active_key` 立刻变、且 `restart_required` 为真 | 用户以为"点了就切过去了"，其实当前进程还连着旧库 |
| B4 | 空白/相对路径必须 400 | 相对路径会随 cwd 变化，同一个工程得到不同 key |

B1/B4 是"拒绝"类判据；B3 是"说清代价"类判据 —— 后者同样重要：
这个系统里最容易误判的不是失败，而是**静默地做了一半**。
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def target_project(tmp_path):
    """一个**在状态根之外**的工程目录。

    必须在外：`validate_project_root` 明确拒绝状态根（及其子目录）当工程，
    而测试的隔离状态根就在 tmp_path 下 —— 放里面会先撞上那条。
    """
    root = pathlib.Path(str(tmp_path)) / "outside" / "my-project"
    root.mkdir(parents=True)
    (root / "README.md").write_text("# project\n", encoding="utf-8")
    return root


def test_b3_list_projects_reports_shape(client: TestClient) -> None:
    """清单端点必须给出 state_home / active_key / projects 三样。"""
    body = client.get("/api/projects").json()
    assert set(body) >= {"state_home", "active_key", "projects"}
    assert isinstance(body["projects"], list)
    for item in body["projects"]:
        # 界面靠这些字段决定"能不能切""要不要警告"
        assert set(item) >= {
            "key",
            "root",
            "active",
            "root_exists",
            "drive_matches",
            "db_exists",
        }


def test_b1_binding_a_missing_directory_is_refused(client: TestClient) -> None:
    """**这条是实测事故的回归**：不存在的目录当时被成功挂载了。

    界面是输入/点选，打错一个字母就会绑成一个跑不起来的工程，
    而用户看到的是"绑定成功"。所以 API 层必须要求目录真实存在
    （CLI 仍允许"先绑后建"，那是另一个取舍）。
    """
    response = client.post("/api/projects/bind", json={"path": "Z:\\definitely-not-here"})
    assert response.status_code == 400, response.text
    assert "不存在" in response.json()["detail"]


def test_b2_binding_the_state_root_is_refused(client: TestClient) -> None:
    """状态根自己不能当工程 —— 那会让状态被当成"被改的工程"。"""
    state_home = client.get("/api/projects").json()["state_home"]
    response = client.post("/api/projects/bind", json={"path": state_home})
    assert response.status_code == 400, response.text
    assert "状态根" in response.json()["detail"]


def test_b2_binding_a_child_of_the_state_root_is_refused(
    client: TestClient, tmp_path
) -> None:
    """状态根**下面**的目录同样不行（更隐蔽的一种）。"""
    state_home = pathlib.Path(client.get("/api/projects").json()["state_home"])
    inside = state_home / "some-project"
    inside.mkdir(parents=True, exist_ok=True)
    response = client.post("/api/projects/bind", json={"path": str(inside)})
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("bad", ["   ", "\t"])
def test_b4_blank_path_is_refused(client: TestClient, bad: str) -> None:
    """空白路径必须 400。

    空白会被 `validate_project_root` 当成"空"而拒绝 —— 但如果哪天它被当成
    当前目录，就会静默绑到 API 的工作目录上。这条把它钉死。
    """
    assert client.post("/api/projects/bind", json={"path": bad}).status_code in (400, 422)


def test_b4_relative_path_is_refused(client: TestClient) -> None:
    """相对路径必须拒：同一个工程会因 cwd 不同得到不同 key。"""
    response = client.post("/api/projects/bind", json={"path": "some/relative/dir"})
    assert response.status_code == 400, response.text
    assert "绝对路径" in response.json()["detail"]


def test_b3_binding_a_real_directory_succeeds_and_is_honest_about_restart(
    client: TestClient, target_project
) -> None:
    """正向：绑一个真实目录 → 成功、成为当前、并**如实说要不要重启**。

    `restart_required` 是这个端点的诚实性所在：绑定写的是 `active.json`，
    而当前进程的库连接是启动时定下的。不报这一项，用户会以为点了就生效。
    """
    response = client.post("/api/projects/bind", json={"path": str(target_project)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "mounted"
    assert body["project"]["active"] is True
    assert body["project"]["root_exists"] is True
    assert isinstance(body["restart_required"], bool)
    assert body["message"]

    # 清单里立刻能看到它，而且它就是当前工程
    listed = client.get("/api/projects").json()
    assert listed["active_key"] == body["project"]["key"]
    keys = [item["key"] for item in listed["projects"]]
    assert body["project"]["key"] in keys


def test_b3_restart_required_is_true_when_the_key_differs(
    client: TestClient, target_project
) -> None:
    """绑一个**和启动时不同**的工程时，`restart_required` 必须是真。

    测试进程启动时通常是未绑定的，所以这里绑一个真实目录必然"与当前不同"。
    """
    body = client.post("/api/projects/bind", json={"path": str(target_project)}).json()
    assert body["restart_required"] is True
    assert body["warnings"], "与当前进程不同的绑定必须给出提醒（至少要说明要重启）"
    assert any("重启" in w for w in body["warnings"])
