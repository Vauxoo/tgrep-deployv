import contextlib
import hashlib
import io
import json
import logging
import os
import tarfile
import zipfile

import pytest

from tgrep_deployv import indexer


def _messages(caplog):
    return "\n".join(record.getMessage() for record in caplog.records)


def test_render_ignore_includes_core_and_modules():
    content = indexer.render_ignore(["extra_addons/vauxoo/sale_extended"])
    assert "\n*\n" in content
    assert "!odoo/odoo/**/" in content
    assert "!extra_addons/" in content
    assert "!extra_addons/vauxoo/" in content
    assert "!extra_addons/vauxoo/sale_extended/**/*.py" in content
    assert "!extra_addons/vauxoo/sale_extended/**/*.rst" in content
    assert "odoo/addons/test_*/" in content
    assert "**/static/lib/" in content
    assert "**/i18n/" in content
    # Heavy paths come last so they win over the module whitelist above them.
    assert content.index("**/i18n/") > content.index("!extra_addons/vauxoo/sale_extended/**/*.py")


def test_discover_modules_skips_tests_core_and_setup(tmp_path):
    def module(rel):
        folder = tmp_path / rel
        folder.mkdir(parents=True)
        (folder / "__manifest__.py").write_text("{'name': 'x'}")

    module("extra_addons/vauxoo/sale_extended")
    module("extra_addons/vauxoo/test_sale_extended")
    module("odoo/addons/sale")
    module("odoo/odoo/addons/base")
    module("extra_addons/oca/setup/sale_oca")
    (tmp_path / "odoo" / "odoo-bin").write_text("")
    found = indexer.discover_modules(str(tmp_path))
    assert found == ["extra_addons/vauxoo/sale_extended", "odoo/addons/sale"]


def test_write_ignore_backs_up_existing(tmp_path):
    original = tmp_path / ".ignore"
    original.write_text("old content\n")
    indexer.write_ignore(str(tmp_path), "new content\n")
    assert original.read_text() == "new content\n"
    backup = tmp_path / (".ignore" + indexer.IGNORE_BACKUP_SUFFIX)
    assert backup.read_text() == "old content\n"
    indexer.write_ignore(str(tmp_path), "newer content\n")
    assert backup.read_text() == "old content\n"


def test_list_installed_modules_script_is_a_packaged_file():
    script = indexer.list_installed_modules_script()
    # basename, not endswith("/..."): the separator is "\" on Windows.
    assert os.path.basename(indexer.LIST_INSTALLED_MODULES_PATH) == "list_installed_modules.py"
    assert os.path.isfile(indexer.LIST_INSTALLED_MODULES_PATH)
    assert "tgrep_module_path=" in script
    assert "tgrep_done=1" in script
    # Importing the module outside odoo-bin shell must not run anything.
    from tgrep_deployv import list_installed_modules

    assert callable(list_installed_modules.tgrep_list_installed_modules)


def test_installed_modules_parses_the_shell_output(monkeypatch, caplog):
    output = (
        "2026-01-01 WARNING odoo.modules: noise\n"
        "tgrep_module_path=odoo/addons/sale\n"
        "tgrep_import_error broken_addon ImportError('x')\n"
        "tgrep_module_path=extra_addons/vauxoo/a\n"
        "tgrep_done=1\n"
    )
    monkeypatch.setattr(indexer, "run_odoo_shell", lambda root, script: output)
    caplog.set_level(logging.WARNING)
    assert indexer.installed_modules("/x") == ["extra_addons/vauxoo/a", "odoo/addons/sale"]
    assert "tgrep_import_error broken_addon" in _messages(caplog)


def test_installed_modules_none_without_database(monkeypatch):
    monkeypatch.setattr(indexer, "run_odoo_shell", lambda root, script: "psycopg2.OperationalError: no db\n")
    assert indexer.installed_modules("/x") is None


def test_read_modules_file_csv_with_state(tmp_path):
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text(
        "name,state\n"
        "sale,installed\n"
        "purchase,uninstalled\n"
        "test_mail,installed\n"
        "# a comment\n"
        "\n"
        '"stock","installed"\n'
    )
    assert indexer.read_modules_file(str(modules_file)) == ["sale", "stock"]


def test_read_modules_file_csv_without_state(tmp_path):
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text("name\nsale\npurchase\n")
    assert indexer.read_modules_file(str(modules_file)) == ["purchase", "sale"]


