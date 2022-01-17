import logging
import os
import re
import shutil
import sys
from contextlib import contextmanager
from copy import deepcopy
from distutils import core
from pathlib import Path
from subprocess import check_output
from tempfile import mkdtemp
from typing import Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import requests
from colorama import Fore, Style

from grayskull.cli.stdout import manage_progressbar, print_msg
from grayskull.config import Configuration
from grayskull.license.discovery import ShortLicense, search_license_file
from grayskull.utils import (
    PyVer,
    get_vendored_dependencies,
    origin_is_github,
    sha256_checksum,
)

log = logging.getLogger(__name__)
RE_DEPS_NAME = re.compile(r"^\s*([\.a-zA-Z0-9_-]+)", re.MULTILINE)
PIN_PKG_COMPILER = {"numpy": "<{ pin_compatible('numpy') }}"}


def search_setup_root(path_folder: Union[Path, str]) -> Path:
    setup_py = list(Path(path_folder).rglob("setup.py"))
    if setup_py:
        return setup_py[0]
    setup_cfg = list(Path(path_folder).rglob("setup.cfg"))
    if setup_cfg:
        return setup_cfg[0]
    pyproject_toml = list(Path(path_folder).rglob("pyproject.toml"))
    if pyproject_toml:
        return pyproject_toml[0]


def clean_deps_for_conda_forge(list_deps: List, py_ver_min: PyVer) -> List:
    """Remove dependencies which conda-forge is not supporting anymore.
    For example Python 2.7, Python version less than 3.6"""
    re_delimiter = re.compile(r"#\s+\[py\s*(?:([<>=!]+))?\s*(\d+)\]\s*$", re.DOTALL)
    result_deps = []
    for dependency in list_deps:
        match_del = re_delimiter.search(dependency)
        if match_del is None:
            result_deps.append(dependency)
            continue

        match_del = match_del.groups()
        if not match_del[0]:
            match_del = ("==", match_del[1])
        major = int(match_del[1][0])
        minor = int(match_del[1][1:].replace("k", "0") or 0)
        current_py = PyVer(major=major, minor=minor)
        log.debug(f"Evaluating: {py_ver_min}{match_del}{current_py} -- {dependency}")
        if eval(f"py_ver_min{match_del[0]}current_py"):
            result_deps.append(dependency)
    return result_deps


def pkg_name_from_sdist_url(sdist_url: str):
    """This method extracts and returns the name of the package from the sdist
    url."""
    if origin_is_github(sdist_url):
        return sdist_url.split("/")[-3] + ".tar.gz"
    else:
        return sdist_url.split("/")[-1]


def parse_extra_metadata_to_selector(option: str, operation: str, value: str) -> str:
    """Method tries to convert the extra metadata received into selectors

    :param option: option (extra, python_version, sys_platform)
    :param operation: (>, >=, <, <=, ==, !=)
    :param value: value after the operation
    :return: selector
    """
    if option == "extra":
        return ""
    if option == "python_version":
        value = value.split(".")
        value = "".join(value[:2])
        return f"py{operation}{value}"
    if option == "sys_platform":
        value = re.sub(r"[^a-zA-Z]+", "", value)
        if operation == "!=":
            return f"not {value.lower()}"
        return value.lower()
    if option == "platform_system":
        replace_val = {"windows": "win", "linux": "linux", "darwin": "osx"}
        value_lower = value.lower().strip()
        if value_lower in replace_val:
            value_lower = replace_val[value_lower]
        if operation == "!=":
            return f"not {value_lower}"
        return value_lower


def get_extra_from_requires_dist(string_parse: str) -> Union[List]:
    """Receives the extra metadata e parse it to get the option, operation
    and value.

    :param string_parse: metadata extra
    :return: return the option , operation and value of the extra metadata
    """
    return re.findall(
        r"(?:(\())?\s*([\.a-zA-Z0-9-_]+)\s*([=!<>]+)\s*[\'\"]*"
        r"([\.a-zA-Z0-9-_]+)[\'\"]*\s*(?:(\)))?\s*(?:(and|or))?",
        string_parse,
    )


