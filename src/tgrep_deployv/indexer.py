"""Install tgrep and build its trigram index for Odoo instances living in Vauxoo containers.

The container layout is the one deployv builds:

    /home/odoo/instance/odoo
    /home/odoo/instance/extra_addons/*

The real instance path is always indexed (never an rsync/copy) so the paths tgrep prints
are the files people then open and edit. The index lives in ``<root>/.tgrep`` and the scope
is written to ``<root>/.ignore``, which tgrep honours both when indexing and when searching,
so a plain ``tgrep PATTERN /home/odoo/instance`` needs no extra flag.
"""

import csv
import hashlib
import json
import logging
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

_logger = logging.getLogger(__name__)

TGREP_BIN = "tgrep.exe" if os.name == "nt" else "tgrep"
TGREP_REPO = "microsoft/tgrep"
# Floor, not ceiling: a newer stable release wins over it (see latest_release_tag), and a
# TGREP_VERSION or TGREP_DOWNLOAD_URL already in the environment wins over both.
TGREP_PINNED_VERSION = "v1.0.4"
TGREP_LATEST_RELEASE_API = "https://api.github.com/repos/%s/releases/latest" % TGREP_REPO
TGREP_DOWNLOAD_URL_TMPL = "https://github.com/%s/releases/download/%%s/%%s" % TGREP_REPO
TGREP_CHECKSUMS_NAME = "checksums.txt"
# Release assets are named tgrep-<tag>-<arch>-<target>.<ext>; the target is the Rust triple.
RELEASE_TARGETS = {
    ("linux", "x86_64"): ("x86_64-unknown-linux-musl", "tar.gz"),
    ("linux", "aarch64"): ("aarch64-unknown-linux-musl", "tar.gz"),
    ("darwin", "x86_64"): ("x86_64-apple-darwin", "tar.gz"),
    ("darwin", "aarch64"): ("aarch64-apple-darwin", "tar.gz"),
    ("windows", "x86_64"): ("x86_64-pc-windows-msvc", "zip"),
    ("windows", "aarch64"): ("aarch64-pc-windows-msvc", "zip"),
}
MACHINE_ALIASES = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}
DEFAULT_INSTALL_DIR = os.path.join(os.path.expanduser("~"), ".local", "bin")
DOWNLOAD_TIMEOUT = 60

DEFAULT_ROOT = "/home/odoo/instance"
CORE_DIRS = ("odoo/odoo",)
CORE_PREFIX = "odoo/odoo/"
INDEX_DIR = ".tgrep"
# tgrep reads ".ignore" files (gitignore syntax) wherever it walks, like ripgrep does, so the
# scope is written there instead of being passed with --ignore-file on every search.
IGNORE_FILE = ".ignore"
IGNORE_BACKUP_SUFFIX = ".before-tgrep-deployv"
SERVE_LOG = "serve.log"
# tgrep is a text search, so module prose stays in: a README, a mail template or a JSON
# fixture are things people grep for. What is dropped is what multiplies the index without
# adding a string anyone searches from here: translations (every module ships one .po per
# language, ~80 copies of the same strings), vendored and minified JavaScript, and caches.
SOURCE_GLOBS = (
    "*.py",
    "*.xml",
    "*.js",
    "*.scss",
    "*.css",
    "*.csv",
    "*.sql",
    "*.rst",
    "*.md",
    "*.html",
    "*.json",
    "*.yml",
    "*.yaml",
    "*.txt",
    "*.cfg",
    "*.toml",
    "*.sh",
)
# What .ignore lets through, so what the index is expected to hold: ".py", ".xml", ...
SOURCE_EXTENSIONS = tuple(sorted(pattern[1:] for pattern in SOURCE_GLOBS))
HEAVY_PATTERNS = (
    ".git/",
    "**/.git/",
    ".github/",
    "**/.github/",
    "__pycache__/",
    "**/__pycache__/",
    "node_modules/",
    "**/node_modules/",
    ".cache/",
    "**/.cache/",
    ".tx/",
    "**/.tx/",
    "dist/",
    "**/dist/",
    "build/",
    "**/build/",
    "**/i18n/",
    "**/i18n/**",
    "**/static/lib/",
    "**/static/lib/**",
    "**/*.min.js",
    "**/*-min.js",
    "**/*.min.css",
)
MANIFEST_NAMES = ("__manifest__.py", "__openerp__.py")
PRUNE_DIRS = {".git", ".github", "__pycache__", "node_modules", ".cache", ".tx", "dist", "build", "setup"}

