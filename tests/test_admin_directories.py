"""Administrator directory list and folder creation, on a real filesystem and DB.

Fixtures (a tmp shared folder with HELLO and 사업B, ingested, READY, an admin
and an ordinary reader, a client with file management on) come from the
relocation suite so both features are tested against the same world.
"""

from __future__ import annotations

import os
import stat

import pytest

from test_document_relocate import (  # noqa: F401 - fixtures are used by name
    ADMIN,
    DEBUG_HEADER,
    READER,
    client,
    conn,
    document,
    dsn,
    factory,
    relocate,
    relocate_db,
    root,
    server,
    world,
)


def directories(client, user=ADMIN):
    return client.get("/api/v1/admin/directories", headers={DEBUG_HEADER: user})


def make(client, user=ADMIN, **body):
    return client.post("/api/v1/admin/directories", json=body, headers={DEBUG_HEADER: user})


def paths(client, user=ADMIN):
    return [entry["path"] for entry in directories(client, user).json()["directories"]]


def tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


class TestDirectoryListing:
    def test_lists_real_directories_including_empty_ones(self, client, world):
        (world / "빈폴더").mkdir()
        (world / "HELLO" / "하위").mkdir()
        body = directories(client).json()["directories"]
        listed = [entry["path"] for entry in body]

        assert {"HELLO", "사업B", "빈폴더", "HELLO/하위"} <= set(listed)
        assert listed.index("HELLO") < listed.index("HELLO/하위")
        assert not any(p.endswith(".hwp") for p in listed)
        child = next(entry for entry in body if entry["path"] == "HELLO/하위")
        assert child == {"path": "HELLO/하위", "name": "하위", "depth": 2}

    def test_never_exposes_the_host_path(self, client, world):
        assert str(world) not in directories(client).text

    def test_symlinks_are_neither_listed_nor_followed(self, client, world, tmp_path):
        outside = tmp_path / "outside"
        (outside / "비밀").mkdir(parents=True)
        (world / "탈출").symlink_to(outside, target_is_directory=True)
        (world / "내부링크").symlink_to(world / "HELLO", target_is_directory=True)
        listed = paths(client)
        assert "탈출" not in listed and "내부링크" not in listed
        assert not any("비밀" in p for p in listed)

    def test_a_legacy_cp949_directory_is_listed_canonically(self, client, world):
        (world / os.fsdecode("레거시".encode("cp949"))).mkdir()
        entry = next(e for e in directories(client).json()["directories"] if e["path"].startswith("%"))
        assert entry["name"] == "레거시"

    def test_an_ordinary_user_is_refused(self, client, world):
        assert directories(client, READER).status_code == 403
        assert make(client, READER, parent_path="", name="막힘").status_code == 403
        assert not (world / "막힘").exists()

    def test_nothing_is_available_while_the_feature_is_off(self, client, world, monkeypatch):
        monkeypatch.setenv("DOCUMENT_FILE_MANAGEMENT_ENABLED", "false")
        assert directories(client).status_code == 503
        response = make(client, parent_path="", name="꺼짐")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "FEATURE_UNAVAILABLE"
        assert not (world / "꺼짐").exists()


