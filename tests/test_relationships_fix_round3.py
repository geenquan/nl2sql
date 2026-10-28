"""M3 最小修补回归：跨 Schema 重验切换目标关系（修复轮 3）。

缺陷：build_candidate_for_mapping 匹配外键未比较 ref_schema——外键改指
archive.guest 后，请求重验 app.booking.guest_id→app.guest.id 会重建出指向
archive 的另一条关系并被保存为 validated，运行仍报告原请求成功。
修复：外键有效目标 Schema（ref_schema 有值用之，否则回退来源表 schema）
必须与请求一致；重绑后增加身份不变量防御检查（不一致→固定安全错误终止，
不探查、不保存）。全部离线：临时 SQLite + 模拟连接器 + 虚构数据。
"""

from typing import Any

import pytest

from app.adapters.repositories.sqlite_metadata_repository import SQLiteMetadataRepository
from app.relationships.discovery import RelationshipDiscoveryService
from app.services.relationship_service import RelationshipService
from tests.relationship_fixtures import (
    SCHEMA,
    FakeM3Connector,
    FakeRelationshipProbe,
    seed_catalog,
    seed_profiles,
    seed_semantics,
    synthetic_catalog,
)
from tests.test_relationships_service import FakeCredentialStore, _settings

ARCHIVE = "archive"


def _table(name: str, schema: str = SCHEMA, columns=None, primary_key=("id",),
           foreign_keys=None) -> dict[str, Any]:
    return {
        "table_id": f"tbl-{schema}-{name}", "source_id": "src-r3",
        "snapshot_id": "snap-r3", "schema_name": schema, "table_name": name,
        "table_comment": None,
        "columns": columns if columns is not None else [
            {"name": "id", "data_type": "int", "nullable": False},
        ],
        "primary_key": list(primary_key), "foreign_keys": foreign_keys or [],
        "indexes": [], "row_count_estimate": 10, "row_count_is_estimate": True,
    }


def _fk(ref_schema=None) -> dict[str, Any]:
    fk = {"name": "fk_booking_guest", "columns": ["guest_id"],
          "ref_table": "guest", "ref_columns": ["id"]}
    if ref_schema is not None:
        fk["ref_schema"] = ref_schema
    return fk


def _booking(fk) -> dict[str, Any]:
    return _table(
        "booking", foreign_keys=[fk],
        columns=[
            {"name": "id", "data_type": "int", "nullable": False},
            {"name": "guest_id", "data_type": "int", "nullable": True},
        ],
        primary_key=("id",),
    )


class TestForeignKeySchemaMatching:
    """单元级：外键目标 Schema 匹配（app.guest 与 archive.guest 并存）。"""

    def _build(self, fk, target_schema=SCHEMA):
        return RelationshipDiscoveryService().build_candidate_for_mapping(
            [_booking(fk), _table("guest", SCHEMA), _table("guest", ARCHIVE)],
            profiles={}, semantics=[],
            source_schema=SCHEMA, source_table="booking",
            source_columns=["guest_id"],
            target_schema=target_schema, target_table="guest",
            target_columns=["id"],
        )

    def test_fk_pointing_to_app_matches(self):
        candidate = self._build(_fk(ref_schema=SCHEMA))
        assert candidate is not None
        assert candidate.origin == ["explicit_fk"]
        assert candidate.target_table["schema_name"] == SCHEMA

    def test_fk_repointed_to_archive_not_matched(self):
        """外键改指 archive.guest：不得作为 app 映射的 explicit_fk 依据，
        走非外键重建流程（不放宽验证门槛）。"""
        candidate = self._build(_fk(ref_schema=ARCHIVE))
        assert candidate is not None
        assert "explicit_fk" not in candidate.origin  # 无匹配外键 → 非外键路径
        assert candidate.target_table["schema_name"] == SCHEMA  # 目标仍是请求的 app.guest

    def test_missing_ref_schema_falls_back_to_source_schema(self):
        """ref_schema 缺失：回退来源表 schema（app）→ 正常匹配。"""
        candidate = self._build(_fk(ref_schema=None))
        assert candidate is not None
        assert candidate.origin == ["explicit_fk"]

    def test_archive_target_request_uses_archive_fk(self):
        """请求本身指向 archive 时，archive 外键才是其 explicit_fk 依据。"""
        candidate = RelationshipDiscoveryService().build_candidate_for_mapping(
            [_booking(_fk(ref_schema=ARCHIVE)),
             _table("guest", SCHEMA), _table("guest", ARCHIVE)],
            profiles={}, semantics=[],
            source_schema=SCHEMA, source_table="booking",
            source_columns=["guest_id"],
            target_schema=ARCHIVE, target_table="guest",
            target_columns=["id"],
        )
        assert candidate is not None
        assert candidate.origin == ["explicit_fk"]
        assert candidate.target_table["schema_name"] == ARCHIVE


