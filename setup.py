import re
from os.path import dirname, join

from setuptools import find_packages, setup

try:
    from pbr import git
except ImportError:
    git = None


def generate_changelog():
    fname = "ChangeLog"
    if not git:
        changelog_str = '# ChangeLog was not generated. You need to install "pbr"'
        with open(fname, "w", encoding="UTF-8") as fchg:
            fchg.write(changelog_str)
        return changelog_str
    # pylint: disable=protected-access
    changelog = git._iter_log_oneline()
    changelog = git._iter_changelog(changelog)
    git.write_git_changelog(changelog=filter(lambda log: not log[1].startswith("* Bump version"), changelog))
    return read(fname)


def generate_dependencies():
    return read("requirements.txt").splitlines()


def read(*names, **kwargs):
    with open(join(dirname(__file__), *names), encoding=kwargs.get("encoding", "utf8")) as file_obj:
        return file_obj.read()


def generage_long_description():
    long_description = "{}\n{}".format(
        read("README.md"),
        re.sub(":[a-z]+:`~?(.*?)`", r"``\1``", generate_changelog()),
    )
    return long_description


setup(
    name="tgrep-deployv",
    version="0.1.0",
    license="LGPL-3.0-or-later",
    description="Install tgrep and build its trigram index for Odoo instances inside Vauxoo containers",
    long_description=generage_long_description(),
    long_description_content_type="text/markdown",
    author="Vauxoo",
    author_email="info@vauxoo.com",
    url="https://github.com/Vauxoo/tgrep-deployv",
    packages=find_packages("src"),
    package_dir={"": "src"},
    include_package_data=True,
    zip_safe=False,
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)",
        "Operating System :: POSIX",
        "Operating System :: Unix",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Topic :: Software Development :: Quality Assurance",
        "Topic :: Utilities",
    ],
    project_urls={
        "Issue Tracker": "https://github.com/Vauxoo/tgrep-deployv/issues",
    },
    keywords=[
        "odoo",
        "tgrep",
        "grep",
        "trigram",
        "search",
        "vauxoo",
    ],
    python_requires=">=3.6",
    install_requires=generate_dependencies(),
    entry_points={
        "console_scripts": [
            "tgrep-deployv = tgrep_deployv.cli:main",
        ]
    },
)