class TestCreateDirectory:
    def test_creates_at_the_top_level(self, client, world):
        response = make(client, parent_path="", name="2026_보고서")
        assert response.status_code == 201, response.text
        assert response.json() == {"path": "2026_보고서", "name": "2026_보고서", "depth": 1}
        assert (world / "2026_보고서").is_dir()

    def test_creates_under_an_existing_folder(self, client, world):
        response = make(client, parent_path="HELLO", name="2026_보고서")
        assert response.status_code == 201, response.text
        assert response.json()["path"] == "HELLO/2026_보고서"
        assert (world / "HELLO" / "2026_보고서").is_dir()

    def test_is_listed_for_admins_at_once_but_not_in_folders(self, client, world):
        make(client, parent_path="HELLO", name="2026_보고서")
        assert "HELLO/2026_보고서" in paths(client)
        for user in (ADMIN, READER):
            folders = client.get("/api/v1/folders", headers={DEBUG_HEADER: user}).json()
            assert "HELLO/2026_보고서" not in [item["path"] for item in folders["items"]]

    def test_an_existing_folder_or_file_is_never_merged_or_replaced(self, client, world):
        assert make(client, parent_path="HELLO", name="중복").status_code == 201
        again = make(client, parent_path="HELLO", name="중복")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "FOLDER_ALREADY_EXISTS"

        (world / "HELLO" / "파일이름").write_bytes(b"FILE")
        clash = make(client, parent_path="HELLO", name="파일이름")
        assert clash.status_code == 409
        assert (world / "HELLO" / "파일이름").read_bytes() == b"FILE"

    @pytest.mark.parametrize("name", ["", "   ", ".", "..", "a/b", "a\\b", "\x00x", "x\x01y", "/etc"])
    def test_a_bad_name_is_refused(self, client, world, name):
        before = tree(world)
        response = make(client, parent_path="HELLO", name=name)
        assert response.status_code == 422, (name, response.text)
        assert tree(world) == before

    @pytest.mark.parametrize("parent", ["../../etc", "/etc", "HELLO/..", "..", "HELLO\\..", "없는폴더"])
    def test_a_bad_parent_is_refused(self, client, world, parent):
        before = tree(world)
        response = make(client, parent_path=parent, name="새폴더")
        assert response.status_code == 422, (parent, response.text)
        assert tree(world) == before

    def test_a_symlinked_parent_is_refused(self, client, world, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (world / "탈출").symlink_to(outside, target_is_directory=True)
        (world / "링크").symlink_to(world / "HELLO", target_is_directory=True)
        assert make(client, parent_path="탈출", name="새폴더").status_code == 422
        assert make(client, parent_path="링크", name="새폴더").status_code == 422
        assert list(outside.iterdir()) == []
        assert not (world / "HELLO" / "새폴더").exists()

    def test_no_intermediate_folder_is_created(self, client, world):
        response = make(client, parent_path="새상위", name="하위")
        assert response.status_code == 422
        assert not (world / "새상위").exists()

    def test_the_new_folder_takes_its_parents_group_and_permissions(self, client, world):
        os.chmod(world / "HELLO", 0o775)
        make(client, parent_path="HELLO", name="권한확인")
        parent, created = (world / "HELLO").stat(), (world / "HELLO" / "권한확인").stat()
        assert stat.S_IMODE(created.st_mode) == stat.S_IMODE(parent.st_mode)
        assert created.st_gid == parent.st_gid

    def test_an_unknown_body_field_is_refused(self, client, world):
        response = make(client, parent_path="", name="x", path="/etc")
        assert response.status_code == 422
        assert not (world / "x").exists()

    def test_creation_is_audited_with_the_relative_path(self, client, conn, world):
        make(client, parent_path="HELLO", name="2026_보고서")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT actor_user_id::text, target_type, target_id, metadata "
                "FROM audit_logs WHERE action = 'FOLDER_CREATED'"
            )
            rows = cur.fetchall()
        assert rows == [(ADMIN, "FOLDER", None, {"path": "HELLO/2026_보고서"})]


class TestEmptyFolderThenMove:
    def test_a_document_moved_into_a_new_empty_folder_then_appears_in_folders(
        self, client, conn, world,
    ):
        assert make(client, parent_path="", name="새빈폴더").status_code == 201
        before = client.get("/api/v1/folders", headers={DEBUG_HEADER: READER}).json()
        assert "새빈폴더" not in [item["path"] for item in before["items"]]

        doc = document(conn, "완료보고서")
        moved = relocate(client, doc["id"], folder_path="새빈폴더")
        assert moved.status_code == 200, moved.text
        assert (world / "새빈폴더" / "완료보고서.hwp").exists()

        # The document is READY, so the folder now holds something readable.
        after = client.get("/api/v1/folders", headers={DEBUG_HEADER: READER}).json()
        assert "새빈폴더" in [item["path"] for item in after["items"]]
