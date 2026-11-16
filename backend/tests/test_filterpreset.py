"""筛选预设回归测试：字段保存、状态流转、条件解析、快照与使用计数。

运行：cd backend && .venv/bin/python -m pytest tests -q
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def create(client: TestClient, **values) -> int:
    payload = {
        "预设编号": "FILT-T001",
        "预设名称": "测试预设",
        "维度条件组": '[{"维度": "区域", "操作符": "=", "值": "华东"}, {"维度": "渠道", "操作符": "in", "值": ["线上", "APP"]}]',
        "时间范围": "2026-09-01~2026-09-30",
        "排序规则": "日期升序",
        "显示列": "日期,区域,指标值",
        "共享状态": "团队共享",
    }
    payload.update(values)
    response = client.post("/api/filterpreset", json={"values": payload})
    body = response.json()
    assert body["ok"], body
    return int(body["entry"]["id"])


def publish(client: TestClient, entry_id: int) -> None:
    assert client.post(f"/api/filterpreset/{entry_id}/actions", json={"values": {"action": "提交预设评审"}}).json()["ok"]
    assert client.post(f"/api/filterpreset/{entry_id}/actions", json={"values": {"action": "发布预设"}}).json()["ok"]


def test_create_keeps_all_fields(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T010", 共享状态="团队共享")
    entry = client.get(f"/api/filterpreset/{entry_id}").json()
    assert entry["时间范围"] == "2026-09-01~2026-09-30"
    assert entry["排序规则"] == "日期升序"
    assert entry["显示列"] == "日期,区域,指标值"
    assert entry["共享状态"] == "团队共享"
    assert entry["发布状态"] == "草稿"
    assert entry["status"] == "草稿"


def test_create_rejects_missing_required(client: TestClient) -> None:
    response = client.post("/api/filterpreset", json={"values": {"预设编号": "FILT-T011"}})
    body = response.json()
    assert body["ok"] is False
    assert "预设名称" in body["message"]


def test_parse_all_conditions_not_only_first(client: TestClient) -> None:
    entry_id = create(
        client,
        预设编号="FILT-T012",
        维度条件组="区域=华东;渠道=线上;城市<>北京",
    )
    detail = client.get(f"/api/filterpreset/{entry_id}").json()
    assert [item["维度"] for item in detail["解析条件"]] == ["区域", "渠道", "城市"]
    assert detail["解析条件"][1]["操作符"] == "="


def test_parse_word_operators_and_json_shapes(client: TestClient) -> None:
    entry_id = create(
        client,
        预设编号="FILT-T012B",
        维度条件组='{"conditions": [{"dimension": "区域", "operator": "in", "values": ["华东", "华南"]}, '
        '{"field": "用户类型", "operator": "not in", "value": "游客"}]}',
    )
    conditions = client.get(f"/api/filterpreset/{entry_id}").json()["解析条件"]
    assert conditions[0] == {"维度": "区域", "操作符": "in", "值": ["华东", "华南"]}
    assert conditions[1] == {"维度": "用户类型", "操作符": "not in", "值": "游客"}


def test_text_list_value_is_not_split(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T012C", 维度条件组="区域=华东;渠道 in [线上,APP];城市<>北京")
    conditions = client.get(f"/api/filterpreset/{entry_id}").json()["解析条件"]
    assert len(conditions) == 3
    assert conditions[1]["值"] == "[线上,APP]"


def test_update_cannot_change_status_directly(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T012D")
    body = client.put(f"/api/filterpreset/{entry_id}", json={"values": {"status": "已发布"}}).json()
    assert body["ok"] is False
    assert "发布状态不能直接改" in body["message"]
    assert client.get(f"/api/filterpreset/{entry_id}").json()["发布状态"] == "草稿"


def test_update_keeps_fields(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T012E")
    body = client.put(
        f"/api/filterpreset/{entry_id}",
        json={"values": {"时间范围": "近7天", "显示列": ["日期", "区域"], "显示顺序": "指标值降序"}},
    ).json()
    assert body["ok"], body
    entry = client.get(f"/api/filterpreset/{entry_id}").json()
    assert entry["时间范围"] == "近7天"
    assert entry["显示列"] == "日期,区域"
    assert entry["排序规则"] == "指标值降序"


def test_detail_matches_apply_snapshot(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T013")
    publish(client, entry_id)
    detail = client.get(f"/api/filterpreset/{entry_id}").json()
    snapshot = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "t13"}}).json()["entry"]
    assert detail["解析条件"] == snapshot["解析条件"]
    assert detail["显示列清单"] == snapshot["显示列"]


def test_apply_requires_published(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T014")
    body = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "t14"}}).json()
    assert body["ok"] is False
    assert "只有已发布预设" in body["message"]


def test_deprecated_cannot_be_applied(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T015")
    publish(client, entry_id)
    snapshot_id = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "t15"}}).json()["entry"]["id"]
    assert client.post(f"/api/filterpreset/{entry_id}/actions", json={"values": {"action": "废弃预设"}}).json()["ok"]
    assert client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "t16"}}).json()["ok"] is False
    # 旧快照仍可只读回看，但不计数
    replay = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"snapshot_id": snapshot_id}}).json()
    assert replay["ok"] is True
    assert client.get(f"/api/filterpreset/{entry_id}").json()["使用次数"] == 1


def test_usage_count_is_idempotent(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T017")
    publish(client, entry_id)
    first = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "same"}}).json()
    second = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "same"}}).json()
    assert first["ok"] and second["ok"]
    assert first["entry"]["id"] == second["entry"]["id"]
    assert client.get(f"/api/filterpreset/{entry_id}").json()["使用次数"] == 1
    client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "other"}})
    assert client.get(f"/api/filterpreset/{entry_id}").json()["使用次数"] == 2
    assert len(client.get(f"/api/filterpreset/usage?entry_id={entry_id}").json()["items"]) == 2


def test_usage_records_are_traceable(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T018")
    publish(client, entry_id)
    snapshot = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "trace"}}).json()["entry"]
    records = client.get(f"/api/filterpreset/usage?entry_id={entry_id}").json()["items"]
    assert records[0]["快照编号"] == snapshot["快照编号"]
    assert records[0]["预设版本"] == 1


def test_snapshot_marks_invalid_dimensions_and_columns(client: TestClient) -> None:
    entry_id = create(
        client,
        预设编号="FILT-T019",
        维度条件组="区域=华东;神秘维度=1",
        显示列="日期,神秘列,指标值",
    )
    publish(client, entry_id)
    snapshot = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "invalid"}}).json()["entry"]
    assert [item["维度"] for item in snapshot["失效维度"]] == ["神秘维度"]
    assert [item["维度"] for item in snapshot["有效条件"]] == ["区域"]
    assert snapshot["失效显示列"] == ["神秘列"]


def test_snapshot_blocked_without_time_range(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T020", 时间范围="")
    publish(client, entry_id)
    body = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "no-time"}}).json()
    assert body["ok"] is False
    assert "时间范围" in body["message"]


def test_dependencies_first_valid_wins(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T021", 时间范围="")
    publish(client, entry_id)
    dependencies = [
        {"kind": "时间范围", "values": {"时间范围": "乱码"}},
        {"kind": "时间范围", "values": {"时间范围": "2026-10-01~2026-10-31"}},
        {"kind": "时间范围", "values": {"时间范围": "2027-01-01~2027-01-31"}},
    ]
    body = client.post(
        f"/api/filterpreset/{entry_id}/apply",
        json={"values": {"request_id": "deps", "dependencies": dependencies}},
    ).json()
    assert body["ok"], body
    snapshot = body["entry"]
    assert snapshot["时间范围"].startswith("2026-10-01")
    adopted = [item for item in snapshot["依赖采纳"] if item["类型"] == "时间范围"]
    assert len(adopted) == 1 and adopted[0]["顺序"] == 2


def test_drilldown_consumes_snapshot(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T022")
    publish(client, entry_id)
    snapshot = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "drill"}}).json()["entry"]
    path = client.post(
        "/api/drilldown",
        json={"values": {"路径编号": "DRIL-T900", "起始维度": "区域", "下钻层级序列": "城市"}},
    ).json()["entry"]
    body = client.post(f"/api/drilldown/{path['id']}/snapshot", json={"values": {"snapshot_id": snapshot["id"]}}).json()
    assert body["ok"], body
    assert body["entry"]["筛选快照编号"] == snapshot["id"]
    assert body["entry"]["应用条件"] == snapshot["有效条件"]
    # 消费快照不回写使用次数
    assert client.get(f"/api/filterpreset/{entry_id}").json()["使用次数"] == 1
    consumed = client.get(f"/api/filterpreset/snapshots/{snapshot['id']}").json()["消费方"]
    assert [item["消费方"] for item in consumed] == ["drilldown"]


def test_deprecated_preset_is_read_only(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T023")
    publish(client, entry_id)
    client.post(f"/api/filterpreset/{entry_id}/actions", json={"values": {"action": "废弃预设"}})
    body = client.put(f"/api/filterpreset/{entry_id}", json={"values": {"预设名称": "新名字"}}).json()
    assert body["ok"] is False
    assert "只供只读引用" in body["message"]


def test_editing_published_conditions_returns_to_review(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T024")
    publish(client, entry_id)
    old_snapshot = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "t24a"}}).json()["entry"]
    assert client.get(f"/api/filterpreset/{entry_id}").json()["版本"] == 1

    body = client.put(f"/api/filterpreset/{entry_id}", json={"values": {"维度条件组": "区域=华东;渠道=线上"}}).json()
    assert body["ok"]
    assert body["entry"]["发布状态"] == "预设评审中"
    # 版本只在重新发布成功时递增，旧快照仍可只读回看
    assert client.get(f"/api/filterpreset/{entry_id}").json()["版本"] == 1
    assert client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"snapshot_id": old_snapshot["id"]}}).json()["ok"]

    publish(client, entry_id)
    entry = client.get(f"/api/filterpreset/{entry_id}").json()
    assert entry["版本"] == 2
    assert entry["使用次数"] == 0
    snapshot = client.post(f"/api/filterpreset/{entry_id}/apply", json={"values": {"request_id": "t24b"}}).json()["entry"]
    assert snapshot["预设版本"] == 2
    assert len(snapshot["解析条件"]) == 2
    # 历史使用记录仍可追溯到旧版本
    versions = {record["预设版本"] for record in client.get(f"/api/filterpreset/usage?entry_id={entry_id}").json()["items"]}
    assert versions == {1, 2}


def test_repeat_publish_does_not_bump_version(client: TestClient) -> None:
    entry_id = create(client, 预设编号="FILT-T025")
    publish(client, entry_id)
    body = client.post(f"/api/filterpreset/{entry_id}/actions", json={"values": {"action": "发布预设"}}).json()
    assert body["ok"]
    assert client.get(f"/api/filterpreset/{entry_id}").json()["版本"] == 1
