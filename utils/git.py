import os
import json
import subprocess
from typing import Optional


class Git:
    def __init__(self, cwd: str = "."):
        self.cwd = cwd

    def run(self, *args, capture_output=False):
        return subprocess.run(
            ["git", *args],
            cwd=self.cwd,
            check=True,
            capture_output=capture_output,
            text=True
        )

    def init(self):
        self.run("init")

    def clone(self, url: str, path: str):
        subprocess.run(
            ["git", "clone", url, path],
            check=True
        )

    def pull(self, branch: Optional[str] = None):
        if branch:
            self.run("pull", "origin", branch)
        else:
            self.run("pull")

    def push(self, branch: Optional[str] = None, set_upstream=False):
        if branch:
            if set_upstream:
                self.run("push", "-u", "origin", branch)
            else:
                self.run("push", "origin", branch)
        else:
            self.run("push")

    def fetch(self, branch: str):
        self.run("fetch", "origin", branch)

    def checkout(self, branch: str):
        self.run("checkout", branch)

    def add(self, path: str = "."):
        self.run("add", path)

    def commit(self, message: str):
        self.run("commit", "-m", message)

    def has_changes(self) -> bool:
        result = subprocess.run(
            ["git", "diff-index", "--quiet", "HEAD"],
            cwd=self.cwd
        )
        return result.returncode != 0

    def has_staged_changes(self) -> bool:
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=self.cwd
        )
        return result.returncode != 0

    def get_remote_url(self, remote: str = "origin") -> Optional[str]:
        result = subprocess.run(
            ["git", "remote", "get-url", remote],
            cwd=self.cwd,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            return None

        return result.stdout.strip()

    def add_remote(self, remote: str, url: str):
        self.run("remote", "add", remote, url)

    def set_remote_url(self, remote: str, url: str):
        self.run("remote", "set-url", remote, url)

    def dispatch(self, event_type: str, payload: dict):
        token = os.environ["UPDATE_TOKEN"]
        repository = os.environ["GITHUB_REPOSITORY"]

        data = {
            "event_type": event_type,
            "client_payload": payload
        }

        subprocess.run(
            [
                "curl",
                "-X", "POST",
                "-H", "Accept: application/vnd.github+json",
                "-H", f"Authorization: token {token}",
                "-H", "X-GitHub-Api-Version: 2022-11-28",
                f"https://api.github.com/repos/{repository}/dispatches",
                "-d", json.dumps(data)
            ],
            check=True
        )