def get_name_version_from_requires_dist(string_parse: str) -> Tuple[str, str]:
    """Extract the name and the version from `requires_dist` present in
    PyPi`s metadata

    :param string_parse: requires_dist value from PyPi metadata
    :return: Name and version of a package
    """
    pkg = re.match(r"^\s*([^\s]+)\s*([\(]*.*[\)]*)?\s*", string_parse, re.DOTALL)
    pkg_name = pkg.group(1).strip()
    version = ""
    if len(pkg.groups()) > 1 and pkg.group(2):
        version = " " + pkg.group(2).strip()
    return pkg_name.strip(), re.sub(r"[\(\)]", "", version).strip()


def generic_py_ver_to(
    metadata: dict, config, is_selector: bool = False
) -> Optional[str]:  # sourcery no-metrics
    """Generic function which abstract the parse of the requires_python
    present in the PyPi metadata. Basically it can generate the selectors
    for Python or the constrained version if it is a `noarch: python` python package"""
    if not metadata.get("requires_python"):
        return None
    req_python = re.findall(
        r"([><=!]+)\s*(\d+)(?:\.(\d+))?",
        metadata["requires_python"],
    )
    if not req_python:
        return None

    py_ver_enabled = config.get_py_version_available(req_python)
    small_py3_version = config.get_oldest_py3_version(list(py_ver_enabled.keys()))
    all_py = list(py_ver_enabled.values())
    if all(all_py):
        return None
    if all(all_py if config.is_strict_cf else all_py[1:]):
        if is_selector:
            return None if config.is_strict_cf else "# [py2k]"
        else:
            return f">={small_py3_version.major}.{small_py3_version.minor}"
    if py_ver_enabled.get(PyVer(2, 7)) and any(all_py[1:]) is False:
        return "# [py3k]" if is_selector else "<3.0"

    for pos, py_ver in enumerate(py_ver_enabled):
        if py_ver == PyVer(2, 7):
            continue
        if all(all_py[pos:]) and any(all_py[:pos]) is False:
            return (
                f"# [py<{py_ver.major}{py_ver.minor}]"
                if is_selector
                else f">={py_ver.major}.{py_ver.minor}"
            )
        elif any(all_py[pos:]) is False:
            if is_selector:
                py2k = ""
                if not config.is_strict_cf and not all_py[0]:
                    py2k = " or py2k"
                return f"# [py>={py_ver.major}{py_ver.minor}{py2k}]"
            else:
                py2 = ""
                if not all_py[0]:
                    py2 = f">={small_py3_version.major}.{small_py3_version.minor},"
                return f"{py2}<{py_ver.major}.{py_ver.minor}"

    all_selector = get_py_multiple_selectors(
        py_ver_enabled, is_selector=is_selector, config=config
    )
    if all_selector:
        return (
            "# [{}]".format(" or ".join(all_selector))
            if is_selector
            else ",".join(all_selector)
        )
    return None


def install_deps_if_necessary(setup_path: str, data_dist: dict, pip_dir: str):
    """Install missing dependencies to run the setup.py

    :param setup_path: path to the setup.py
    :param data_dist: metadata
    :param pip_dir: path where the missing packages will be downloaded
    """
    all_setup_deps = get_vendored_dependencies(setup_path)
    for dep in all_setup_deps:
        pip_install_dep(data_dist, dep, pip_dir)


def pip_install_dep(data_dist: dict, dep_name: str, pip_dir: str):
    """Install dependency using `pip`

    :param data_dist: sdist metadata
    :param dep_name: Package name which will be installed
    :param pip_dir: Path to the folder where `pip` will let the packages
    """
    if not data_dist.get("setup_requires"):
        data_dist["setup_requires"] = []
    if dep_name == "pkg_resources":
        dep_name = "setuptools"
    try:
        check_output(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                dep_name,
                f"--target={pip_dir}",
            ]
        )
    except Exception as err:
        log.error(
            f"It was not possible to install {dep_name}.\n"
            f"Command: pip install {dep_name} --target={pip_dir}.\n"
            f"Error: {err}"
        )
    else:
        if (
            dep_name.lower() not in data_dist["setup_requires"]
            and dep_name.lower() != "setuptools"
        ):
            data_dist["setup_requires"].append(dep_name.lower())


