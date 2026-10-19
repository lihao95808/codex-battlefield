"""The real probe must not mistake one scheduler pass for a completed task."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def driver():
    source = Path(__file__).resolve().parents[2] / "scripts" / "real_scene3d_probe.py"
    spec = importlib.util.spec_from_file_location("scene3d_probe_driver", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_deferred_product_task_is_awaited_before_probe_finishes(driver):
    state = {"status": "waiting_children"}
    class Product:
        _background_tasks = set()
        settings = SimpleNamespace(scheduler_deferred_retry_seconds=0.001)
        def __init__(self):
            self._deferred_parent_tasks = {}
            self.calls = 0
        async def run_tree(self, root_id, runner_mode):
            self.calls += 1
            async def deferred():
                await asyncio.sleep(0.01)
                state["status"] = "done"
            self._deferred_parent_tasks[root_id] = asyncio.create_task(deferred())
    product = Product()
    await asyncio.wait_for(driver.drive_product_tree(product, "root", lambda: state["status"]), timeout=1)
    assert state["status"] == "done"
    assert product.calls == 1


@pytest.mark.asyncio
async def test_queued_return_gets_next_product_scheduling_pass(driver):
    state = {"status": "queued"}
    class Product:
        _background_tasks = set()
        _deferred_parent_tasks = {}
        settings = SimpleNamespace(scheduler_deferred_retry_seconds=0.001)
        calls = 0
        async def run_tree(self, root_id, runner_mode):
            self.calls += 1
            if self.calls == 2:
                state["status"] = "done"
    product = Product()
    await asyncio.wait_for(driver.drive_product_tree(product, "root", lambda: state["status"]), timeout=1)
    assert product.calls == 2 and state["status"] == "done"


@pytest.mark.asyncio
async def test_hibernation_is_not_spun_as_completion(driver):
    class Product:
        async def run_tree(self, root_id, runner_mode):
            return None
    await asyncio.wait_for(driver.drive_product_tree(Product(), "root", lambda: "hibernating"), timeout=1)