def test_read_modules_file_plain_list(tmp_path):
    modules_file = tmp_path / "modules.txt"
    modules_file.write_text("sale\n\npurchase\nsale\n")
    assert indexer.read_modules_file(str(modules_file)) == ["purchase", "sale"]


def test_read_modules_file_accepts_exported_labels(tmp_path):
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text(
        "Display Name,Technical Name,Status\nSales,sale,Installed\nPurchase,purchase,Not Installed\n"
    )
    assert indexer.read_modules_file(str(modules_file)) == ["sale"]


def test_read_modules_file_headerless_with_state(tmp_path):
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text("sale,installed\npurchase,uninstalled\n")
    assert indexer.read_modules_file(str(modules_file)) == ["sale"]


def test_read_modules_file_empty(tmp_path):
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text("# nothing\n")
    assert indexer.read_modules_file(str(modules_file)) == []


def test_modules_from_names_maps_paths_and_reports_unknown(tmp_path):
    def module(rel):
        folder = tmp_path / rel
        folder.mkdir(parents=True)
        (folder / "__manifest__.py").write_text("{'name': 'x'}")

    module("extra_addons/vauxoo/sale_extended")
    module("extra_addons/oca/sale_extended")  # same name in two repos: keep both
    module("odoo/addons/sale")
    paths, unknown = indexer.modules_from_names(str(tmp_path), ["sale", "sale_extended", "missing_here"])
    assert paths == ["extra_addons/oca/sale_extended", "extra_addons/vauxoo/sale_extended", "odoo/addons/sale"]
    assert unknown == ["missing_here"]


def test_version_key_orders_releases():
    ordered = ["v1.0.4", "v1.1.0-rc.1", "v1.1.0-rc.2", "v1.1.0", "v1.10.0"]
    assert sorted(ordered, key=indexer.version_key) == ordered
    assert indexer.version_key("1.0.4") == indexer.version_key("v1.0.4")


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Linux", "x86_64", "tgrep-v1.0.4-x86_64-unknown-linux-musl.tar.gz"),
        ("Linux", "aarch64", "tgrep-v1.0.4-aarch64-unknown-linux-musl.tar.gz"),
        ("Darwin", "arm64", "tgrep-v1.0.4-aarch64-apple-darwin.tar.gz"),
        ("Darwin", "x86_64", "tgrep-v1.0.4-x86_64-apple-darwin.tar.gz"),
        ("Windows", "AMD64", "tgrep-v1.0.4-x86_64-pc-windows-msvc.zip"),
        ("Windows", "ARM64", "tgrep-v1.0.4-aarch64-pc-windows-msvc.zip"),
    ],
)
def test_release_asset_name_per_platform(system, machine, expected):
    assert indexer.release_asset_name("v1.0.4", system, machine) == expected


def test_release_asset_name_unsupported_platform():
    with pytest.raises(SystemExit) as error:
        indexer.release_asset_name("v1.0.4", "FreeBSD", "x86_64")
    assert "TGREP_DOWNLOAD_URL" in str(error.value)


def _fake_latest_release(monkeypatch, payload):
    """Stub the GitHub API: payload is the JSON dict or an exception to raise."""

    def urlopen(url, timeout=None):
        assert url == indexer.TGREP_LATEST_RELEASE_API
        if isinstance(payload, Exception):
            raise payload
        return contextlib.closing(io.StringIO(json.dumps(payload)))

    monkeypatch.setattr(indexer.urllib.request, "urlopen", urlopen)


def test_latest_release_tag_prefers_newer_stable(monkeypatch):
    _fake_latest_release(monkeypatch, {"tag_name": "v9.9.9", "prerelease": False})
    assert indexer.latest_release_tag() == "v9.9.9"


def test_latest_release_tag_keeps_pin_on_older_or_prerelease(monkeypatch):
    _fake_latest_release(monkeypatch, {"tag_name": "v0.1.0", "prerelease": False})
    assert indexer.latest_release_tag() is None
    _fake_latest_release(monkeypatch, {"tag_name": indexer.TGREP_PINNED_VERSION, "prerelease": False})
    assert indexer.latest_release_tag() is None
    _fake_latest_release(monkeypatch, {"tag_name": "v9.9.9", "prerelease": True})
    assert indexer.latest_release_tag() is None
    _fake_latest_release(monkeypatch, {})
    assert indexer.latest_release_tag() is None