def merge_sdist_metadata(setup_py: dict, setup_cfg: dict) -> dict:
    """This method will merge the metadata present in the setup.py and
    setup.cfg. It is an auxiliary method.

    :param setup_py: Metadata from setup.py
    :param setup_cfg: Metadata from setup.cfg
    :return: Return the merged data from setup.py and setup.cfg
    """
    result = deepcopy(setup_py)
    for key, value in setup_cfg.items():
        if key not in result:
            result[key] = value

    def get_full_list(sec_key: str) -> List:
        if sec_key not in setup_py:
            return setup_cfg.get(sec_key, [])
        cfg_val = set(setup_cfg.get(sec_key, []))
        result_val = set(result.get(sec_key, []))
        return list(cfg_val.union(result_val))

    if "install_requires" in result:
        result["install_requires"] = get_full_list("install_requires")
    if "extras_require" in result:
        result["extras_require"] = get_full_list("extras_require")
    if "setup_requires" in result:
        result["setup_requires"] = get_full_list("setup_requires")
        if "setuptools-scm" in result["setup_requires"]:
            result["setup_requires"].remove("setuptools-scm")
    if "compilers" in result:
        result["compilers"] = get_full_list("compilers")
    return result


def get_setup_cfg(source_path: str) -> dict:
    """Method responsible to extract the setup.cfg metadata

    :param source_path: Path to the folder where is located the sdist
     files unpacked
    :return: Metadata of setup.cfg
    """
    from setuptools.config import read_configuration

    log.debug(f"Started setup.cfg from {source_path}")
    print_msg("Recovering metadata from setup.cfg")
    path_setup_cfg = list(Path(source_path).rglob("setup.cfg"))
    if not path_setup_cfg:
        return {}
    path_setup_cfg = path_setup_cfg[0]

    setup_cfg = read_configuration(str(path_setup_cfg))
    setup_cfg = dict(setup_cfg)
    if setup_cfg.get("options", {}).get("python_requires"):
        setup_cfg["options"]["python_requires"] = ensure_pep440(
            str(setup_cfg["options"]["python_requires"])
        )
    result = {}
    result.update(setup_cfg.get("options", {}))
    result.update(setup_cfg.get("metadata", {}))
    if result.get("build_ext"):
        result["compilers"] = ["c"]
    log.debug(f"Data recovered from setup.cfg: {result}")
    return result


@contextmanager
def injection_distutils(folder: str) -> dict:
    """This is a bit of "dark magic", please don't do it at home.
    It is injecting code in the distutils.core.setup and replacing the
    setup function by the inner function __fake_distutils_setup.
    This method is a contextmanager, after leaving the context it will return
    with the normal implementation of the distutils.core.setup.
    This method is necessary for two reasons:
    -pypi metadata for some packages might be absent from the pypi API.
    -pypi metadata, when present, might be missing some information.

    :param folder: Path to the folder where the sdist package was extracted
    :yield: return the metadata from sdist
    """
    from distutils import core

    setup_core_original = core.setup
    old_dir = os.getcwd()
    path_setup = search_setup_root(folder)
    os.chdir(os.path.dirname(str(path_setup)))

    data_dist = {}

    def __fake_distutils_setup(*args, **kwargs):
        if not isinstance(kwargs, dict) or not kwargs:
            return

        def _fix_list_requirements(key_deps: str) -> List:
            """Fix when dependencies have lists inside of another sequence"""
            val_deps = kwargs.get(key_deps)
            if not val_deps:
                return val_deps
            list_req = []
            if isinstance(val_deps, str):
                val_deps = [val_deps]
            for val in val_deps:
                if isinstance(val, (tuple, list)):
                    list_req.extend(list(map(str, val)))
                else:
                    list_req.append(str(val))
            return list_req

        if "setup_requires" in kwargs:
            kwargs["setup_requires"] = _fix_list_requirements("setup_requires")
        if "install_requires" in kwargs:
            kwargs["install_requires"] = _fix_list_requirements("install_requires")

        data_dist.update(kwargs)
        if not data_dist.get("setup_requires"):
            data_dist["setup_requires"] = []
        data_dist["setup_requires"] += (
            kwargs.get("setup_requires") if kwargs.get("setup_requires") else []
        )

        if "use_scm_version" in data_dist and kwargs["use_scm_version"]:
            log.debug("setuptools_scm found on setup.py")
            if "setuptools_scm" not in data_dist["setup_requires"]:
                data_dist["setup_requires"] += ["setuptools_scm"]
            if "setuptools-scm" in data_dist["setup_requires"]:
                data_dist["setup_requires"].remove("setuptools-scm")

        if kwargs.get("ext_modules", None):
            data_dist["compilers"] = ["c"]
            if len(kwargs["ext_modules"]) > 0:
                for ext_mod in kwargs["ext_modules"]:
                    if (
                        hasattr(ext_mod, "has_f2py_sources")
                        and ext_mod.has_f2py_sources()
                    ):
                        data_dist["compilers"].append("fortran")
                        break
        log.debug(f"Injection distutils all arguments: {kwargs}")
        if data_dist.get("run_py", False):
            del data_dist["run_py"]
            return
        setup_core_original(*args, **kwargs)

    try:
        core.setup = __fake_distutils_setup
        path_setup = str(path_setup)
        print_msg("Executing injected distutils...")
        __run_setup_py(path_setup, data_dist)
        if not data_dist or not data_dist.get("install_requires", None):
            print_msg(
                "No data was recovered from setup.py."
                " Forcing to execute the setup.py as script"
            )
            __run_setup_py(path_setup, data_dist, run_py=True)
        yield data_dist
    except BaseException as err:
        log.debug(f"Exception occurred when executing sdist injection: {err}")
        yield data_dist
    finally:
        core.setup = setup_core_original
        os.chdir(old_dir)