# Fed to "odoo-bin shell" as text (it needs the shell's "self"), so it ships as a real
# module of the package instead of a string constant: black/pytest see it like any file.
LIST_INSTALLED_MODULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "list_installed_modules.py")

# Column names accepted in a modules file: an ir.module.module export uses the technical
# names when exporting fields, and the labels when exporting through the UI translations.
MODULES_FILE_NAME_COLUMNS = ("name", "technical name", "module")
MODULES_FILE_STATE_COLUMNS = ("state", "status")
INSTALLED_STATE = "installed"
NO_EXTENSION = "(none)"


def list_installed_modules_script():
    """Return the source fed to odoo-bin shell to list the installed modules."""
    with open(LIST_INSTALLED_MODULES_PATH) as script:
        return script.read()


def check_layout(root):
    """Fail fast when not running inside a container with the expected layout."""
    odoo_bin = os.path.join(root, "odoo", "odoo-bin")
    if not os.path.isfile(odoo_bin):
        raise SystemExit(
            "%s not found. This tool only runs inside a container following the "
            "/home/odoo/instance layout (odoo + extra_addons)." % odoo_bin
        )
    return odoo_bin


def find_tgrep():
    """Locate the tgrep binary, extending PATH with the directories the installer uses."""
    found = shutil.which(TGREP_BIN)
    if found:
        return found
    for folder in (DEFAULT_INSTALL_DIR, os.path.expanduser("~/bin"), "/usr/local/bin"):
        candidate = os.path.join(folder, TGREP_BIN)
        if os.access(candidate, os.X_OK):
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
            return candidate
    return None


def tgrep_version(binary):
    """Return the installed tgrep version ("1.0.4"), or "" when it cannot be read."""
    try:
        raw = subprocess.check_output([binary, "--version"], universal_newlines=True)
    except (OSError, subprocess.CalledProcessError):
        return ""
    words = raw.strip().splitlines()[-1].split() if raw.strip() else []
    return words[-1] if words else ""


def version_key(version):
    """Sort key for release tags: numeric parts first, then a final release beats its rc.

    "v1.1.0" > "v1.1.0-rc.2" > "v1.1.0-rc.1" > "v1.0.4". Good enough for these tags;
    not a full semver implementation on purpose (no build metadata, no alpha/beta order).
    """
    release, _, prerelease = version.lstrip("v").partition("-")
    numbers = tuple(int(part) for part in release.split(".") if part.isdigit())
    rc = int("".join(char for char in prerelease if char.isdigit()) or 0)
    return (numbers, 0 if prerelease else 1, rc)