def test_latest_release_tag_survives_network_errors(monkeypatch):
    _fake_latest_release(monkeypatch, OSError("offline"))
    assert indexer.latest_release_tag() is None
    _fake_latest_release(monkeypatch, ValueError("not json"))
    assert indexer.latest_release_tag() is None


def test_resolve_download_uses_newer_stable_over_the_pin(monkeypatch):
    monkeypatch.delenv("TGREP_DOWNLOAD_URL", raising=False)
    monkeypatch.delenv("TGREP_VERSION", raising=False)
    monkeypatch.setattr(indexer, "latest_release_tag", lambda: "v9.9.9")
    monkeypatch.setattr(indexer, "release_asset_name", lambda tag: "tgrep-%s-x86_64-unknown-linux-musl.tar.gz" % tag)
    archive, checksums, asset = indexer.resolve_download()
    assert archive == indexer.TGREP_DOWNLOAD_URL_TMPL % ("v9.9.9", asset)
    assert checksums == indexer.TGREP_DOWNLOAD_URL_TMPL % ("v9.9.9", "checksums.txt")
    assert asset == "tgrep-v9.9.9-x86_64-unknown-linux-musl.tar.gz"


def test_resolve_download_falls_back_to_the_pin(monkeypatch):
    monkeypatch.delenv("TGREP_DOWNLOAD_URL", raising=False)
    monkeypatch.delenv("TGREP_VERSION", raising=False)
    monkeypatch.setattr(indexer, "latest_release_tag", lambda: None)
    monkeypatch.setattr(indexer, "release_asset_name", lambda tag: "tgrep-%s-x.tar.gz" % tag)
    archive, _, asset = indexer.resolve_download()
    assert indexer.TGREP_PINNED_VERSION in archive
    assert asset == "tgrep-%s-x.tar.gz" % indexer.TGREP_PINNED_VERSION


def test_resolve_download_respects_version_and_url_overrides(monkeypatch):
    monkeypatch.delenv("TGREP_DOWNLOAD_URL", raising=False)
    monkeypatch.setenv("TGREP_VERSION", "1.2.3")
    monkeypatch.setattr(indexer, "latest_release_tag", lambda: pytest.fail("TGREP_VERSION must skip the API"))
    monkeypatch.setattr(indexer, "release_asset_name", lambda tag: "tgrep-%s-x.tar.gz" % tag)
    archive, _, asset = indexer.resolve_download()
    assert "/v1.2.3/tgrep-v1.2.3-x.tar.gz" in archive
    monkeypatch.setenv(
        "TGREP_DOWNLOAD_URL", "https://mirror.example.com/tgrep/tgrep-v1.0.4-x86_64-apple-darwin.tar.gz"
    )
    archive, checksums, asset = indexer.resolve_download()
    assert archive == "https://mirror.example.com/tgrep/tgrep-v1.0.4-x86_64-apple-darwin.tar.gz"
    assert checksums == "https://mirror.example.com/tgrep/checksums.txt"
    assert asset == "tgrep-v1.0.4-x86_64-apple-darwin.tar.gz"


def test_expected_checksum_reads_the_asset_line():
    text = "aaaa  tgrep-v1.0.4-aarch64-apple-darwin.tar.gz\nbbbb *tgrep-v1.0.4-x86_64-apple-darwin.tar.gz\n"
    assert indexer.expected_checksum(text, "tgrep-v1.0.4-x86_64-apple-darwin.tar.gz") == "bbbb"
    assert indexer.expected_checksum(text, "tgrep-v1.0.4-aarch64-apple-darwin.tar.gz") == "aaaa"
    assert indexer.expected_checksum(text, "missing.tar.gz") is None