# -- 服务层 revalidate -----------------------------------------------------------


def _new_env(tmp_path):
    repo = SQLiteMetadataRepository(f"sqlite:///{tmp_path / 'meta.db'}")
    source_id = repo.create_source({
        "name": "default", "dialect": "mysql", "host": "127.0.0.1", "port": 3306,
        "database": "app", "username": "ro", "credential_ref": "cred-1",
        "allowed_schemas": [SCHEMA], "read_only": True, "is_default": True,
    })
    catalog = synthetic_catalog(source_id=source_id)
    seed_catalog(repo, catalog, source_id=source_id)
    seed_profiles(repo, catalog)
    seed_semantics(repo, catalog)
    return repo, source_id, catalog


def _make_service(repo, source_id, catalog, *, settings=None, probe=None,
                  discovery=None):
    def connector_factory(params):
        return FakeM3Connector(catalog,
                               statement_counter=params.get("statement_counter"))

    def probe_factory(params):
        target = probe if probe is not None else FakeRelationshipProbe()
        target._counter = params.get("statement_counter")
        return target

    return RelationshipService(
        metadata_repository=repo, source_repository=repo,
        credential_store=FakeCredentialStore({"cred-1": "secret"}),
        connector_factory=connector_factory, probe_factory=probe_factory,
        settings=settings or _settings(), discovery=discovery,
    )


def _seed_repointed_snapshot(repo, source_id):
    """snap-2：外键改指 archive.guest；archive.guest 表存在；app.guest 去掉
    主键且不种语义（使 app 链不被常规召回，走重绑路径）。"""

    def mutate(catalog):
        booking = next(t for t in catalog if t["table_name"] == "booking")
        for fk in booking["foreign_keys"]:
            if fk.get("name") == "fk_booking_guest":
                fk["ref_schema"] = ARCHIVE
        guest = next(t for t in catalog if t["table_name"] == "guest")
        guest["primary_key"] = []  # app 链不被信号召回 → 触发重绑
        catalog.append({  # 同名表存在于其他 Schema
            "table_id": "snap-2-archive-guest", "source_id": source_id,
            "snapshot_id": "snap-2", "schema_name": ARCHIVE,
            "table_name": "guest", "table_comment": None,
            "columns": [{"name": "id", "data_type": "int", "nullable": False}],
            "primary_key": ["id"], "foreign_keys": [], "indexes": [],
            "row_count_estimate": 5, "row_count_is_estimate": True,
        })

    catalog = synthetic_catalog(source_id=source_id, snapshot_id="snap-2")
    for table in catalog:
        table["table_id"] = f"snap-2-{table['table_name']}"
    mutate(catalog)
    seed_catalog(repo, catalog, source_id=source_id, snapshot_id="snap-2",
                 started_at="2026-10-28T03:00:00+00:00")
    seed_profiles(repo, catalog)
    return catalog