def __run_setup_py(path_setup: str, data_dist: dict, run_py=False, deps_installed=None):
    """Method responsible to run the setup.py

    :param path_setup: full path to the setup.py
    :param data_dist: metadata
    :param run_py: If it should run the setup.py with run_py, otherwise it will run
    invoking the distutils directly
    """
    deps_installed = deps_installed or []
    original_path = deepcopy(sys.path)
    pip_dir = mkdtemp(prefix="pip-dir-")
    if not os.path.exists(pip_dir):
        os.mkdir(pip_dir)
    if os.path.dirname(path_setup) not in sys.path:
        sys.path.append(os.path.dirname(path_setup))
        sys.path.append(pip_dir)
    install_deps_if_necessary(path_setup, data_dist, pip_dir)
    try:
        if run_py:
            import runpy

            data_dist["run_py"] = True
            runpy.run_path(path_setup, run_name="__main__")
        else:
            core.run_setup(path_setup, script_args=["install", f"--target={pip_dir}"])
    except ModuleNotFoundError as err:
        log.debug(
            f"When executing setup.py did not find the module: {err.name}."
            f" Exception: {err}"
        )
        dep_install = err.name
        if dep_install in deps_installed:
            dep_install = dep_install.split(".")[0]
        if dep_install not in deps_installed:
            deps_installed.append(dep_install)
            pip_install_dep(data_dist, dep_install, pip_dir)
            __run_setup_py(path_setup, data_dist, run_py, deps_installed=deps_installed)
    except Exception as err:
        log.debug(f"Exception when executing setup.py as script: {err}")
    data_dist.update(
        merge_sdist_metadata(data_dist, get_setup_cfg(os.path.dirname(str(path_setup))))
    )
    log.debug(f"Data recovered from setup.py: {data_dist}")
    if os.path.exists(pip_dir):
        shutil.rmtree(pip_dir)
    sys.path = original_path


def get_compilers(
    requires_dist: List, sdist_metadata: dict, config: Configuration
) -> List:
    """Return which compilers are necessary"""
    compilers = set(sdist_metadata.get("compilers", []))
    for pkg in requires_dist:
        pkg = RE_DEPS_NAME.match(pkg).group(0)
        pkg = pkg.lower().strip()
        if pkg.strip() in config.pkg_need_c_compiler:
            compilers.add("c")
        if pkg.strip() in config.pkg_need_cxx_compiler:
            compilers.add("cxx")
    return list(compilers)


