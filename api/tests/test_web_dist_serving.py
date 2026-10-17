"""P6-1 的**运行期**那一半：API 真的在托管前端产物吗？

## 为什么需要这一条

容器里的"界面可用"= 镜像里 `pnpm build` 的产物被 API 托管（`main._resolve_web_dist()` 找到
`web/dist/index.html` → `StaticFiles(html=True)` 挂在 `/`）。而 `test_container.py` 的 21 条
**全是静态判据**（Dockerfile / compose 的文本），没有任何一条真的去 `GET /`。

静态判据能证明"产物落点与探测路径一致"，证明不了"挂载真的生效、API 路由没被吃掉、
未知路径会回落到 SPA"。这三件事都只有跑一次 HTTP 才能看见 —— 而它们正是"界面打不开"的
真实故障形态（`_resolve_web_dist` 返回 None 时 API 只是**静默**不托管界面）。

## 为什么可能 skip（而不是假装通过）

`verify_all` 的顺序是 **先 pytest、后 `pnpm build`**，所以在干净 clone（CI）里跑 pytest 时
`web/dist` 还不存在 —— 那时 API 的确不托管界面。跳过是**诚实**的（"这次没验"），
而不是把"没有产物"判成"托管坏了"。本机跑过 build，所以这里会真的验。
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[2]
DIST_INDEX = ROOT / "web" / "dist" / "index.html"


@pytest.fixture(scope="module")
def client() -> TestClient:
    if not DIST_INDEX.is_file():
        pytest.skip("web/dist 不存在（干净 clone 里 pytest 在 pnpm build 之前跑）—— 这条这次没验")
    from app.main import app

    return TestClient(app)


def test_w1_the_spa_is_served_at_the_root(client: TestClient) -> None:
    """`GET /` 要给 index.html，而不是 404 —— 这就是容器里"界面可用"的那一步。"""

    response = client.get("/")
    assert response.status_code == 200, response.text
    assert 'id="root"' in response.text, "根路径返回的不是这份前端（找不到挂载点 #root）"
    assert "<script" in response.text, "index.html 里没有脚本引用 —— 那不是构建产物"


def test_w2_the_api_routes_are_not_shadowed_by_the_mount(client: TestClient) -> None:
    """挂载在 `/` 不能把 `/api/*` 吃掉（Starlette 按注册顺序匹配，先挂根就会）。"""

    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    assert health.json().get("status") == "ok"


def test_w3_assets_resolve_and_are_not_html(client: TestClient) -> None:
    """index.html 引用的静态资源必须真的取得到（否则界面白屏，而 curl / 仍然是 200）。"""

    html = DIST_INDEX.read_text(encoding="utf-8")
    references = re.findall(r'(?:src|href)="([^"]+)"', html)
    assets = [item for item in references if item.startswith("/assets/")]
    assert assets, f"index.html 里没有 /assets/ 引用：{references[:5]}"

    for asset in assets[:4]:
        response = client.get(asset)
        assert response.status_code == 200, f"{asset} 取不到：{response.status_code}"
        content_type = response.headers.get("content-type", "")
        assert "text/html" not in content_type, f"{asset} 被当成 HTML 返回（{content_type}）"


def test_w4_unknown_paths_are_404_and_that_is_the_documented_boundary(client: TestClient) -> None:
    """未知路径是 **404**，不是回落到首页 —— 这是刻意记录的边界。

    我原本以为 `StaticFiles(html=True)` 会把未知路径回落到 `index.html`（那是很多 SPA 部署的
    做法）—— **实测不是**：`html=True` 只对"目录路径"给 `index.html`（所以 `/` 能开），
    未知路径照样 404。而 `main.py` 的注释也写明了："这个前端没有路由库（单页、无深链接），
    所以不需要更复杂的 SPA 回退逻辑"。

    所以这里断言的**就是这个 404**：它把"没有深链接"这件事变成可执行的判据 ——
    将来谁加了路由库，这条会红，提醒他同时补回落规则。
    """

    response = client.get("/some/deep/link")
    assert response.status_code == 404, (
        "未知路径不再是 404 —— 要么加了路由库（那就要同时补 SPA 回落），要么挂载方式变了"
    )