class TestRevalidateAcrossSchema:
    def test_repointed_fk_keeps_requested_identity(self, tmp_path):
        """服务层：外键改指 archive 后重验——请求关系身份不变、不继承
        explicit_fk、不新增/验证另一条关系、旧快照历史不变。"""
        repo, source_id, catalog = _new_env(tmp_path)
        service = _make_service(
            repo, source_id, catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        service.validate_batch(source_id, limit=12)
        key = next(
            r["relationship_key"]
            for r in service.list_relationships(source_id, status="validated")
            if r["source_columns"] == ["guest_id"] and r["target_table"] == "guest")
        history_before = [
            (r["revision"], r["snapshot_id"], r["status"])
            for r in repo.get_relationship_history(key)]

        new_catalog = _seed_repointed_snapshot(repo, source_id)
        service = _make_service(
            repo, source_id, new_catalog,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        run = service.revalidate(source_id, key)
        assert run["status"] in ("succeeded", "partial")

        # 请求关系的完整身份保持不变（仍是 app.booking.guest_id → app.guest.id）
        record = repo.get_effective_relationship(key, snapshot_id="snap-2")
        assert record is not None
        assert (record["source_schema"], record["source_table"],
                tuple(record["source_columns"])) == (SCHEMA, "booking", ("guest_id",))
        assert (record["target_schema"], record["target_table"],
                tuple(record["target_columns"])) == (SCHEMA, "guest", ("id",))
        # 不错误继承 explicit_fk（目标快照无指向 app.guest 的外键）
        assert "explicit_fk" not in record["origin"]
        # 不验证另一条关系：任何 validated 记录都不是指向 archive 的
        for validated in repo.list_relationships(
                source_id, snapshot_id="snap-2", status="validated"):
            assert validated["target_schema"] != ARCHIVE
        # 旧快照历史保持不变（追加不覆盖）
        history_after = [
            (r["revision"], r["snapshot_id"], r["status"])
            for r in repo.get_relationship_history(key)]
        assert set(history_before) <= set(history_after)

    def test_identity_mismatch_aborts_without_probe_or_save(self, tmp_path):
        """注入返回错误目标（archive）的候选：固定安全错误终止——不探查、
        不保存另一条关系、旧历史不变。"""

        class WrongTargetDiscovery(RelationshipDiscoveryService):
            def build_candidate_for_mapping(self, metadata, profiles, semantics,
                                            **kwargs):
                return super().build_candidate_for_mapping(
                    metadata, profiles, semantics,
                    source_schema=kwargs["source_schema"],
                    source_table=kwargs["source_table"],
                    source_columns=kwargs["source_columns"],
                    target_schema=ARCHIVE,  # 注入：指向错误 Schema
                    target_table=kwargs["target_table"],
                    target_columns=kwargs["target_columns"])

        repo, source_id, catalog = _new_env(tmp_path)
        probe = FakeRelationshipProbe()
        service = _make_service(
            repo, source_id, catalog, probe=probe,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        service.validate_batch(source_id, limit=12)
        key = next(
            r["relationship_key"]
            for r in service.list_relationships(source_id, status="validated")
            if r["source_columns"] == ["guest_id"] and r["target_table"] == "guest")
        new_catalog = _seed_repointed_snapshot(repo, source_id)
        poisoned = _make_service(
            repo, source_id, new_catalog, probe=probe,
            settings=_settings(relationship_validation_max_candidates=15),
            discovery=WrongTargetDiscovery())
        poisoned.discover_relationships(source_id)
        # 基线在发现之后、重验之前：重验本身不得写入任何修订
        revisions_before = {
            (r["relationship_key"], r["revision"])
            for r in repo.list_relationships(source_id, effective_only=False)
        }
        probe.window_requests.clear()
        with pytest.raises(ValueError, match="内部一致性校验失败"):
            poisoned.revalidate(source_id, key)
        # 不探查、不保存：探查未触发；仓储修订集合不变（含 archive 关系）
        assert probe.window_requests == []
        assert {
            (r["relationship_key"], r["revision"])
            for r in repo.list_relationships(source_id, effective_only=False)
        } == revisions_before

    def test_identity_mismatch_on_wrong_relationship_key(self, tmp_path):
        """注入篡改关系键的候选：同样被不变量拦截。"""

        class WrongKeyDiscovery(RelationshipDiscoveryService):
            def build_candidate_for_mapping(self, *args, **kwargs):
                candidate = super().build_candidate_for_mapping(*args, **kwargs)
                if candidate is not None:
                    candidate.relationship_key = "deadbeef"  # 注入：错误关系键
                return candidate

        repo, source_id, catalog = _new_env(tmp_path)
        probe = FakeRelationshipProbe()
        service = _make_service(
            repo, source_id, catalog, probe=probe,
            settings=_settings(relationship_validation_max_candidates=15))
        service.discover_relationships(source_id)
        service.validate_batch(source_id, limit=12)
        key = next(
            r["relationship_key"]
            for r in service.list_relationships(source_id, status="validated")
            if r["source_columns"] == ["guest_id"] and r["target_table"] == "guest")
        new_catalog = _seed_repointed_snapshot(repo, source_id)
        poisoned = _make_service(
            repo, source_id, new_catalog, probe=probe,
            settings=_settings(relationship_validation_max_candidates=15),
            discovery=WrongKeyDiscovery())
        poisoned.discover_relationships(source_id)
        probe.window_requests.clear()
        with pytest.raises(ValueError, match="内部一致性校验失败"):
            poisoned.revalidate(source_id, key)
        assert probe.window_requests == []