def get_py_multiple_selectors(
    selectors: Dict[PyVer, bool],
    config: Configuration,
    is_selector: bool = False,
) -> List:
    """Get python selectors available.

    :param selectors: Dict with the Python version and if it is selected
    :param is_selector: if it needs to convert to selector or constrain python
    :param config: Configuration object
    :return: list with all selectors or constrained python
    """
    all_selector = []
    if not config.is_strict_cf and selectors[PyVer(2, 7)] is False:
        all_selector += (
            ["py2k"]
            if is_selector
            else config.get_oldest_py3_version(list(selectors.keys()))
        )
    for py_ver, is_enabled in selectors.items():
        if (not config.is_strict_cf and py_ver == PyVer(2, 7)) or is_enabled:
            continue
        all_selector += (
            [f"py=={py_ver.major}{py_ver.minor}"]
            if is_selector
            else [f"!={py_ver.major}.{py_ver.minor}"]
        )
    return all_selector


def py_version_to_selector(pypi_metadata: dict, config) -> Optional[str]:
    return generic_py_ver_to(pypi_metadata, is_selector=True, config=config)


def py_version_to_limit_python(pypi_metadata: dict, config=None) -> Optional[str]:
    config = config or Configuration()
    result = generic_py_ver_to(pypi_metadata, is_selector=False, config=config)
    if not result and config.is_strict_cf:
        result = (
            f">={config.py_cf_supported[0].major}.{config.py_cf_supported[0].minor}"
        )
    return result


def update_requirements_with_pin(requirements: dict):
    """Get a dict with the `host`, `run` and `build` in it and replace
    if necessary the run requirements with the appropriated pin.

    :param requirements: Dict with the requirements in it
    """

    def is_compiler_present() -> bool:
        if "build" not in requirements:
            return False
        re_compiler = re.compile(
            r"^\s*[<{]\{\s*compiler\(['\"]\w+['\"]\)\s*\}\}\s*$", re.MULTILINE
        )
        for build in requirements["build"]:
            if re_compiler.match(build):
                return True
        return False

    if not is_compiler_present():
        return
    for pkg in requirements["host"]:
        pkg_name = RE_DEPS_NAME.match(pkg).group(0)
        if pkg_name in PIN_PKG_COMPILER.keys():
            if pkg_name in requirements["run"]:
                requirements["run"].remove(pkg_name)
            requirements["run"].append(PIN_PKG_COMPILER[pkg_name])


def discover_license(metadata: dict) -> Optional[ShortLicense]:
    """Based on the metadata this method will try to discover what is the
    right license for the package

    :param metadata: metadata
    :return: Return an object which contains relevant information regarding
    the license.
    """
    git_url = metadata.get("dev_url")
    if not git_url and urlparse(metadata.get("project_url", "")).netloc == "github.com":
        git_url = metadata.get("project_url")
    # "url" is always present but sometimes set to None
    if not git_url and urlparse((metadata.get("url") or "")).netloc == "github.com":
        git_url = metadata.get("url")

    short_license = search_license_file(
        metadata.get("sdist_path"),
        git_url,
        metadata.get("version"),
        license_name_metadata=metadata.get("license"),
    )
    if short_license:
        return short_license


def get_test_entry_points(entry_points: Union[List, str]) -> List:
    if entry_points and isinstance(entry_points, str):
        entry_points = [entry_points]
    return [f"{ep.split('=')[0].strip()} --help" for ep in entry_points]


def get_test_imports(metadata: dict, default: Optional[str] = None) -> List:
    if default:
        default = default.replace("-", "_")
    if "packages" not in metadata or not metadata["packages"]:
        return [default]
    meta_pkg = metadata["packages"]
    if isinstance(meta_pkg, str):
        meta_pkg = [metadata["packages"]]
    result = []
    for module in sorted(meta_pkg):
        if "/" in module or "." in module or module.startswith("_"):
            continue
        if module in ["test", "tests"]:
            log.warning(
                f"The package wrongfully added the test folder as a module ({module}),"
                f" as a result that might result in conda clobber warnings."
            )
            continue
        result.append(module)
    if not result:
        return [impt.replace("/", ".") for impt in sorted(meta_pkg)[:2]]
    return result


