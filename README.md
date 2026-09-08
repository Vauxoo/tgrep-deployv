[//]: # (start-badges)

[![Build Status](https://github.com/Vauxoo/tgrep-deployv/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/Vauxoo/tgrep-deployv/actions/workflows/test.yml?query=branch%3Amain)
[![codecov](https://codecov.io/gh/Vauxoo/tgrep-deployv/branch/main/graph/badge.svg)](https://codecov.io/gh/Vauxoo/tgrep-deployv)
[![code-style-black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![version](https://img.shields.io/pypi/v/tgrep-deployv.svg)](https://pypi.org/project/tgrep-deployv)
[![pypi-downloads-monthly](https://img.shields.io/pypi/dm/tgrep-deployv.svg?style=flat)](https://pypi.python.org/pypi/tgrep-deployv)
[![supported-versions](https://img.shields.io/pypi/pyversions/tgrep-deployv.svg)](https://pypi.org/project/tgrep-deployv)
[![commits-since](https://img.shields.io/github/commits-since/Vauxoo/tgrep-deployv/v0.1.1.svg)](https://github.com/Vauxoo/tgrep-deployv/compare/v0.1.1...main)

[//]: # (end-badges)

# tgrep-deployv

Install [tgrep](https://github.com/microsoft/tgrep) (trigram-indexed grep, up to 52x faster than
ripgrep on large trees) and build its index for Odoo instances inside containers following the
Vauxoo layout:

```text
/home/odoo/instance/odoo
/home/odoo/instance/extra_addons/*
```

This tool only runs inside such a container (or SSH session) as user `odoo`; it refuses to start
when `/home/odoo/instance/odoo/odoo-bin` is missing. It always indexes the real instance path,
never an rsync/copy, so the paths tgrep prints are the files people then open and edit.

## Install

```bash
pip install tgrep-deployv
```

## Usage

```bash
tgrep-deployv
```

That single command:

1. Installs `tgrep` under `~/.local/bin` when it is not on `PATH`: downloads the release asset
   built for this platform from GitHub (newest stable release, `v1.0.4` as the floor), verifies
   it against the published `checksums.txt` and extracts the binary. No `curl | bash`, no
   cargo, no root. `TGREP_VERSION` pins a release; `TGREP_DOWNLOAD_URL` points at a mirror.
2. Picks the module list, in this order:
   - **`--modules-file` given**: the modules listed there (see below).
   - **Database reachable** through `odoo-bin shell`: only installed modules from
     `ir.module.module` (no `test_*` addon modules, keeping backend unit tests inside real
     modules), listed by `list_installed_modules.py` which is fed to the shell.
   - **Neither**: every addon module found on disk.
3. Renders `/home/odoo/instance/.ignore` (Odoo core `odoo/odoo` complete + selected modules,
   dropping `i18n`, `static/lib`, minified assets, caches and `.git`). tgrep reads `.ignore`
   files with gitignore syntax both when indexing and when searching, so a plain
   `tgrep PATTERN /home/odoo/instance` sees exactly the same scope with no extra flag.
4. Runs `tgrep index /home/odoo/instance`, which writes the trigram index to
   `/home/odoo/instance/.tgrep` with bounded memory (external merge sort), and prints
   `tgrep status`.
5. Validates the scope: no `test_*` addon modules in it, no missing and no extra module roots
   compared to the expected module list, and **every file in scope carrying one of the
   configured extensions** (`SOURCE_GLOBS`: `.py .xml .js .scss .css .csv .sql .rst .md .html
   .json .yml .yaml .txt .cfg .toml .sh`); anything else means `.ignore` did not apply. Counts
   per extension are logged, unexpected ones marked `(!)`:

   ```text
   extensions_configured=.cfg,.css,.csv,.html,.js,.json,.md,.py,.rst,.scss,.sh,.sql,.toml,.txt,.xml,.yaml,.yml
   extensions_indexed=6 files=12057
   extension .py      files=4949
   extension .xml     files=3276
   extension .po      files=39 (!)
   unexpected_extensions=1 files=39 (not in SOURCE_GLOBS: .po)
   ```

   Both checks read `tgrep --files`, the list of files tgrep would search under the root.

Translations are the one text format deliberately left out: every module ships one `.po` per
language, so they multiply the same strings by ~80 without adding anything to grep for from here.

Then search as usual, with the ripgrep flags you already know:

```bash
tgrep "def _compute_amount" /home/odoo/instance
tgrep -l "inherit_id=\"sale.view_order_form\"" /home/odoo/instance
tgrep-deployv --serve   # keep the index hot and watched; searches answer in milliseconds
```

## Options

```text
--repo-path PATH    instance root to index (default: /home/odoo/instance)
--mode MODE         auto (default), installed, or all
--modules-file PATH ir.module.module export listing the modules to index
--force             rebuild the index from scratch (tgrep index --force)
--serve             start "tgrep serve" detached afterwards (log in .tgrep/serve.log)
--skip-install      do not install tgrep when missing
--skip-validate     do not validate the index scope afterwards
```

## Indexing the modules installed in production

A local docker container only installs the modules of the main app, while production usually has
more modules installed by hand. Export `ir.module.module` from production and pass the file, so
the index covers the same code production runs:

```bash
tgrep-deployv --modules-file modules_installed.csv
```

The file is an `ir.module.module` export, either a csv or one module name per line
(`#` comments and blank lines are ignored):

```csv
name,state
sale,installed
purchase,uninstalled
```

- With a `state` column (`state`/`status`), only the `installed` rows are used.
- Without one, the file is assumed to be already filtered to the installed modules.
- Column headers may be the technical names (`name`, `state`) or the exported labels
  (`Technical Name`, `Status`); a headerless file is read as `name[,state]`.
- Modules listed but missing on disk are reported as `not_on_disk` and skipped; they simply are
  not in this container's repos.

## Development

```bash
tox -e py-multi   # tests in parallel (the integration test needs tgrep on PATH, else it skips)
tox -e lint       # pre-commit checks
tox -e build      # release rehearsal (sdist+wheel, twine check, install smoke test)
```
