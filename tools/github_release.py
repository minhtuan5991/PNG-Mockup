"""Publish PNG Mockup only to its explicitly approved, dedicated GitHub repo.

Developer tool, never included in the installed app. Credentials come from
Git Credential Manager and stay in memory; they are not printed or saved.
Commands are explicit: status, create, release. No background publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_release import INSTALLER_NAME, MANIFEST_NAME, ROOT, inspect_installer, validate_repository

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
ASSET_HOSTS = {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class ReleaseError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_credential(account: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", account):
        raise ReleaseError("Invalid GitHub account name.")
    environment = dict(os.environ, GCM_INTERACTIVE="Never", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost=github.com\nusername={account}\n\n",
        capture_output=True, encoding="utf-8", env=environment, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise ReleaseError("No usable stored GitHub login. Sign in with Git Credential Manager first.")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    token = fields.get("password", "")
    if not token or any(c in token for c in "\r\n"):
        raise ReleaseError("Git Credential Manager did not return a usable credential.")
    return token


class GitHub:
    def __init__(self, account: str):
        self._token = load_credential(account)
        self._opener = build_opener(NoRedirect())
        identity = self.json("GET", "/user")
        if identity.get("login", "").casefold() != account.casefold():
            raise ReleaseError("Authenticated GitHub identity does not match the selected account.")
        self.account = identity["login"]

    def _request(self, method: str, url: str, *, data: bytes | None = None,
                 content_type: str = "application/json", accept: str = "application/vnd.github+json"):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc not in {"api.github.com", "uploads.github.com"} or parsed.username or parsed.password:
            raise ReleaseError("Refusing to send a credential to a non-GitHub API host.")
        request = Request(url, data=data, method=method, headers={
            "Accept": accept, "Content-Type": content_type,
            "Authorization": "Bearer " + self._token,
            "User-Agent": "PNG-Mockup-Release-Tool", "X-GitHub-Api-Version": "2026-03-10",
        })
        return self._opener.open(request, timeout=30)

    def json(self, method: str, path: str, body: dict | None = None,
             *, allow_missing: bool = False) -> dict | list | None:
        if not path.startswith("/") or path.startswith("//"):
            raise ReleaseError("API requests must use a relative GitHub API path.")
        try:
            with self._request(method, API + path, data=None if body is None else json.dumps(body).encode("utf-8")) as response:
                return json.load(response)
        except HTTPError as error:
            if allow_missing and error.code == 404:
                return None
            raise ReleaseError(f"GitHub API returned HTTP {error.code} for {method} {path.split('?')[0]}.") from None

    def upload(self, repository: str, release_id: int, path: Path) -> dict:
        url = f"{UPLOADS}/repos/{repository}/releases/{release_id}/assets?{urlencode({'name': path.name})}"
        try:
            with self._request("POST", url, data=path.read_bytes(), content_type="application/octet-stream") as response:
                return json.load(response)
        except HTTPError as error:
            raise ReleaseError(f"Uploading {path.name} returned HTTP {error.code}; the release remains a draft.") from None

    def verify_asset(self, repository: str, asset: dict, path: Path) -> None:
        content = path.read_bytes()
        expected_hash = hashlib.sha256(content).hexdigest()
        if asset.get("name") != path.name or asset.get("size") != len(content) or asset.get("state") != "uploaded":
            raise ReleaseError(f"Uploaded asset metadata does not match {path.name}.")
        expected_api = f"{API}/repos/{repository}/releases/assets/{int(asset['id'])}"
        try:
            response = self._request("GET", expected_api, accept="application/octet-stream")
        except HTTPError as error:
            if error.code not in {301, 302, 303, 307, 308}:
                raise ReleaseError(f"Verifying draft asset returned HTTP {error.code}.") from None
            location = error.headers.get("Location", "")
            parsed = urlsplit(location)
            if parsed.scheme != "https" or parsed.hostname not in ASSET_HOSTS or parsed.port not in {None, 443} or parsed.username or parsed.password:
                raise ReleaseError("GitHub asset redirected to an unexpected host.") from None
            # Redirect requests deliberately carry no Authorization header.
            response = self._opener.open(Request(location, headers={"User-Agent": "PNG-Mockup-Release-Tool"}), timeout=30)
        with response:
            digest = hashlib.sha256()
            received = 0
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > len(content):
                    raise ReleaseError(f"Remote asset is larger than expected: {path.name}")
                digest.update(chunk)
        if received != len(content) or digest.hexdigest() != expected_hash:
            raise ReleaseError(f"Remote asset hash mismatch: {path.name}")


def repo_summary(client: GitHub, repository: str) -> dict:
    result = client.json("GET", f"/repos/{repository}", allow_missing=True)
    if result is None:
        return {"repository": repository, "exists": False, "account": client.account}
    return {"repository": result["full_name"], "exists": True,
            "private": result["private"], "url": result["html_url"],
            "can_push": bool(result.get("permissions", {}).get("push")), "account": client.account}


def create_repo(client: GitHub, repository: str, visibility: str) -> dict:
    current = repo_summary(client, repository)
    private = visibility == "private"
    if current["exists"]:
        if current["private"] != private:
            raise ReleaseError("Existing repository visibility differs; it will not be changed automatically.")
        return current
    owner, name = repository.split("/")
    if owner.casefold() != client.account.casefold():
        raise ReleaseError("Creating organization repositories requires a separate explicit workflow.")
    client.json("POST", "/user/repos", {
        "name": name, "private": private,
        "description": "Independent Windows PNG/PSD mockup app with a private runtime and dedicated updates.",
        "auto_init": False, "has_issues": True, "has_wiki": False, "has_projects": False,
    })
    return repo_summary(client, repository)


def publish_release(client: GitHub, repository: str, prepared: Path, commit: str, publish: bool) -> dict:
    folder = prepared.resolve()
    if (ROOT / "build").resolve() not in folder.parents:
        raise ReleaseError("Only a prepared release inside this project's build/ may be uploaded.")
    if any(path.is_symlink() or path.is_junction() for path in (prepared, *prepared.parents)):
        raise ReleaseError("Prepared release paths cannot use reparse points.")
    if not re.fullmatch(r"[a-fA-F0-9]{40}", commit):
        raise ReleaseError("Select the exact pushed commit SHA before creating a release.")
    manifest = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
    verified = inspect_installer(folder / INSTALLER_NAME, repository)
    if manifest != verified:
        raise ReleaseError("Prepared update manifest does not match the verified installer/channel.")
    remote = repo_summary(client, repository)
    if not remote.get("exists") or not remote.get("can_push") or remote.get("private"):
        raise ReleaseError("Token-free installed-app updates require the approved public repository with push access.")
    client.json("GET", f"/repos/{repository}/commits/{commit}")
    tag = manifest["tag"]
    notes = (folder / "release-notes.md").read_text(encoding="utf-8")
    releases = client.json("GET", f"/repos/{repository}/releases?per_page=100")
    release = next((item for item in releases if item["tag_name"] == tag), None)
    if release is not None and not release.get("draft"):
        raise ReleaseError("This version is already published. Never replace published update assets; bump the version instead.")
    if release is None:
        release = client.json("POST", f"/repos/{repository}/releases", {
            "tag_name": tag, "target_commitish": commit, "name": "PNG Mockup " + manifest["version"],
            "body": notes, "draft": True, "prerelease": False,
        })
    if release.get("target_commitish") != commit or release.get("prerelease"):
        raise ReleaseError("Existing draft does not target the approved commit or is a prerelease.")
    release_id = int(release["id"])
    assets = client.json("GET", f"/repos/{repository}/releases/{release_id}/assets?per_page=100")
    for name in (INSTALLER_NAME, INSTALLER_NAME + ".sha256", MANIFEST_NAME):
        path = folder / name
        asset = next((item for item in assets if item["name"] == name), None)
        if asset is None:
            asset = client.upload(repository, release_id, path)
        client.verify_asset(repository, asset, path)
    if publish:
        release = client.json("PATCH", f"/repos/{repository}/releases/{release_id}", {"draft": False, "make_latest": "true"})
    return {"repository": repository, "version": manifest["version"], "url": release["html_url"],
            "draft": release["draft"], "assets_verified": True, "installer_sha256": manifest["installer"]["sha256"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--repository", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    create = commands.add_parser("create")
    create.add_argument("--visibility", required=True, choices=("public", "private"))
    release = commands.add_parser("release")
    release.add_argument("--prepared", type=Path, required=True)
    release.add_argument("--commit", required=True)
    release.add_argument("--publish", action="store_true", help="Explicitly publish after every asset has been verified.")
    args = parser.parse_args()
    repository = validate_repository(args.repository)
    client = GitHub(args.account)
    if args.command == "status":
        result = repo_summary(client, repository)
    elif args.command == "create":
        result = create_repo(client, repository, args.visibility)
    else:
        result = publish_release(client, repository, args.prepared, args.commit, args.publish)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None