def get_entry_points_from_sdist(sdist_metadata: dict) -> List:
    """Extract entry points from sdist metadata

    :param sdist_metadata: sdist metadata
    :return: list with all entry points
    """
    all_entry_points = sdist_metadata.get("entry_points", {})
    if isinstance(all_entry_points, str) or not all_entry_points:
        return []
    if all_entry_points.get("console_scripts") or all_entry_points.get("gui_scripts"):
        console_scripts = all_entry_points.get("console_scripts", [])
        gui_scripts = all_entry_points.get("gui_scripts", [])
        entry_points_result = []
        if console_scripts:
            if isinstance(console_scripts, str):
                console_scripts = [console_scripts]
            entry_points_result += console_scripts
        if gui_scripts:
            if isinstance(gui_scripts, str):
                gui_scripts = [gui_scripts]
            entry_points_result += gui_scripts
        return_entry_point = []
        for entry_point in entry_points_result:
            if isinstance(entry_point, str):
                entry_point = entry_point.split("\n")
            return_entry_point.extend(entry_point)
        return [ep for ep in return_entry_point if ep.strip()]
    return []


def download_sdist_pkg(sdist_url: str, dest: str, name: Optional[str] = None):
    """Download the sdist package

    :param sdist_url: sdist url
    :param dest: Folder were the method will download the sdist
    """
    print_msg(
        f"{Fore.GREEN}Starting the download of the sdist package"
        f" {Fore.BLUE}{Style.BRIGHT}{name}"
    )
    log.debug(f"Downloading {name} sdist - {sdist_url}")
    response = requests.get(sdist_url, allow_redirects=True, stream=True, timeout=5)
    response.raise_for_status()
    total_size = int(response.headers.get("Content-length", 0))
    with manage_progressbar(max_value=total_size, prefix=f"{name} ") as bar, open(
        dest, "wb"
    ) as pkg_file:
        progress_val = 0
        chunk_size = 512
        for chunk_data in response.iter_content(chunk_size=chunk_size):
            if chunk_data:
                pkg_file.write(chunk_data)
                progress_val += chunk_size
                bar.update(min(progress_val, total_size))


def get_sdist_metadata(
    sdist_url: str, config: Configuration, with_source: bool = False
) -> dict:
    """Method responsible to return the sdist metadata which is basically
    the metadata present in setup.py and setup.cfg
    :param sdist_url: URL to the sdist package
    :param config: package configuration
    :param with_source: a boolean value to indicate Github packages
    :return: sdist metadata
    """
    temp_folder = mkdtemp(prefix=f"grayskull-{config.name}-")
    pkg_name = pkg_name_from_sdist_url(sdist_url)
    path_pkg = os.path.join(temp_folder, pkg_name)

    download_sdist_pkg(sdist_url=sdist_url, dest=path_pkg, name=config.name)
    if config.download:
        config.files_to_copy.append(path_pkg)
    log.debug(f"Unpacking {path_pkg} to {temp_folder}")
    shutil.unpack_archive(path_pkg, temp_folder)
    print_msg("Recovering information from setup.py")
    with injection_distutils(temp_folder) as metadata:
        metadata["sdist_path"] = temp_folder

    # At this point the tarball was successfully extracted
    # so we can assume the sha256 can be computed reliably
    if with_source:
        metadata["source"] = {"url": sdist_url, "sha256": sha256_checksum(path_pkg)}

    return metadata


def ensure_pep440_in_req_list(list_req: List[str]) -> List[str]:
    return [ensure_pep440(pkg) for pkg in list_req]


def ensure_pep440(pkg: str) -> str:
    if not pkg:
        return pkg
    if pkg.strip().startswith("<{") or pkg.strip().startswith("{{"):
        return pkg
    split_pkg = pkg.strip().split(" ")
    if len(split_pkg) <= 1:
        return pkg
    constrain_pkg = "".join(split_pkg[1:])
    list_constrains = constrain_pkg.split(",")
    full_constrain = []
    for constrain in list_constrains:
        if "~=" in constrain:
            version = constrain.strip().replace("~=", "").strip()
            version_reduced = ".".join(version.split(".")[:-1])
            version_reduced += ".*"
            full_constrain.append(f">={version},=={version_reduced}")
        else:
            full_constrain.append(constrain.strip())
    all_constrains = ",".join(full_constrain)
    return f"{split_pkg[0]} {all_constrains}"