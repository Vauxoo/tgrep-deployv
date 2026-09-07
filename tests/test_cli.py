import logging

import pytest

from tgrep_deployv import cli
from tgrep_deployv.indexer import DEFAULT_ROOT


@pytest.fixture(autouse=True)
def _drop_log_handlers():
    """Detach the handler main() installs on the package logger.

    It holds the stdout pytest captured for this test; left in place, logging flushes to
    that closed stream when the worker shuts down ("I/O operation on closed file").
    """
    yield
    logger = logging.getLogger(cli.PACKAGE_LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)


def test_parser_defaults():
    args = cli.build_parser().parse_args([])
    assert args.repo_path == DEFAULT_ROOT
    assert args.mode == "auto"
    assert args.modules_file is None
    assert not args.force
    assert not args.serve
    assert not args.skip_install
    assert not args.skip_validate


def test_main_fails_outside_container(tmp_path):
    try:
        cli.main(["--repo-path", str(tmp_path)])
    except SystemExit as error:
        assert "odoo-bin" in str(error)
    else:
        raise AssertionError("main must refuse to run outside the container layout")


def _instance(tmp_path, *module_rel_paths):
    odoo_dir = tmp_path / "odoo"
    odoo_dir.mkdir()
    (odoo_dir / "odoo-bin").write_text("")
    for rel in module_rel_paths:
        folder = tmp_path / rel
        folder.mkdir(parents=True)
        (folder / "__manifest__.py").write_text("{'name': 'x'}")
    return str(tmp_path)


def _stub_tgrep(monkeypatch, built=None):
    """Replace every call that needs a real tgrep binary; record build_index calls in built."""
    built = [] if built is None else built
    monkeypatch.setattr(cli, "ensure_tgrep_installed", lambda: "/fake/tgrep")
    monkeypatch.setattr(cli, "build_index", lambda root, force=False: built.append((root, force)))
    monkeypatch.setattr(cli, "index_status", lambda root: "Index status for %s\n  Files: 3\n" % root)
    monkeypatch.setattr(cli, "validate_scope", lambda root, modules: True)
    monkeypatch.setattr(cli, "start_server", lambda root: pytest.fail("--serve was not given"))
    return built


def test_main_modules_file_wins_over_the_database(tmp_path, monkeypatch, capsys):
    """Production may have modules the local container never installs: the file rules."""
    root = _instance(tmp_path, "extra_addons/vauxoo/sale_extended", "extra_addons/vauxoo/never_installed_here")
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text("name,state\nsale_extended,installed\nnever_installed_here,installed\nweb,installed\n")
    built = _stub_tgrep(monkeypatch)
    monkeypatch.setattr(cli, "installed_modules", lambda root_: pytest.fail("the database must not be queried"))
    assert cli.main(["--repo-path", root, "--modules-file", str(modules_file)]) == 0
    out = capsys.readouterr().out
    assert "mode=file modules=2" in out
    assert "not_on_disk=1" in out
    assert "not_on_disk web" in out
    assert "Files: 3" in out
    assert "validation=ok" in out
    assert built == [(root, False)]
    ignore = (tmp_path / ".ignore").read_text()
    assert "!extra_addons/vauxoo/never_installed_here/**/*.py" in ignore


def test_main_modules_file_ignored_with_mode_all(tmp_path, monkeypatch, capsys):
    root = _instance(tmp_path, "extra_addons/vauxoo/sale_extended")
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text("name\nsale_extended\n")
    built = _stub_tgrep(monkeypatch)
    monkeypatch.setattr(cli, "validate_scope", lambda root_, modules: pytest.fail("--skip-validate was given"))
    args = ["--repo-path", root, "--mode=all", "--modules-file", str(modules_file), "--skip-validate", "--force"]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "modules_file=ignored" in out
    assert "mode=all modules=1" in out
    assert built == [(root, True)]


def test_main_modules_file_without_modules_on_disk(tmp_path, monkeypatch):
    root = _instance(tmp_path, "extra_addons/vauxoo/sale_extended")
    modules_file = tmp_path / "modules.csv"
    modules_file.write_text("name\nonly_in_production\n")
    _stub_tgrep(monkeypatch)
    try:
        cli.main(["--repo-path", root, "--modules-file", str(modules_file)])
    except SystemExit as error:
        assert "does not list any module" in str(error)
    else:
        raise AssertionError("main must fail when the modules file matches nothing on disk")


def test_main_falls_back_to_disk_without_database(tmp_path, monkeypatch, capsys):
    root = _instance(tmp_path, "extra_addons/vauxoo/a", "extra_addons/vauxoo/b", "odoo/addons/test_mail")
    _stub_tgrep(monkeypatch)
    monkeypatch.setattr(cli, "installed_modules", lambda root_: None)
    assert cli.main(["--repo-path", root]) == 0
    out = capsys.readouterr().out
    assert "database=not-reachable" in out
    assert "mode=all modules=2" in out


def test_main_mode_installed_requires_a_database(tmp_path, monkeypatch):
    root = _instance(tmp_path, "extra_addons/vauxoo/a")
    _stub_tgrep(monkeypatch)
    monkeypatch.setattr(cli, "installed_modules", lambda root_: None)
    with pytest.raises(SystemExit) as error:
        cli.main(["--repo-path", root, "--mode=installed"])
    assert "--mode=all" in str(error.value)


def test_main_reports_validation_failure(tmp_path, monkeypatch):
    root = _instance(tmp_path, "extra_addons/vauxoo/a")
    _stub_tgrep(monkeypatch)
    monkeypatch.setattr(cli, "installed_modules", lambda root_: ["extra_addons/vauxoo/a"])
    monkeypatch.setattr(cli, "validate_scope", lambda root_, modules: False)
    with pytest.raises(SystemExit) as error:
        cli.main(["--repo-path", root])
    assert "Validation failed" in str(error.value)


def test_main_serve_starts_the_server_last(tmp_path, monkeypatch, capsys):
    root = _instance(tmp_path, "extra_addons/vauxoo/a")
    _stub_tgrep(monkeypatch)
    monkeypatch.setattr(cli, "installed_modules", lambda root_: ["extra_addons/vauxoo/a"])
    served = []
    monkeypatch.setattr(cli, "start_server", lambda root_: served.append(root_) or 4242)
    assert cli.main(["--repo-path", root, "--serve", "--skip-install"]) == 0
    assert served == [root]
    assert "mode=installed modules=1" in capsys.readouterr().out
