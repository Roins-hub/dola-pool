"""api-pool 选号引擎（pool.py）的移植行为验证。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pool import (
    AccountConfig,
    AccountPool,
    DailyUsage,
    ProxyAccountConfig,
    ProxyBindError,
)


def _write(path: Path, cookies: bool, proxy: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"cookies": {"sessionid": {"value": "s"}}} if cookies else {}
    if proxy:
        data["proxy"] = proxy
    path.write_text(json.dumps(data), encoding="utf-8")


def _accounts(tmp_path: Path, n: int = 3, cookies: bool = True) -> list[AccountConfig]:
    out = []
    for i in range(1, n + 1):
        name = f"acc{i}"
        p = tmp_path / "accounts" / f"{name}.json"
        _write(p, cookies)
        out.append(AccountConfig(id=name, state_file=p))
    return out


def _healthy(pool: AccountPool) -> AccountPool:
    for st in pool.states.values():
        st.status = "healthy"
    return pool


def test_logged_in_account_starts_standby(tmp_path):
    p = tmp_path / "accounts" / "acc1.json"
    _write(p, True)
    pool = AccountPool([AccountConfig(id="acc1", state_file=p)])
    row = pool.snapshot()[0]
    assert row["status"] == "standby"
    meta = pool.snapshot_meta()
    assert meta["standby_count"] == 1
    assert meta["checking_count"] == 0
    assert pool.try_assign() is None
    assert pool.has_recoverable_account() is True
    pool.mark_healthy("acc1", "uid-1")
    assert pool.snapshot()[0]["status"] == "healthy"
    assert pool.try_assign() == "acc1"


def test_empty_account_is_unsigned_not_checking(tmp_path):
    pool = AccountPool(_accounts(tmp_path, cookies=False))
    meta = pool.snapshot_meta()
    assert meta["unsigned_count"] == 3
    assert meta["checking_count"] == 0
    assert {row["status"] for row in meta["accounts"]} == {"unsigned"}


def test_daily_success_limit_zero_means_unlimited(tmp_path):
    pool = AccountPool(_accounts(tmp_path), daily_success_limit=0)
    _healthy(pool)
    assert pool.has_daily_capacity() is True
    first = pool.try_assign()
    assert first
    pool.record_success(first)
    pool.release(first)
    assert pool.try_assign()


def test_three_jobs_round_robin_three_accounts(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    _healthy(pool)
    assigned = [pool.try_assign() for _ in range(3)]
    assert assigned == ["acc1", "acc2", "acc3"]
    assert pool.try_assign() is None
    assert [pool.states[a].inflight for a in assigned] == [1, 1, 1]


def test_fourth_waits_until_release(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    _healthy(pool)
    first = [pool.try_assign() for _ in range(3)]
    assert pool.try_assign() is None
    pool.release(first[0])
    assert pool.try_assign() == "acc1"


def test_expired_acc2_is_skipped(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    _healthy(pool)
    pool.mark_expired("acc2", "login state not found")
    assigned = [pool.try_assign() for _ in range(2)]
    assert assigned == ["acc1", "acc3"]
    assert pool.try_assign() is None
    pool.release("acc1")
    assert pool.try_assign() == "acc1"


def test_max_five_open_and_daily_fairness(tmp_path):
    pool = AccountPool(_accounts(tmp_path, n=8), max_open_accounts=5)
    _healthy(pool)
    first = [pool.try_assign() for _ in range(5)]
    assert first == ["acc1", "acc2", "acc3", "acc4", "acc5"]
    assert pool.try_assign() is None
    pool.release("acc1")
    sixth = pool.try_assign()
    assert sixth == "acc6"
    assert pool.states["acc6"].successes_today == 0
    assert pool.states["acc1"].successes_today == 0


def test_disabled_account_is_skipped(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    _healthy(pool)
    pool.set_enabled("acc1", False)
    assigned = [pool.try_assign() for _ in range(2)]
    assert assigned == ["acc2", "acc3"]


def test_assign_does_not_consume_success_quota(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    _healthy(pool)
    assert pool.try_assign() == "acc1"
    row = next(item for item in pool.snapshot() if item["id"] == "acc1")
    assert row["successes_today"] == 0
    assert row["remaining_today"] == 2
    pool.record_success("acc1")
    pool.release("acc1")
    row = next(item for item in pool.snapshot() if item["id"] == "acc1")
    assert row["successes_today"] == 1
    assert row["remaining_today"] == 1


def test_daily_success_limit_skips_exhausted_account(tmp_path):
    pool = AccountPool(_accounts(tmp_path), daily_success_limit=2)
    _healthy(pool)
    pool.record_success("acc1")
    pool.record_success("acc1")
    assigned = [pool.try_assign() for _ in range(2)]
    assert "acc1" not in assigned
    assert set(assigned) == {"acc2", "acc3"}
    preview = pool.preview_route()
    blocked = {row["id"]: row["blocked"] for row in preview["candidates"]}
    assert blocked["acc1"] == "quota_exhausted"


def test_upstream_quota_marks_remaining_zero(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    _healthy(pool)
    pool.mark_day_exhausted("acc1", "今天的生成次数已经达到上限")
    assert pool.try_assign() == "acc2"
    row = next(item for item in pool.snapshot() if item["id"] == "acc1")
    assert row["remaining_today"] == 0
    assert row["successes_today"] == 2
    assert row["status"] == "cooldown"
    assert not pool.has_daily_capacity("acc1")


def test_standby_not_probed_until_needed_and_activate(tmp_path):
    pool = AccountPool(_accounts(tmp_path, n=2))
    probed: list[str] = []

    def fake_probe(state_file, *_args):
        probed.append(Path(state_file).stem)
        return True, "uid"

    assert pool.snapshot_meta()["standby_count"] == 2
    assert pool.try_assign() is None
    assert pool.probe_all(fake_probe, "JP", "1") == []
    assert probed == []
    activated = pool.activate_standby(fake_probe, "JP", "1")
    assert activated == "acc1"
    assert pool.states["acc1"].status == "healthy"
    assert pool.states["acc2"].status == "standby"
    assert probed == ["acc1"]
    assert pool.try_assign() == "acc1"
    assert pool.states["acc2"].status == "standby"
    assert pool.probe_all(fake_probe, "JP", "1") == []


def test_weighted_prefers_remaining_and_low_fail_score(tmp_path):
    pool = AccountPool(_accounts(tmp_path), isolate_shared_egress=True)
    _healthy(pool)
    pool.record_success("acc1")
    pool.record_failure("acc2", "boom")
    assert pool.try_assign() == "acc3"


def test_weight_participates_in_rank(tmp_path):
    accts = _accounts(tmp_path)
    accts[0].weight = 1
    accts[1].weight = 1
    accts[2].weight = 10
    pool = AccountPool(accts, isolate_shared_egress=True)
    _healthy(pool)
    assert pool.try_assign() == "acc3"


def test_fail_score_cap_removes_account(tmp_path):
    pool = AccountPool(_accounts(tmp_path), fail_score_cap=20)
    _healthy(pool)
    pool.record_failure("acc1", "a")
    pool.record_failure("acc1", "b")
    assert pool.states["acc1"].status == "cooldown"
    assert pool.try_assign() == "acc2"


def test_fail_score_cap_cooldown_expiry_allows_retry(tmp_path):
    import time as _t
    pool = AccountPool(_accounts(tmp_path), fail_score_cap=20)
    _healthy(pool)
    pool.record_failure("acc1", "a")
    pool.record_failure("acc1", "b")
    assert pool.states["acc1"].status == "cooldown"
    pool.states["acc1"].cooldown_until = _t.time() - 1
    assert pool.try_assign() == "acc1"
    assert pool.states["acc1"].fail_score == 0  # 冷却到期清空失败分


def test_shared_egress_allows_only_one_inflight(tmp_path):
    pool = AccountPool(_accounts(tmp_path), isolate_shared_egress=True)
    _healthy(pool)
    assert pool.try_assign() == "acc1"
    assert pool.try_assign() is None
    preview = pool.preview_route()
    blocked = {row["id"]: row["blocked"] for row in preview["candidates"]}
    assert blocked["acc2"] == "shared_egress"


def test_identical_proxy_is_same_egress(tmp_path):
    accts = _accounts(tmp_path)
    for a in accts:
        a.proxy = "socks5h://user:pass@10.0.0.8:1080"
    pool = AccountPool(accts, isolate_shared_egress=True)
    _healthy(pool)
    assert pool.try_assign() == "acc1"
    assert pool.try_assign() is None
    iso = pool.snapshot_meta()["isolation"]
    assert iso["ok"] is False
    assert iso["shared_egress"][0]["egress"].startswith("socks5h://10.0.0.8")
    assert set(iso["shared_egress"][0]["accounts"]) == {"acc1", "acc2", "acc3"}


def test_unique_proxy_allows_parallel(tmp_path):
    accts = _accounts(tmp_path)
    for i, a in enumerate(accts, 1):
        a.proxy = f"socks5h://127.0.0.{i}:1080"
    pool = AccountPool(accts, isolate_shared_egress=True)
    _healthy(pool)
    assert [pool.try_assign() for _ in range(3)] == ["acc1", "acc2", "acc3"]


def test_egress_unhealthy_blocks_assign_allows_other_egress(tmp_path):
    accts = _accounts(tmp_path)
    for i, a in enumerate(accts, 1):
        a.proxy = f"socks5h://10.0.0.{i}:1080"
    pool = AccountPool(accts, isolate_shared_egress=True, account_proxy_enabled=True)
    _healthy(pool)
    pool.mark_egress_unhealthy(pool.effective_egress("acc1"), "proxy down", seconds=120)
    assert pool.try_assign() == "acc2"
    assert pool.has_assignable_other_egress("acc1") is True


def test_min_submit_interval_blocks_same_account(tmp_path):
    pool = AccountPool(_accounts(tmp_path), min_submit_interval_seconds=30)
    _healthy(pool)
    assert pool.try_assign() == "acc1"
    pool.release("acc1")
    assert pool.try_assign() == "acc2"


def test_proxy_persists_on_state_json(tmp_path):
    p = tmp_path / "accounts" / "acc1.json"
    _write(p, True)
    pool = AccountPool([AccountConfig(id="acc1", state_file=p)])
    pool.set_proxy("acc1", "socks5h://user:secret@1.2.3.4:1080")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["proxy"] == "socks5h://user:secret@1.2.3.4:1080"


def test_rebalance_evenly_distributes_idle_accounts(tmp_path):
    accts = _accounts(tmp_path, n=6)
    proxies = [
        ProxyAccountConfig(id=f"proxy{i}", proxy=f"http://10.0.0.{i}:8000") for i in range(1, 4)
    ]
    pool = AccountPool(accts, proxy_accounts=proxies)
    _healthy(pool)
    for st in pool.states.values():
        st.proxy_account_id = "proxy1"
        st.config.proxy = proxies[0].proxy
    result = pool.rebalance_proxy_bindings()
    assert result["moved_count"] == 4
    assert result["busy_count"] == 0
    assert result["distribution"] == {"proxy1": 2, "proxy2": 2, "proxy3": 2}
    assert {st.proxy_account_id for st in pool.states.values()} == {"proxy1", "proxy2", "proxy3"}


def test_rebalance_keeps_busy_accounts(tmp_path):
    accts = _accounts(tmp_path, n=4)
    proxies = [
        ProxyAccountConfig(id="proxy1", proxy="http://10.0.0.1:8000"),
        ProxyAccountConfig(id="proxy2", proxy="http://10.0.0.2:8000"),
    ]
    pool = AccountPool(accts, proxy_accounts=proxies)
    _healthy(pool)
    for account_id in ("acc1", "acc2"):
        pool.states[account_id].proxy_account_id = "proxy1"
        pool.states[account_id].config.proxy = proxies[0].proxy
    pool.states["acc1"].inflight = 1
    pool._working.add("acc2")
    result = pool.rebalance_proxy_bindings()
    assert result["busy_count"] == 2
    assert pool.states["acc1"].proxy_account_id == "proxy1"
    assert pool.states["acc2"].proxy_account_id == "proxy1"
    assert result["distribution"] == {"proxy1": 2, "proxy2": 2}


def test_rebalance_requires_proxy_accounts(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    with pytest.raises(ProxyBindError) as exc:
        pool.rebalance_proxy_bindings()
    assert exc.value.code == "proxy_pool_empty"


def test_bind_proxies_sticky_and_short_and_duplicate(tmp_path):
    pool = AccountPool(_accounts(tmp_path), isolate_shared_egress=True)
    proxies = [
        "socks5h://u:p@10.0.0.1:1080",
        "socks5h://u:p@10.0.0.2:1080",
        "socks5h://u:p@10.0.0.3:1080",
    ]
    result = pool.bind_proxies(proxies, force=False, mode="sticky")
    assert result["ok"] is True
    assert [item["id"] for item in result["bound"]] == ["acc1", "acc2", "acc3"]
    assert result["isolation"]["ok"] is True
    # 数量不足且非 force => 报 proxy_pool_short
    with pytest.raises(ProxyBindError) as exc:
        pool.bind_proxies(["socks5h://u:p@10.0.0.8:1080"], force=False, mode="sticky")
    assert exc.value.code == "proxy_pool_short"
    # 重复出口
    with pytest.raises(ProxyBindError) as exc:
        pool.bind_proxies(
            [
                "socks5h://a:b@127.0.0.1:7897",
                "socks5h://c:d@127.0.0.1:7897",
                "socks5h://e:f@10.0.0.3:1080",
            ],
            mode="sticky",
        )
    assert exc.value.code == "duplicate_proxy"
    # 不支持的模式
    with pytest.raises(ProxyBindError) as exc:
        pool.bind_proxies(["socks5h://10.0.0.1:1080"], mode="rotate")
    assert exc.value.code == "unsupported_mode"


def test_preview_route_does_not_assign(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    _healthy(pool)
    preview = pool.preview_route()
    assert preview["next_account_id"] == "acc1"
    assert pool.states["acc1"].inflight == 0
    pool.try_assign()
    preview = pool.preview_route()
    assert preview["open_ids"] == ["acc1"]
    assert preview["next_account_id"] == "acc2"


def test_preferred_account_pins_routing(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    _healthy(pool)
    pool.set_preferred("acc2")
    assert pool.try_assign() == "acc2"
    assert pool.try_assign() is None
    pool.release("acc2")
    assert pool.try_assign("acc3") == "acc3"
    pool.release("acc3")
    preview = pool.preview_route()
    assert preview["strategy"] == "pinned"
    assert preview["preferred_id"] == "acc2"
    assert preview["next_account_id"] == "acc2"


def test_preferred_unavailable_does_not_silently_reassign(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    _healthy(pool)
    pool.set_preferred("acc2")
    pool.mark_expired("acc2", "login state not found")
    assert pool.try_assign() is None
    pool.mark_healthy("acc2")
    pool.record_success("acc2")
    pool.record_success("acc2")
    assert pool.try_assign() is None


def test_reconcile_ghost_inflight(tmp_path):
    pool = AccountPool(_accounts(tmp_path), isolate_shared_egress=True)
    _healthy(pool)
    assert pool.try_assign() == "acc1"
    released = pool.reconcile_inflight(set(), grace_seconds=0)
    assert released == ["acc1"]
    assigned = pool.try_assign()
    assert assigned in {"acc1", "acc2", "acc3"}


def test_reconcile_keeps_inflight_when_busy(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    _healthy(pool)
    assert pool.try_assign() == "acc1"
    assert pool.reconcile_inflight({"acc1"}, grace_seconds=0) == []
    assert pool.states["acc1"].inflight == 1


def test_reconcile_respects_grace_window(tmp_path):
    pool = AccountPool(_accounts(tmp_path))
    _healthy(pool)
    assert pool.try_assign() == "acc1"
    assert pool.reconcile_inflight(set()) == []
    assert pool.states["acc1"].inflight == 1