def release_asset_name(tag, system=None, machine=None):
    """Name of the release asset built for this platform, e.g. tgrep-v1.0.4-x86_64-unknown-linux-musl.tar.gz."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    machine = MACHINE_ALIASES.get(machine, machine)
    target = RELEASE_TARGETS.get((system, machine))
    if not target:
        raise SystemExit(
            "No tgrep release for %s/%s; build it with cargo (https://github.com/%s) or set TGREP_DOWNLOAD_URL"
            % (system, machine, TGREP_REPO)
        )
    triple, extension = target
    return "tgrep-%s-%s.%s" % (tag, triple, extension)


def latest_release_tag():
    """Return the newest usable release tag, or None to use the pin.

    "Usable" means a real release (the GitHub /releases/latest endpoint never returns
    prereleases) strictly newer than the pinned version. Any failure (offline registry,
    rate-limited API, changed payload) falls back to the pin: installing must keep working
    without access to the GitHub API, and the download itself is not rate limited.
    """
    try:
        with urllib.request.urlopen(TGREP_LATEST_RELEASE_API, timeout=10) as response:
            release = json.load(response)
    except (OSError, ValueError):
        return None
    tag = release.get("tag_name") or ""
    if not tag or release.get("prerelease"):
        return None
    if version_key(tag) <= version_key(TGREP_PINNED_VERSION):
        return None
    return tag


def resolve_download(tag=None):
    """Return (archive url, checksums url or None, asset name) honouring the environment.

    TGREP_DOWNLOAD_URL points at the archive itself (a mirror, an internal artifact store);
    checksums.txt is then looked up next to it. TGREP_VERSION pins a release tag. With
    neither, the newest usable release is used and the pin is the fallback.
    """
    url = os.environ.get("TGREP_DOWNLOAD_URL")
    if url:
        base, _, asset = url.rpartition("/")
        return url, "%s/%s" % (base, TGREP_CHECKSUMS_NAME) if base else None, asset
    tag = tag or os.environ.get("TGREP_VERSION") or latest_release_tag() or TGREP_PINNED_VERSION
    if not tag.startswith("v"):
        tag = "v" + tag
    asset = release_asset_name(tag)
    return TGREP_DOWNLOAD_URL_TMPL % (tag, asset), TGREP_DOWNLOAD_URL_TMPL % (tag, TGREP_CHECKSUMS_NAME), asset


def download(url, destination):
    """Download url into destination (a path), streaming to keep memory flat."""
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response, open(destination, "wb") as target:
        shutil.copyfileobj(response, target)
    return destination


def expected_checksum(checksums_text, asset):
    """The sha256 listed for asset in a checksums.txt ("<sha256>  <asset>" per line), or None."""
    for line in checksums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset:
            return parts[0].lower()
    return None


def verify_checksum(archive, checksums_text, asset):
    """Raise SystemExit when the archive does not match the published sha256."""
    expected = expected_checksum(checksums_text, asset)
    if not expected:
        raise SystemExit(
            "%s does not list %s; refusing to install an unverified binary" % (TGREP_CHECKSUMS_NAME, asset)
        )
    digest = hashlib.sha256()
    with open(archive, "rb") as handler:
        for chunk in iter(lambda: handler.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise SystemExit("sha256 mismatch for %s: expected %s, got %s" % (asset, expected, actual))
    return actual


def extract_binary(archive, install_dir):
    """Extract the tgrep executable from a release archive into install_dir; return its path."""
    target = os.path.join(install_dir, TGREP_BIN)
    os.makedirs(install_dir, exist_ok=True)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            member = next(
                (name for name in bundle.namelist() if os.path.basename(name) in ("tgrep", "tgrep.exe")), None
            )
            if not member:
                raise SystemExit("%s does not contain a tgrep executable" % archive)
            with bundle.open(member) as source, open(target, "wb") as destination:
                shutil.copyfileobj(source, destination)
    else:
        with tarfile.open(archive, "r:*") as bundle:
            member = next(
                (item for item in bundle.getmembers() if item.isfile() and os.path.basename(item.name) == "tgrep"),
                None,
            )
            if not member:
                raise SystemExit("%s does not contain a tgrep executable" % archive)
            with bundle.extractfile(member) as source, open(target, "wb") as destination:
                shutil.copyfileobj(source, destination)
    mode = os.stat(target).st_mode
    os.chmod(target, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def install_tgrep(tag=None, install_dir=DEFAULT_INSTALL_DIR):
    """Download the release archive, verify it against checksums.txt and install the binary."""
    archive_url, checksums_url, asset = resolve_download(tag)
    _logger.info("installing tgrep from %s", archive_url)
    workdir = tempfile.mkdtemp(prefix="tgrep-deployv-")
    try:
        archive = download(archive_url, os.path.join(workdir, asset))
        checksums_text = ""
        if checksums_url:
            try:
                with open(download(checksums_url, os.path.join(workdir, TGREP_CHECKSUMS_NAME))) as handler:
                    checksums_text = handler.read()
            except OSError:
                checksums_text = ""
        if checksums_text:
            verify_checksum(archive, checksums_text, asset)
        elif os.environ.get("TGREP_DOWNLOAD_URL"):
            _logger.warning("no %s next to TGREP_DOWNLOAD_URL; installing %s unverified", TGREP_CHECKSUMS_NAME, asset)
        else:
            raise SystemExit("could not download %s; refusing to install an unverified binary" % checksums_url)
        return extract_binary(archive, install_dir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def ensure_tgrep_installed():
    """Return the tgrep binary, installing it under ~/.local/bin when it is not on PATH."""
    found = find_tgrep()
    if found:
        _logger.info("tgrep=%s version=%s", found, tgrep_version(found))
        return found
    install_tgrep()
    found = find_tgrep()
    if not found:
        raise SystemExit("tgrep still not found after installing it; is %s on PATH?" % DEFAULT_INSTALL_DIR)
    _logger.info("tgrep=%s version=%s", found, tgrep_version(found))
    return found


def run_odoo_shell(root, script):
    """Feed a script to odoo-bin shell and return its combined output ("" on failure)."""
    odoo_bin = os.path.join(root, "odoo", "odoo-bin")
    env = dict(os.environ)
    env["TGREP_ROOT"] = root
    try:
        proc = subprocess.run(
            [odoo_bin, "shell", "--no-http", "--stop-after-init", "--log-level=warn"],
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=env,
            cwd=root,
        )
    except OSError:
        return ""
    return proc.stdout or ""


def installed_modules(root):
    """Return installed module paths via odoo-bin shell, or None when no database is reachable."""
    output = run_odoo_shell(root, list_installed_modules_script())
    if "tgrep_done=1" not in output:
        return None
    paths = set()
    for line in output.splitlines():
        if line.startswith("tgrep_module_path="):
            paths.add(line.split("=", 1)[1].strip())
        elif line.startswith("tgrep_import_error"):
            _logger.warning("%s", line)
    return sorted(paths)


def discover_modules(root):
    """Return every addon module found on disk (no database mode), excluding test_* addons.

    The odoo/odoo core tree is skipped here because .ignore always includes it completely.
    """
    core = os.path.join(root, "odoo", "odoo")
    found = set()
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in PRUNE_DIRS and not name.startswith(".")]
        if current == core:
            dirs[:] = []
            continue
        if any(name in files for name in MANIFEST_NAMES):
            dirs[:] = []
            if os.path.basename(current).startswith("test_"):
                continue
            found.add(os.path.relpath(current, root).replace(os.sep, "/"))
    return sorted(found)


def _columns(row):
    """Indexes of the name/state columns when the row is a header, else (None, None)."""
    cells = [cell.strip().strip('"').lower() for cell in row]
    name_index = next((index for index, cell in enumerate(cells) if cell in MODULES_FILE_NAME_COLUMNS), None)
    if name_index is None:
        return None, None
    state_index = next((index for index, cell in enumerate(cells) if cell in MODULES_FILE_STATE_COLUMNS), None)
    return name_index, state_index


def read_modules_file(path):
    """Return the module names listed in an ir.module.module export (csv or plain list).

    Production usually has modules installed by hand that the local docker container does
    not install, so its module list can be exported and passed instead of asking the local
    database. A "state" column keeps only the installed rows; without one the file is
    assumed to be already filtered, which is what a manual export looks like.
    """
    with open(path) as handler:
        rows = list(csv.reader(line for line in handler if line.strip() and not line.lstrip().startswith("#")))
    if not rows:
        return []
    name_index, state_index = _columns(rows[0])
    if name_index is None:
        # Headerless: one module name per line, optionally followed by its state.
        name_index, state_index = 0, 1 if len(rows[0]) > 1 else None
    else:
        rows = rows[1:]
    names = set()
    for row in rows:
        if len(row) <= name_index:
            continue
        if state_index is not None:
            state = row[state_index].strip().lower() if len(row) > state_index else ""
            if state != INSTALLED_STATE:
                continue
        name = row[name_index].strip()
        if name and not name.startswith("test_"):
            names.add(name)
    return sorted(names)


def modules_from_names(root, names):
    """Map module names to their paths on disk; return (module paths, names not found)."""
    paths_by_name = {}
    for rel in discover_modules(root):
        paths_by_name.setdefault(os.path.basename(rel), []).append(rel)
    paths = set()
    unknown = []
    for name in names:
        found = paths_by_name.get(name)
        if not found:
            unknown.append(name)
            continue
        # Several repos may ship the same module name; without the database the effective
        # addons_path order is unknown, so keep every copy rather than guessing one.
        paths.update(found)
    return sorted(paths), unknown


def render_ignore(module_rel_paths):
    """Render a .ignore keeping only the Odoo core tree plus the given modules.

    gitignore semantics: a file cannot be re-included when one of its parent directories
    is excluded, so every parent of a kept module is re-included explicitly before the
    module's own files. Later patterns win, which is why the heavy paths come last.
    """
    patterns = []
    seen = set()

    def add(line):
        if line not in seen:
            seen.add(line)
            patterns.append(line)

    def include_tree(rel):
        parts = rel.split("/")
        cur = ""
        for part in parts:
            cur = part if not cur else cur + "/" + part
            add("!%s/" % cur)
        add("!%s/**/" % rel)
        for glob_pattern in SOURCE_GLOBS:
            add("!%s/**/%s" % (rel, glob_pattern))

    add("# Generated by tgrep-deployv from Odoo modules.")
    add("# tgrep reads this file when indexing and when searching %s." % DEFAULT_ROOT)
    add("")
    add("*")
    add("")
    add("# Odoo core package. Keep it complete for ORM/http/sql/api internals.")
    for rel in CORE_DIRS:
        include_tree(rel)
    add("")
    add("# Drop Odoo test addon modules, but keep unittest folders inside real modules.")
    for line in [
        "odoo/odoo/addons/test_*/",
        "odoo/odoo/addons/test_*/**",
        "odoo/addons/test_*/",
        "odoo/addons/test_*/**",
    ]:
        add(line)
    add("")
    add("# Selected Odoo addons.")
    for rel in sorted(module_rel_paths):
        include_tree(rel)
    add("")
    add("# Heavy/generated/vendor paths and translations.")
    for line in HEAVY_PATTERNS:
        add(line)
    return "\n".join(patterns).rstrip() + "\n"


def write_ignore(root, content):
    """Write .ignore in the instance root, backing up any pre-existing file once."""
    path = os.path.join(root, IGNORE_FILE)
    backup = path + IGNORE_BACKUP_SUFFIX
    if os.path.exists(path) and not os.path.exists(backup):
        shutil.copyfile(path, backup)
    with open(path, "w") as fh:
        fh.write(content)
    return path


def build_index(root, force=False):
    """Build (or refresh) the trigram index of root in <root>/.tgrep."""
    cmd = [find_tgrep() or TGREP_BIN, "index", root]
    if force:
        cmd.append("--force")
    subprocess.check_call(cmd)


def index_status(root):
    """Return the text of "tgrep status <root>" (files, trigrams, server)."""
    return subprocess.check_output([find_tgrep() or TGREP_BIN, "status", root], universal_newlines=True)


def start_server(root):
    """Start "tgrep serve <root>" detached, logging into <root>/.tgrep/serve.log; return its pid.

    The server keeps the index hot in memory and watches the tree, so the searches that
    follow answer in milliseconds and pick up edits without re-indexing.
    """
    log_dir = os.path.join(root, INDEX_DIR)
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, SERVE_LOG)
    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(
            [find_tgrep() or TGREP_BIN, "serve", root],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=root,
            start_new_session=True,
        )
    _logger.info("serve_pid=%d serve_log=%s", proc.pid, log_path)
    return proc.pid


def indexed_files(root):
    """Every file tgrep searches under root, relative to it with "/" separators.

    "tgrep --files" walks the tree with the same ignore rules the indexer applies, so it
    is the list of files in scope: what .ignore let through. Paths come back absolute when
    root is absolute, so they are made relative here.
    """
    raw = subprocess.check_output([find_tgrep() or TGREP_BIN, "--files", root], universal_newlines=True)
    root_abs = os.path.abspath(root)
    paths = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if os.path.isabs(line):
            line = os.path.relpath(line, root_abs)
        elif line.startswith(root):
            line = os.path.relpath(line, root)
        paths.append(line.replace(os.sep, "/"))
    return paths


def indexed_manifest_files(paths):
    """Manifest paths among the given indexed file paths."""
    return [path for path in paths if path.rpartition("/")[2] in MANIFEST_NAMES]


def indexed_extensions(paths):
    """Return {extension: file count} for the given indexed file paths."""
    counts = {}
    for path in paths:
        extension = os.path.splitext(path)[1] or NO_EXTENSION
        counts[extension] = counts.get(extension, 0) + 1
    return counts


def validate_extensions(paths):
    """Prove the scope only holds the extensions .ignore lets through.

    Anything else means .ignore did not apply (stale file, pattern typo, another root),
    which silently bloats the index with vendored/generated content and translations.
    """
    counts = indexed_extensions(paths)
    unexpected = {ext: total for ext, total in counts.items() if ext not in SOURCE_EXTENSIONS}
    _logger.info("extensions_configured=%s", ",".join(SOURCE_EXTENSIONS))
    _logger.info("extensions_indexed=%d files=%d", len(counts), sum(counts.values()))
    for extension, total in sorted(counts.items(), key=lambda item: -item[1]):
        _logger.info("extension %-8s files=%d%s", extension, total, "" if extension in SOURCE_EXTENSIONS else " (!)")
    if unexpected:
        _logger.error(
            "unexpected_extensions=%d files=%d (not in SOURCE_GLOBS: %s)",
            len(unexpected),
            sum(unexpected.values()),
            ",".join(sorted(unexpected)),
        )
        return False
    _logger.info("unexpected_extensions=0")
    return True


def outermost_module_roots(module_rel_paths):
    """Drop module roots nested inside another one.

    Odoo ships manifests *inside* real modules: the base_import_module test fixture, the
    point_of_sale posbox overwrite_after_init overlay. Those are files of their parent
    module, not modules of their own; discover_modules prunes at the first manifest for
    the same reason, so the comparison has to prune the indexed side too.
    """
    kept = []
    for rel in sorted(module_rel_paths):
        if kept and rel.startswith(kept[-1] + "/"):
            continue
        kept.append(rel)
    return set(kept)


def _is_test_addon(module_rel_path):
    """True for Odoo test addon modules ("<...>/addons/test_*"), not for test folders."""
    parent, _, name = module_rel_path.rpartition("/")
    return name.startswith("test_") and (parent == "addons" or parent.endswith("/addons"))


def validate_scope(root, expected_module_paths):
    """Prove the search scope matches the expected modules; return True when clean.

    Addons are enumerated through their manifests: every addon has exactly one, so the
    manifests in scope are the modules in scope. The odoo/odoo core tree is excluded from
    the comparison: it is included completely by design.
    """
    ok = True
    paths = indexed_files(root)
    indexed_roots = outermost_module_roots(path.rpartition("/")[0] for path in indexed_manifest_files(paths))
    leaked_tests = sorted(rel for rel in indexed_roots if _is_test_addon(rel))
    if leaked_tests:
        ok = False
        _logger.error("test_addon_modules_indexed=%d (must be 0)", len(leaked_tests))
        for rel in leaked_tests[:20]:
            _logger.error("leaked %s", rel)
    indexed_cmp = {rel for rel in indexed_roots if not rel.startswith(CORE_PREFIX)}
    expected_cmp = {rel for rel in expected_module_paths if not rel.startswith(CORE_PREFIX)}
    missing = sorted(expected_cmp - indexed_cmp)
    extra = sorted(indexed_cmp - expected_cmp)
    _logger.info("module_roots_expected=%d", len(expected_cmp))
    _logger.info("module_roots_indexed=%d", len(indexed_cmp))
    _logger.info("missing_module_roots=%d", len(missing))
    _logger.info("extra_indexed_module_roots=%d", len(extra))
    for rel in missing[:50]:
        _logger.error("missing %s", rel)
    for rel in extra[:50]:
        _logger.error("extra %s", rel)
    # Extensions are checked last: the module comparison is the headline, this one tells
    # whether .ignore really applied to whatever did get indexed.
    return validate_extensions(paths) and ok and not missing and not extra
