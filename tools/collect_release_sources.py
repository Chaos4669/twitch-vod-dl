"""Collect release materials inside the exact Linux image being distributed."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import file_digest
from importlib import metadata
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tarfile
import time
import tomllib
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import zipfile
import zlib


APORTS = "https://raw.githubusercontent.com/alpinelinux/aports"
DISTFILES = "https://distfiles.alpinelinux.org/distfiles"
SPDX = "https://raw.githubusercontent.com/spdx/license-list-data/v3.26.0/text"
NOTICE = re.compile(r"^(?:licen[cs]es?|copying|copyright|notice|patents)(?:$|[-_.])", re.I)
HEADER = re.compile(rb"/\*.*?\*/", re.DOTALL)
ATTRIBUTION = re.compile(rb"copyright|SPDX-License-Identifier|Permission is hereby granted", re.I)
RECIPE_VARS = 'printf "%s\\0" "$pkgver" "$pkgrel" "$source" "$sha512sums" "$sha256sums" "$install" "$triggers"'


def retrieve(url, path, *, digest=None):
    if path.is_file() and (digest is None or checksum(path, digest[0]) == digest[1]):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "twitch-vod-dl-source-release"}), timeout=35) as response:
                with temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
            if digest and checksum(temporary, digest[0]) != digest[1]:
                raise ValueError(f"Checksum mismatch for {url}")
            temporary.replace(path)
            return
        except HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise
            if attempt == 2:
                raise
        except (OSError, ValueError):
            if attempt == 2:
                raise
        finally:
            temporary.unlink(missing_ok=True)
        time.sleep(attempt + 1)


def checksum(path, algorithm="sha256"):
    with path.open("rb") as stream:
        return file_digest(stream, algorithm).hexdigest()


def installed_apk():
    for entry in Path("/lib/apk/db/installed").read_text().strip().split("\n\n"):
        fields = dict(line.split(":", 1) for line in entry.splitlines() if line and line[0] in "PVoLcAU")
        yield dict(zip(("name", "version", "origin", "license", "commit", "arch", "homepage"),
                       (fields[key] for key in "PVoLcAU"), strict=True))


def source_name(spec):
    name, alias, url = spec.partition("::")
    return (name, url) if alias else (Path(urlsplit(name).path).name, name)


def download_apk_source(root, branch, origin, version, commit):
    directory = root / "sources" / "alpine" / f"{origin}-{version}"
    recipe = directory / "APKBUILD"
    for section in ("main", "community"):
        base = f"{APORTS}/{commit}/{section}/{origin}"
        try:
            if recipe.is_file():
                base = (directory / "APORTS-URL.txt").read_text().strip()
                break
            retrieve(f"{base}/APKBUILD", recipe)
            (directory / "APORTS-URL.txt").write_text(base + "\n")
            break
        except HTTPError as error:
            if error.code != 404 or section == "community":
                raise

    command = f'. ./APKBUILD\n{RECIPE_VARS}'
    result = subprocess.run(
        ["/bin/sh", "-ec", command], cwd=directory, capture_output=True, check=True,
        env={"PATH": "/usr/bin:/bin", "CARCH": "x86_64", "CBUILD_ARCH": "x86_64",
             "CARCH_LIBC": "musl", "CTARGET_ARCH": "x86_64", "startdir": str(directory)},
    )
    pkgver, pkgrel, sources, sha512, sha256, install, triggers, _ = result.stdout.decode().split("\0")
    if f"{pkgver}-r{pkgrel}" != version:
        raise ValueError(f"APKBUILD version {pkgver}-r{pkgrel} does not match {origin} {version}")
    hashes = {}
    for hashes_text, algorithm in ((sha256, "sha256"), (sha512, "sha512")):
        for line in hashes_text.strip().splitlines():
            value, name = line.split(None, 1)
            if value != "SKIP":
                hashes[name] = (algorithm, value)
    for spec in sources.split():
        name, url = source_name(spec)
        digest = hashes.get(name)
        if not digest:
            raise ValueError(f"No Alpine checksum for {origin}/{name}")
        destination = directory / name
        if not urlsplit(url).scheme:
            retrieve(f"{base}/{quote(url, safe='/')}", destination, digest=digest)
            continue
        for candidate in (f"{DISTFILES}/{branch}/{quote(name)}", url):
            try:
                retrieve(candidate, destination, digest=digest)
                break
            except (OSError, ValueError):
                if candidate == url:
                    raise
    for filename in set(install.split() + [entry.split("=", 1)[0] for entry in triggers.split()]):
        retrieve(f"{base}/{quote(filename, safe='/')}", directory / filename)
    return directory


def download_python_source(root, name, version):
    project = json.load(urlopen(f"https://pypi.org/pypi/{quote(name)}/{quote(version)}/json", timeout=35))
    source, = (item for item in project["urls"] if item["packagetype"] == "sdist")
    directory = root / "sources" / "python" / f"{name}-{version}"
    retrieve(source["url"], directory / source["filename"], digest=("sha256", source["digests"]["sha256"]))
    return directory


def copy_notice(root, name, stream):
    parts = Path(name).parts
    if any(part in ("..", "/") for part in parts):
        raise ValueError(f"Invalid notice filename {name!r}")
    target = root.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as output:
        shutil.copyfileobj(stream, output)


def is_notice(path):
    return bool(NOTICE.match(path.name)) or any(part.lower() == "licenses" for part in path.parts[:-1])


def collect_notices(directory, dest):
    for source in sorted(directory.iterdir()):
        if NOTICE.match(source.name):
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest / source.name)
        elif source.name.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tar", ".crate")):
            with tarfile.open(source, "r|*") as archive:
                for entry in archive:
                    if entry.isfile() and is_notice(Path(entry.name)):
                        copy_notice(dest / source.name, entry.name, archive.extractfile(entry))
        elif source.suffix == ".zip":
            with zipfile.ZipFile(source) as archive:
                for entry in archive.infolist():
                    if not entry.is_dir() and is_notice(Path(entry.filename)):
                        with archive.open(entry) as stream:
                            copy_notice(dest / source.name, entry.filename, stream)


def supplement_notices(root, origins):
    alpine = root / "licenses" / "alpine"
    texts = root / "licenses" / "license-texts"
    for identifier in ("GPL-2.0-only", "MIT", "MPL-2.0"):
        retrieve(f"{SPDX}/{identifier}.txt", texts / f"{identifier}.txt")

    packages = {origin: f"{origin}-{version}" for origin, version, commit in origins}
    for origin, identifiers in {
        "alpine-base": ("MIT",),
        "alpine-baselayout": ("GPL-2.0-only",),
        "alpine-keys": ("MIT",),
        "ca-certificates": ("MIT", "MPL-2.0"),
        "libdrm": ("MIT",),
    }.items():
        if origin not in packages:
            continue
        package = packages[origin]
        destination = alpine / package
        destination.mkdir(parents=True, exist_ok=True)
        for identifier in identifiers:
            shutil.copy2(texts / f"{identifier}.txt", destination / f"{identifier}.txt")
        shutil.copy2(root / "sources" / "alpine" / package / "APKBUILD", destination / "APKBUILD")

        if origin in ("ca-certificates", "libdrm"):
            for source in (root / "sources" / "alpine" / package).glob("*.tar.*"):
                with tarfile.open(source, "r|*") as archive:
                    for entry in archive:
                        if not entry.isfile() or Path(entry.name).suffix not in (".c", ".h", ".cpp", ".txt"):
                            continue
                        initial = archive.extractfile(entry).read(8192)
                        notices = [block for block in HEADER.findall(initial) if ATTRIBUTION.search(block)]
                        if origin == "ca-certificates" and Path(entry.name).name == "certdata.txt":
                            notices.append(initial.split(b"# certdata.txt", 1)[0].strip())
                        if notices:
                            copy_notice(destination / "source-notices", entry.name + ".txt",
                                        BytesIO(b"\n\n".join(notices) + b"\n"))


def rust_metadata(binary):
    # cargo-auditable embeds zlib-compressed JSON under .dep-v0 in ELF images.
    data = binary.read_bytes()
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError(f"Expected a 64-bit little-endian ELF binary: {binary}")
    section_offset, = struct.unpack_from("<Q", data, 40)
    size, count, names_index = struct.unpack_from("<HHH", data, 58)
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, section_offset + size * index)
                for index in range(count)]
    names_start, names_size = sections[names_index][4:6]
    names = data[names_start:names_start + names_size]
    for number, _, _, _, offset, length, *_ in sections:
        if names[number:].split(b"\0", 1)[0] == b".dep-v0":
            return json.loads(zlib.decompress(data[offset:offset + length]))["packages"]
    raise ValueError(f"Missing cargo-auditable metadata: {binary}")


def collect_rust_sources(root, origins):
    audited = {}
    crates = {}
    for origin, library in (("libdovi", "libdovi"), ("rav1e", "librav1e")):
        for name, version, _ in origins:
            if name != origin:
                continue
            binary = next(path for path in Path("/usr/lib").glob(library + ".so.*") if not path.is_symlink())
            audited[binary.name] = rust_metadata(binary)
            locked = {}
            for source in (root / "sources" / "alpine" / f"{origin}-{version}").glob("*.tar.*"):
                with tarfile.open(source, "r|*") as archive:
                    for entry in archive:
                        if entry.isfile() and Path(entry.name).name == "Cargo.lock":
                            content = archive.extractfile(entry).read().decode()
                            for package in tomllib.loads(content)["package"]:
                                if package.get("checksum"):
                                    locked[package["name"], package["version"]] = package["checksum"]
            for package in audited[binary.name]:
                if package["source"] == "local":
                    continue
                key = package["name"], package["version"]
                if package["source"] != "crates.io" or key not in locked:
                    raise ValueError(f"Missing Cargo.lock checksum for {binary.name}: {key}")
                if crates.setdefault(key, locked[key]) != locked[key]:
                    raise ValueError(f"Conflicting Cargo.lock checksums for {key}")

    def collect_one(item):
        (name, version), digest = item
        crate = f"{name}-{version}"
        directory = root / "sources" / "rust" / crate
        archive = directory / f"{crate}.crate"
        retrieve(f"https://static.crates.io/crates/{quote(name)}/{quote(crate)}.crate",
                 archive, digest=("sha256", digest))
        notices = root / "licenses" / "rust" / crate
        collect_notices(directory, notices)
        if name in ("profiling", "profiling-procmacros", "simd_helpers"):
            with tarfile.open(archive) as source:
                manifest = source.extractfile(f"{crate}/Cargo.toml").read()
                vcs = json.load(source.extractfile(f"{crate}/.cargo_vcs_info.json"))
            if name == "simd_helpers":
                notices.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / "licenses" / "license-texts" / "MIT.txt", notices / "MIT.txt")
                (notices / "Cargo.toml").write_bytes(manifest)
            else:
                project = tomllib.loads(manifest.decode())["package"]["repository"].removeprefix("https://github.com/")
                commit = vcs["git"]["sha1"]
                for filename in ("LICENSE-MIT", "LICENSE-APACHE"):
                    retrieve(f"https://raw.githubusercontent.com/{project}/{commit}/{filename}", notices / filename)
        if not any(entry.is_file() for entry in notices.rglob("*")):
            raise ValueError(f"No notice found for Rust crate {crate}")

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(collect_one, sorted(crates.items())))
    inventory = root / "inventory.json"
    recorded = json.loads(inventory.read_text())
    recorded["rust"] = audited
    inventory.write_text(json.dumps(recorded, indent=2) + "\n")


def package_release(root, image):
    inventory = json.loads((root / "inventory.json").read_text())
    if inventory["image"] != image:
        raise ValueError("Inventory does not match the requested image digest")
    packages = {
        "alpine": {f'{item["origin"]}-{item["version"]}' for item in inventory["alpine"]},
        "python": {item["name"] for item in inventory["python"]},
        "rust": {f'{item["name"]}-{item["version"]}' for entries in inventory["rust"].values()
                 for item in entries if item["source"] == "crates.io"},
    }
    for ecosystem, names in packages.items():
        for name in names:
            if not any(file.is_file() for file in (root / "licenses" / ecosystem / name).rglob("*")):
                raise ValueError(f"Missing license material for {ecosystem}/{name}")
    if any(root.joinpath("sources").rglob("*.part")):
        raise ValueError("Incomplete source downloads remain")
    readme = root / "README.txt"
    readme.write_text(
        f"Source and license materials for twitch-vod-dl\nImage digest: {image}\nPlatform: linux/amd64\n\n"
        "inventory.json lists the packages in the image and their source versions.\n"
        "sources/alpine contains the exact aports recipes, patches and source files.\n"
        "sources/python contains the matching PyPI source distributions.\n"
        "sources/rust contains the exact crates recorded in the two native Rust libraries.\n"
        "sources/twitch-vod-dl contains the application sources and Dockerfile.\n"
        "licenses contains the collected original license and copyright texts.\n",
        encoding="utf-8",
    )
    contents = [readme, root / "inventory.json", root / "licenses", root / "sources"]
    archive = root / f"twitch-vod-dl-sources-{image.removeprefix('sha256:')}-linux-amd64.tar.gz"
    temporary = archive.with_name(archive.name + ".part")
    with tarfile.open(temporary, "w:gz", compresslevel=6) as output:
        for path in contents:
            output.add(path, arcname=path.name)
    temporary.replace(archive)
    archive.with_name(archive.name + ".sha256").write_text(
        f"{checksum(archive)}  {archive.name}\n", encoding="ascii", newline="\n",
    )


def copy_project(root):
    project = Path(__file__).resolve().parents[1]
    for filename in ("downloader.py", "manual_download.py", "entrypoint.sh", "tdl.sh", "LICENSE"):
        installed = Path("/usr/local/bin") / ("tdl" if filename == "tdl.sh" else filename)
        if filename == "LICENSE":
            installed = Path("/usr/share/licenses/twitch-vod-dl/LICENSE")
        if checksum(project / filename) != checksum(installed):
            raise ValueError(f"{filename} differs between the checkout and the image")

    target = root / "sources" / "twitch-vod-dl"
    target.mkdir(parents=True, exist_ok=True)
    for filename in ("Dockerfile", ".dockerignore", ".gitignore", "LICENSE", "README.md", "downloader.py",
                     "manual_download.py", "entrypoint.sh", "tdl.sh"):
        shutil.copy2(project / filename, target / filename)
    for directory in ("tests", "tools"):
        shutil.copytree(project / directory, target / directory, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    licenses = root / "licenses" / "twitch-vod-dl"
    licenses.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project / "LICENSE", licenses / "LICENSE")


def run(args):
    root = args.output
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image):
        raise ValueError("Expected the full sha256 image digest")
    inventory_path = root / "inventory.json"
    if inventory_path.exists() and json.loads(inventory_path.read_text())["image"] != args.image:
        raise ValueError("Use a new output directory for a different image")
    copy_project(root)
    packages = sorted(installed_apk(), key=lambda item: item["name"])
    distributions = sorted(metadata.distributions(path=list(map(str, Path("/opt/venv/lib").glob("python*/site-packages")))),
                           key=lambda dist: dist.metadata["Name"].lower())
    python_packages = []
    for dist in distributions:
        name = dist.metadata["Name"]
        python_packages.append({"name": name, "version": dist.version,
                                "license": dist.metadata.get("License-Expression") or dist.metadata.get("License"),
                                "license_classifiers": [item for item in dist.metadata.get_all("Classifier", [])
                                                        if item.startswith("License ::")]})
        for entry in dist.files or ():
            if ".dist-info" in entry.parts[0] and any(NOTICE.match(part) for part in entry.parts[1:]):
                with dist.locate_file(entry).open("rb") as stream:
                    copy_notice(root / "licenses" / "python" / name, "/".join(entry.parts[1:]), stream)

    root.mkdir(parents=True, exist_ok=True)
    inventory = {"image": args.image, "platform": "linux/amd64", "alpine": packages, "python": python_packages}
    inventory_path.write_text(json.dumps(inventory, indent=2) + "\n")
    branch = "v" + ".".join(Path("/etc/alpine-release").read_text().strip().split(".")[:2])
    origins = sorted({(item["origin"], item["version"], item["commit"]) for item in packages})
    errors = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        jobs = {executor.submit(download_apk_source, root, branch, name, version, commit): name
                for name, version, commit in origins}
        jobs.update({executor.submit(download_python_source, root, dist["name"], dist["version"]): dist["name"]
                     for dist in python_packages})
        for job in as_completed(jobs):
            name = jobs[job]
            try:
                directory = job.result()
                collect_notices(directory, root / "licenses" / directory.parent.name / directory.name)
            except Exception as error:
                if isinstance(error, subprocess.CalledProcessError):
                    error = f"{error}: {error.stderr.decode().strip()}"
                errors.append(f"{name}: {error}")
    report = root / "collection-errors.txt"
    if errors:
        report.write_text("\n".join(sorted(errors)) + "\n")
        raise SystemExit(f"{len(errors)} sources need attention. See {report}.")
    report.unlink(missing_ok=True)
    supplement_notices(root, origins)
    collect_rust_sources(root, origins)
    package_release(root, args.image)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Digest of the Linux image being inspected")
    parser.add_argument("--output", required=True, type=Path)
    run(parser.parse_args())