def _tarball(tmp_path, name="tgrep-v1.0.4-x86_64-unknown-linux-musl.tar.gz", payload=b"#!/bin/sh\necho tgrep 1.0.4\n"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / name
    with tarfile.open(str(archive), "w:gz") as bundle:
        info = tarfile.TarInfo("./tgrep")
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
        readme = tarfile.TarInfo("./README.md")
        readme.size = 1
        bundle.addfile(readme, io.BytesIO(b"x"))
    return archive


def test_verify_checksum_accepts_and_rejects(tmp_path):
    archive = _tarball(tmp_path)
    good = hashlib.sha256(archive.read_bytes()).hexdigest()
    text = "%s  %s\n" % (good, archive.name)
    assert indexer.verify_checksum(str(archive), text, archive.name) == good
    with pytest.raises(SystemExit) as error:
        indexer.verify_checksum(str(archive), "0" * 64 + "  %s\n" % archive.name, archive.name)
    assert "sha256 mismatch" in str(error.value)
    with pytest.raises(SystemExit) as error:
        indexer.verify_checksum(str(archive), "%s  other.tar.gz\n" % good, archive.name)
    assert "does not list" in str(error.value)


def test_extract_binary_from_tarball(tmp_path):
    archive = _tarball(tmp_path)
    target = indexer.extract_binary(str(archive), str(tmp_path / "bin"))
    assert os.path.basename(target) == indexer.TGREP_BIN
    with open(target, "rb") as handler:
        assert handler.read().startswith(b"#!/bin/sh")
    assert os.access(target, os.X_OK)


def test_extract_binary_from_zip(tmp_path):
    archive = tmp_path / "tgrep-v1.0.4-x86_64-pc-windows-msvc.zip"
    with zipfile.ZipFile(str(archive), "w") as bundle:
        bundle.writestr("tgrep.exe", b"MZ fake")
    target = indexer.extract_binary(str(archive), str(tmp_path / "bin"))
    with open(target, "rb") as handler:
        assert handler.read() == b"MZ fake"


def test_extract_binary_without_executable(tmp_path):
    archive = tmp_path / "empty.tar.gz"
    with tarfile.open(str(archive), "w:gz") as bundle:
        info = tarfile.TarInfo("./README.md")
        info.size = 0
        bundle.addfile(info, io.BytesIO(b""))
    with pytest.raises(SystemExit):
        indexer.extract_binary(str(archive), str(tmp_path / "bin"))


def test_install_tgrep_downloads_verifies_and_extracts(tmp_path, monkeypatch):
    archive = _tarball(tmp_path / "release")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    urls = {
        "https://example.com/r/" + archive.name: archive.read_bytes(),
        "https://example.com/r/checksums.txt": ("%s  %s\n" % (digest, archive.name)).encode(),
    }

    def download(url, destination):
        with open(destination, "wb") as handler:
            handler.write(urls[url])
        return destination

    monkeypatch.delenv("TGREP_DOWNLOAD_URL", raising=False)
    monkeypatch.setattr(indexer, "download", download)
    monkeypatch.setattr(
        indexer,
        "resolve_download",
        lambda tag=None: (
            "https://example.com/r/" + archive.name,
            "https://example.com/r/checksums.txt",
            archive.name,
        ),
    )
    target = indexer.install_tgrep(install_dir=str(tmp_path / "bin"))
    assert os.path.isfile(target)
    assert os.access(target, os.X_OK)


def test_install_tgrep_refuses_unverified_release(tmp_path, monkeypatch):
    archive = _tarball(tmp_path / "release")

    def download(url, destination):
        if url.endswith("checksums.txt"):
            raise OSError("404")
        with open(destination, "wb") as handler:
            handler.write(archive.read_bytes())
        return destination

    monkeypatch.delenv("TGREP_DOWNLOAD_URL", raising=False)
    monkeypatch.setattr(indexer, "download", download)
    monkeypatch.setattr(
        indexer,
        "resolve_download",
        lambda tag=None: ("https://x/" + archive.name, "https://x/checksums.txt", archive.name),
    )
    with pytest.raises(SystemExit) as error:
        indexer.install_tgrep(install_dir=str(tmp_path / "bin"))
    assert "unverified" in str(error.value)


def test_install_tgrep_warns_on_mirror_without_checksums(tmp_path, monkeypatch, caplog):
    archive = _tarball(tmp_path / "release")

    def download(url, destination):
        if url.endswith("checksums.txt"):
            raise OSError("404")
        with open(destination, "wb") as handler:
            handler.write(archive.read_bytes())
        return destination

    monkeypatch.setenv("TGREP_DOWNLOAD_URL", "https://mirror/" + archive.name)
    monkeypatch.setattr(indexer, "download", download)
    caplog.set_level(logging.WARNING)
    target = indexer.install_tgrep(install_dir=str(tmp_path / "bin"))
    assert os.path.isfile(target)
    assert "unverified" in _messages(caplog)


def test_ensure_tgrep_installed_keeps_an_existing_binary(monkeypatch):
    monkeypatch.setattr(indexer, "find_tgrep", lambda: "/fake/tgrep")
    monkeypatch.setattr(indexer, "tgrep_version", lambda binary: "1.0.4")
    monkeypatch.setattr(indexer, "install_tgrep", lambda *args, **kwargs: pytest.fail("must not reinstall"))
    assert indexer.ensure_tgrep_installed() == "/fake/tgrep"


def test_ensure_tgrep_installed_installs_when_missing(monkeypatch):
    found = iter([None, "/home/odoo/.local/bin/tgrep"])
    installed = []
    monkeypatch.setattr(indexer, "find_tgrep", lambda: next(found))
    monkeypatch.setattr(indexer, "tgrep_version", lambda binary: "1.0.4")
    monkeypatch.setattr(indexer, "install_tgrep", lambda *args, **kwargs: installed.append(True))
    assert indexer.ensure_tgrep_installed() == "/home/odoo/.local/bin/tgrep"
    assert installed == [True]


def test_ensure_tgrep_installed_fails_when_still_missing(monkeypatch):
    monkeypatch.setattr(indexer, "find_tgrep", lambda: None)
    monkeypatch.setattr(indexer, "install_tgrep", lambda *args, **kwargs: None)
    with pytest.raises(SystemExit):
        indexer.ensure_tgrep_installed()


def test_tgrep_version_parses_and_survives_failures(monkeypatch):
    monkeypatch.setattr(indexer.subprocess, "check_output", lambda cmd, **kwargs: "tgrep 1.0.4\n")
    assert indexer.tgrep_version("/fake/tgrep") == "1.0.4"
    monkeypatch.setattr(indexer.subprocess, "check_output", lambda cmd, **kwargs: "")
    assert indexer.tgrep_version("/fake/tgrep") == ""

    def boom(cmd, **kwargs):
        raise OSError("missing")

    monkeypatch.setattr(indexer.subprocess, "check_output", boom)
    assert indexer.tgrep_version("/fake/tgrep") == ""


def _fake_files(monkeypatch, root, rel_paths):
    """Stub "tgrep --files <root>", which prints absolute paths for an absolute root."""
    calls = []

    def check_output(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[1:] == ["--files", root]
        return "".join("%s\n" % os.path.join(root, rel.replace("/", os.sep)) for rel in rel_paths)

    monkeypatch.setattr(indexer.subprocess, "check_output", check_output)
    return calls


def test_indexed_files_relative_to_root(monkeypatch, tmp_path):
    root = str(tmp_path)
    _fake_files(monkeypatch, root, ["odoo/addons/sale/__manifest__.py", "odoo/addons/sale/models/sale.py"])
    assert indexer.indexed_files(root) == ["odoo/addons/sale/__manifest__.py", "odoo/addons/sale/models/sale.py"]


def test_indexed_files_accepts_relative_output(monkeypatch):
    monkeypatch.setattr(indexer.subprocess, "check_output", lambda cmd, **kwargs: "a/b.py\n\n./c.py\n")
    assert indexer.indexed_files("inst") == ["a/b.py", "./c.py"]


def test_build_index_passes_force(monkeypatch):
    calls = []
    monkeypatch.setattr(indexer, "find_tgrep", lambda: "/fake/tgrep")
    monkeypatch.setattr(indexer.subprocess, "check_call", lambda cmd, **kwargs: calls.append(cmd))
    indexer.build_index("/inst")
    indexer.build_index("/inst", force=True)
    assert calls == [["/fake/tgrep", "index", "/inst"], ["/fake/tgrep", "index", "/inst", "--force"]]


def test_start_server_detaches_and_logs(monkeypatch, tmp_path):
    popen_calls = []

    class FakeProc:
        pid = 4242

    def popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return FakeProc()

    monkeypatch.setattr(indexer, "find_tgrep", lambda: "/fake/tgrep")
    monkeypatch.setattr(indexer.subprocess, "Popen", popen)
    assert indexer.start_server(str(tmp_path)) == 4242
    cmd, kwargs = popen_calls[0]
    assert cmd == ["/fake/tgrep", "serve", str(tmp_path)]
    assert kwargs["start_new_session"] is True
    assert os.path.isfile(str(tmp_path / indexer.INDEX_DIR / indexer.SERVE_LOG))


def test_indexed_manifest_files_matches_both_manifest_names():
    paths = [
        "odoo/addons/sale/__manifest__.py",
        "extra_addons/old/__openerp__.py",
        "odoo/addons/sale/models/sale.py",
        "odoo/addons/sale/__manifest__.pyc",
    ]
    assert indexer.indexed_manifest_files(paths) == [
        "odoo/addons/sale/__manifest__.py",
        "extra_addons/old/__openerp__.py",
    ]


def test_validate_scope_clean(monkeypatch, caplog, tmp_path):
    root = str(tmp_path)
    _fake_files(
        monkeypatch,
        root,
        [
            "odoo/addons/sale/__manifest__.py",
            "odoo/addons/sale/views/sale.xml",
            "extra_addons/enterprise/quality/__manifest__.py",
            # The core tree is indexed completely by design, so it is out of the comparison.
            "odoo/odoo/addons/base/__manifest__.py",
        ],
    )
    caplog.set_level(logging.INFO)
    assert indexer.validate_scope(root, ["odoo/addons/sale", "extra_addons/enterprise/quality"]) is True
    out = _messages(caplog)
    assert "missing_module_roots=0" in out
    assert "extra_indexed_module_roots=0" in out
    assert "extensions_indexed=2 files=4" in out
    assert "unexpected_extensions=0" in out


def test_validate_scope_reports_missing_and_extra(monkeypatch, caplog, tmp_path):
    root = str(tmp_path)
    _fake_files(monkeypatch, root, ["odoo/addons/sale/__manifest__.py", "odoo/addons/stock/__manifest__.py"])
    caplog.set_level(logging.INFO)
    expected = ["odoo/addons/sale", "extra_addons/enterprise/quality"]
    assert indexer.validate_scope(root, expected) is False
    out = _messages(caplog)
    assert "missing extra_addons/enterprise/quality" in out
    assert "extra odoo/addons/stock" in out


def test_validate_scope_rejects_leaked_test_addons(monkeypatch, caplog, tmp_path):
    root = str(tmp_path)
    _fake_files(
        monkeypatch,
        root,
        [
            "odoo/addons/test_mail/__manifest__.py",
            "extra_addons/vauxoo/sale/tests/__manifest__.py",
        ],
    )
    caplog.set_level(logging.INFO)
    expected = ["odoo/addons/test_mail", "extra_addons/vauxoo/sale/tests"]
    assert indexer.validate_scope(root, expected) is False
    out = _messages(caplog)
    # Only the addons/test_* module leaks; a "tests" folder inside a real module does not.
    assert "test_addon_modules_indexed=1" in out
    assert "leaked odoo/addons/test_mail" in out


def test_validate_scope_rejects_unexpected_extensions(monkeypatch, caplog, tmp_path):
    root = str(tmp_path)
    _fake_files(monkeypatch, root, ["odoo/addons/sale/__manifest__.py", "odoo/addons/sale/i18n/es.po"])
    caplog.set_level(logging.INFO)
    assert indexer.validate_scope(root, ["odoo/addons/sale"]) is False
    out = _messages(caplog)
    assert "extension .po      files=1 (!)" in out
    assert "unexpected_extensions=1 files=1" in out


def test_outermost_module_roots_drops_nested_manifests():
    """Odoo ships manifests inside real modules; they are files, not modules."""
    roots = [
        "odoo/addons/base_import_module",
        "odoo/addons/base_import_module/tests/test_module",
        "odoo/addons/point_of_sale",
        "odoo/addons/point_of_sale/tools/posbox/overwrite_after_init/home/pi/odoo/addons/point_of_sale",
        "odoo/addons/sale",
    ]
    assert indexer.outermost_module_roots(roots) == {
        "odoo/addons/base_import_module",
        "odoo/addons/point_of_sale",
        "odoo/addons/sale",
    }
    # A sibling whose path merely starts with the same characters is not nested.
    assert indexer.outermost_module_roots(["a/sale", "a/sale_stock"]) == {"a/sale", "a/sale_stock"}


def test_validate_scope_ignores_manifests_nested_in_a_module(monkeypatch, caplog, tmp_path):
    root = str(tmp_path)
    _fake_files(
        monkeypatch,
        root,
        [
            "odoo/addons/base_import_module/__manifest__.py",
            "odoo/addons/base_import_module/tests/test_module/__manifest__.py",
        ],
    )
    caplog.set_level(logging.INFO)
    assert indexer.validate_scope(root, ["odoo/addons/base_import_module"]) is True
    assert "extra_indexed_module_roots=0" in _messages(caplog)


def test_source_extensions_track_the_ignore_globs():
    assert indexer.SOURCE_EXTENSIONS == tuple(sorted(pattern[1:] for pattern in indexer.SOURCE_GLOBS))
    assert ".py" in indexer.SOURCE_EXTENSIONS and "*.py" not in indexer.SOURCE_EXTENSIONS
    # Translations are the one text format deliberately left out of a grep index.
    assert ".po" not in indexer.SOURCE_EXTENSIONS and ".pot" not in indexer.SOURCE_EXTENSIONS


def test_indexed_extensions_counts_by_path_suffix():
    paths = ["a/b.py", "a/c.py", "a/d.xml", "a/odoo-bin", "a/static/lib/x.min.js"]
    assert indexer.indexed_extensions(paths) == {".py": 2, ".xml": 1, indexer.NO_EXTENSION: 1, ".js": 1}


# --- Integration with a real tgrep binary (skipped when it is not installed) -----------------


def _fake_instance(tmp_path):
    """An instance tree with everything .ignore must keep out, plus what it must keep."""
    files = {
        "odoo/odoo-bin": "#!/usr/bin/env python3",
        "odoo/odoo/tools/misc.py": "needle core",
        "odoo/addons/sale/__manifest__.py": "{'name': 'sale'}",
        "odoo/addons/sale/models/sale.py": "needle sale",
        "odoo/addons/sale/i18n/es.po": "needle translation",
        "odoo/addons/sale/static/lib/vendor.js": "needle vendored",
        "odoo/addons/sale/static/src/js/app.min.js": "needle minified",
        "odoo/addons/sale/static/src/js/app.js": "needle app",
        "odoo/addons/test_mail/__manifest__.py": "{'name': 'test'}",
        "odoo/addons/test_mail/tests/test_it.py": "needle test addon",
        "extra_addons/vauxoo/kept/__manifest__.py": "{'name': 'kept'}",
        "extra_addons/vauxoo/kept/models/kept.py": "needle kept",
        "extra_addons/vauxoo/kept/README.rst": "needle readme",
        "extra_addons/vauxoo/kept/tests/test_kept.py": "needle unittest",
        "extra_addons/vauxoo/kept/node_modules/dep/index.js": "needle node",
        "extra_addons/vauxoo/dropped/__manifest__.py": "{'name': 'dropped'}",
        "extra_addons/vauxoo/dropped/models/dropped.py": "needle dropped",
    }
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return str(tmp_path)


@pytest.mark.skipif(indexer.find_tgrep() is None, reason="tgrep is not installed")
def test_real_tgrep_honours_the_rendered_ignore(tmp_path, caplog):
    """The .ignore whitelist decides the scope at index time and at search time alike."""
    root = _fake_instance(tmp_path)
    modules = ["odoo/addons/sale", "extra_addons/vauxoo/kept"]
    indexer.write_ignore(root, indexer.render_ignore(modules))
    indexer.build_index(root, force=True)
    assert os.path.isdir(os.path.join(root, indexer.INDEX_DIR))
    caplog.set_level(logging.INFO)
    assert indexer.validate_scope(root, modules) is True
    assert sorted(indexer.indexed_files(root)) == [
        "extra_addons/vauxoo/kept/README.rst",
        "extra_addons/vauxoo/kept/__manifest__.py",
        "extra_addons/vauxoo/kept/models/kept.py",
        "extra_addons/vauxoo/kept/tests/test_kept.py",
        "odoo/addons/sale/__manifest__.py",
        "odoo/addons/sale/models/sale.py",
        "odoo/addons/sale/static/src/js/app.js",
        "odoo/odoo/tools/misc.py",
    ]
    status = indexer.index_status(root)
    assert "Files:" in status
    hits = indexer.subprocess.check_output(
        [indexer.find_tgrep(), "-l", "needle", root], universal_newlines=True
    ).splitlines()
    hits = sorted(os.path.relpath(hit, root).replace(os.sep, "/") for hit in hits)
    assert hits == [
        "extra_addons/vauxoo/kept/README.rst",
        "extra_addons/vauxoo/kept/models/kept.py",
        "extra_addons/vauxoo/kept/tests/test_kept.py",
        "odoo/addons/sale/models/sale.py",
        "odoo/addons/sale/static/src/js/app.js",
        "odoo/odoo/tools/misc.py",
    ]
