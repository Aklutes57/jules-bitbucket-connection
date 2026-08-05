#!/usr/bin/env python3
"""
jules-bitbucket-connection sync engine.

Bridges Bitbucket Cloud repositories to GitHub mirror repositories so that
Google Jules (which only connects to GitHub) can work on Bitbucket-hosted
code. Runs inside GitHub Actions (see .github/workflows/jules-sync.yml),
but can also be run locally.

Ownership model (per branch):
  - Branches on Bitbucket that do NOT look like Jules branches are owned by
    Bitbucket: force-synced Bitbucket -> GitHub mirror.
  - Branches that look like Jules branches (trailing 15+ digit session id,
    e.g. "fix-login-bug-1640409954249346636") or that exist only on the
    GitHub mirror are owned by GitHub: synced GitHub -> Bitbucket so they
    can be reviewed and merged in Bitbucket.
  - Deletions are never propagated unless prune_mirror_branches is enabled.
  - Tags sync one way, Bitbucket -> GitHub, and are never force-updated.

State files under state/ record which side each branch came from. The
workflow commits them after every run; they prevent a branch deleted on one
side from being resurrected by the other.

Environment variables:
  BITBUCKET_TOKEN     required. Atlassian API token (scoped) or a Bitbucket
                      workspace/project/repository access token.
  BITBUCKET_EMAIL     required when BITBUCKET_TOKEN is an Atlassian API
                      token: the Atlassian account email. Leave unset when
                      using a workspace/project/repository access token.
  MIRROR_GITHUB_PAT   required. GitHub PAT used to create/push the mirror
                      repos (see README for the exact permissions).
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
CONFIG_FILE = REPO_ROOT / "jules-sync.yml"

GITHUB_API = "https://api.github.com"
BITBUCKET_API = "https://api.bitbucket.org/2.0"
MIRROR_TOPIC = "jules-bitbucket-mirror"

DEFAULTS = {
    "mirror_owner": "",            # GitHub user/org for mirrors; "" = PAT owner
    "mirror_suffix": "-mirror",    # default mirror name is <bitbucket slug><suffix>
    "private_mirrors": True,
    "create_bitbucket_prs": True,
    "bitbucket_main_branch": "",   # "" = auto-detect from Bitbucket
    "prune_mirror_branches": False,
    "sync_tags": True,
    "jules_branch_regex": r"-\d{15,}$",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def api(url, headers, method="GET", body=None, allow=()):
    """Small urllib wrapper. Returns (status, parsed-json-or-None)."""
    data = json.dumps(body).encode() if body is not None else None
    req_headers = {"Accept": "application/json", **headers}
    if body is not None:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode()
            return resp.status, (json.loads(text) if text.strip() else None)
    except urllib.error.HTTPError as e:
        if e.code in allow:
            return e.code, None
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from None


class Bridge:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run

        bb_token = os.environ.get("BITBUCKET_TOKEN", "").strip()
        bb_email = os.environ.get("BITBUCKET_EMAIL", "").strip()
        gh_pat = os.environ.get("MIRROR_GITHUB_PAT", "").strip()
        if not bb_token:
            die("BITBUCKET_TOKEN is not set (add it as an Actions secret).")
        if not gh_pat:
            die("MIRROR_GITHUB_PAT is not set (add it as an Actions secret).")

        # REST auth.
        # Atlassian API tokens use Basic auth with the ACCOUNT EMAIL as the
        # username; workspace/project/repo access tokens use Bearer auth.
        if bb_email:
            self.bb_headers = {"Authorization": f"Basic {b64(f'{bb_email}:{bb_token}')}"}
            git_user = "x-bitbucket-api-token-auth"  # static username for API tokens
        else:
            self.bb_headers = {"Authorization": f"Bearer {bb_token}"}
            git_user = "x-token-auth"                # static username for access tokens
        self.gh_headers = {
            "Authorization": f"Bearer {gh_pat}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        # Git auth via URL-scoped extraheader config passed through the
        # environment, so tokens never appear in argv or in remote URLs
        # (git error messages print URLs).
        self.git_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.https://bitbucket.org/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {b64(f'{git_user}:{bb_token}')}",
            "GIT_CONFIG_KEY_1": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_1": f"Authorization: Basic {b64(f'x-access-token:{gh_pat}')}",
        }

        _, me = api(f"{GITHUB_API}/user", self.gh_headers)
        self.gh_login = me["login"]

    # -- git ----------------------------------------------------------------

    def git(self, workdir, *args, check=True):
        res = subprocess.run(
            ["git", "-C", str(workdir), *args],
            env=self.git_env, capture_output=True, text=True,
        )
        if check and res.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {res.stderr.strip()[:800]}")
        return res

    def local_refs(self, workdir, prefix):
        res = self.git(workdir, "for-each-ref", prefix,
                       "--format=%(refname) %(objectname)")
        refs = {}
        for line in res.stdout.splitlines():
            name, sha = line.rsplit(" ", 1)
            refs[name[len(prefix) + 1:]] = sha
        return refs

    def remote_tags(self, url):
        res = self.git(REPO_ROOT, "ls-remote", "--tags", url)
        tags = {}
        for line in res.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.endswith("^{}"):  # peeled annotated tag: record target
                tags[ref[len("refs/tags/"):-3]] = sha
            elif ref.startswith("refs/tags/"):
                tags.setdefault(ref[len("refs/tags/"):], sha)
        return tags

    # -- GitHub mirror management -------------------------------------------

    def ensure_mirror(self, owner, repo, pair, cfg, bb_full):
        """Create the GitHub mirror repo if needed. Returns True if created."""
        status, data = api(f"{GITHUB_API}/repos/{owner}/{repo}",
                           self.gh_headers, allow=(404,))
        if status == 404:
            if self.dry_run:
                log(f"  [dry-run] would create GitHub repo {owner}/{repo}")
                return True
            body = {
                "name": repo,
                "private": bool(cfg["private_mirrors"]),
                "description": f"Jules mirror of bitbucket.org/{bb_full} "
                               f"(managed by jules-bitbucket-connection)",
            }
            if owner.lower() == self.gh_login.lower():
                api(f"{GITHUB_API}/user/repos", self.gh_headers, "POST", body)
            else:
                api(f"{GITHUB_API}/orgs/{owner}/repos", self.gh_headers, "POST", body)
            api(f"{GITHUB_API}/repos/{owner}/{repo}/topics", self.gh_headers,
                "PUT", {"names": [MIRROR_TOPIC]})
            log(f"  + created GitHub mirror {owner}/{repo}")
            log(f"    NOTE: grant the Google Labs Jules GitHub App access to "
                f"{owner}/{repo} before starting Jules tasks on it.")
            return True

        # Repo exists: refuse to force-push over anything that isn't marked
        # as a bridge mirror, unless it's empty or explicitly allowed.
        topics = data.get("topics") or []
        if MIRROR_TOPIC in topics:
            return False
        _, branches = api(f"{GITHUB_API}/repos/{owner}/{repo}/branches?per_page=1",
                          self.gh_headers)
        if branches and not pair.get("allow_existing"):
            die(f"GitHub repo {owner}/{repo} already exists, is not empty, and is "
                f"not marked as a bridge mirror (topic '{MIRROR_TOPIC}'). Refusing "
                f"to sync over it. If you are certain, set 'allow_existing: true' "
                f"for this repo in jules-sync.yml.")
        if not self.dry_run:
            api(f"{GITHUB_API}/repos/{owner}/{repo}/topics", self.gh_headers,
                "PUT", {"names": sorted(set(topics) | {MIRROR_TOPIC})})
        return False

    def set_default_branch(self, owner, repo, branch):
        api(f"{GITHUB_API}/repos/{owner}/{repo}", self.gh_headers,
            "PATCH", {"default_branch": branch})

    # -- Bitbucket helpers ---------------------------------------------------

    def bb_main_branch(self, ws, slug, override):
        if override:
            return override
        _, data = api(f"{BITBUCKET_API}/repositories/{ws}/{slug}", self.bb_headers)
        main = (data.get("mainbranch") or {}).get("name")
        return main or "main"

    def ensure_bitbucket_pr(self, ws, slug, branch, dest, gh_owner, gh_repo):
        q = urllib.parse.quote(f'source.branch.name = "{branch}" AND state = "OPEN"')
        _, existing = api(
            f"{BITBUCKET_API}/repositories/{ws}/{slug}/pullrequests?q={q}",
            self.bb_headers)
        if existing.get("values"):
            return
        # Link the mirror-side PR (Jules usually opens one) if it exists.
        _, gh_prs = api(
            f"{GITHUB_API}/repos/{gh_owner}/{gh_repo}/pulls"
            f"?head={gh_owner}:{urllib.parse.quote(branch)}&state=all&per_page=1",
            self.gh_headers)
        mirror_link = (gh_prs[0]["html_url"] if gh_prs
                       else f"https://github.com/{gh_owner}/{gh_repo}/tree/{branch}")
        title = "Jules: " + (re.sub(r"-\d{15,}$", "", branch).replace("-", " ")
                             or branch)
        description = (
            f"Automated pull request for a branch created by Google Jules on the "
            f"GitHub mirror and synced here by jules-bitbucket-connection.\n\n"
            f"Mirror-side work: {mirror_link}\n\n"
            f"Tip: to ask Jules for changes, comment on the GitHub pull request "
            f"above (Jules responds to PR comments there). This Bitbucket PR is "
            f"updated automatically as Jules pushes. Merge it here when ready."
        )
        _, pr = api(
            f"{BITBUCKET_API}/repositories/{ws}/{slug}/pullrequests",
            self.bb_headers, "POST",
            {
                "title": title,
                "source": {"branch": {"name": branch}},
                "destination": {"branch": {"name": dest}},
                "description": description,
                "close_source_branch": True,
            })
        log(f"  + opened Bitbucket PR: {pr['links']['html']['href']}")


# ---------------------------------------------------------------------------
# Per-repo sync
# ---------------------------------------------------------------------------

def load_state(ws, slug):
    path = STATE_DIR / f"{ws}__{slug}.json"
    if path.exists():
        data = json.loads(path.read_text())
        return path, set(data.get("bitbucket_origin", [])), set(data.get("github_origin", []))
    return path, set(), set()


def save_state(path, bb_origin, gh_origin):
    STATE_DIR.mkdir(exist_ok=True)
    payload = json.dumps(
        {"bitbucket_origin": sorted(bb_origin), "github_origin": sorted(gh_origin)},
        indent=2) + "\n"
    if not path.exists() or path.read_text() != payload:
        path.write_text(payload)


def sync_pair(bridge: Bridge, pair: dict, cfg: dict) -> bool:
    bb_full = pair["bitbucket"]
    ws, _, slug = bb_full.partition("/")
    if not ws or not slug:
        die(f"Invalid 'bitbucket' value {bb_full!r}: expected 'workspace/repo-slug'.")

    owner = cfg["mirror_owner"] or bridge.gh_login
    repo = pair.get("github_repo") or f"{slug}{cfg['mirror_suffix']}"
    log(f"\n=== {bb_full}  <->  github.com/{owner}/{repo} ===")

    created = bridge.ensure_mirror(owner, repo, pair, cfg, bb_full)

    bb_url = f"https://bitbucket.org/{ws}/{slug}.git"
    gh_url = f"https://github.com/{owner}/{repo}.git"

    workdir = Path(tempfile.mkdtemp(prefix="jules-sync-"))
    try:
        bridge.git(workdir, "init", "--bare", "-q")

        bridge.git(workdir, "fetch", "--no-tags", bb_url,
                   "+refs/heads/*:refs/bb/*", "+refs/tags/*:refs/tags/*")
        if not created:
            # A just-created mirror is empty; there is nothing to fetch.
            bridge.git(workdir, "fetch", "--no-tags", gh_url,
                       "+refs/heads/*:refs/gh/*")

        bb_branches = bridge.local_refs(workdir, "refs/bb")
        gh_branches = bridge.local_refs(workdir, "refs/gh")

        state_path, bb_origin, gh_origin = load_state(ws, slug)

        jules_re = re.compile(cfg["jules_branch_regex"])
        push_gh, push_bb, new_jules = [], [], []

        for b, sha in sorted(bb_branches.items()):
            if jules_re.search(b):
                # Jules branches are owned by the GitHub side.
                if b in gh_branches:
                    if gh_branches[b] != sha:
                        push_bb.append(f"+refs/gh/{b}:refs/heads/{b}")
                        log(f"  GH->BB  {b} (Jules update)")
                else:
                    log(f"  ~ {b}: matches the Jules pattern but is missing from "
                        f"the mirror; leaving the Bitbucket copy untouched")
                gh_origin.add(b)
                bb_origin.discard(b)
            else:
                if gh_branches.get(b) != sha:
                    push_gh.append(f"+refs/bb/{b}:refs/heads/{b}")
                    log(f"  BB->GH  {b}")
                bb_origin.add(b)
                gh_origin.discard(b)

        for b, sha in sorted(gh_branches.items()):
            if b in bb_branches:
                continue
            if b in bb_origin:
                if cfg["prune_mirror_branches"]:
                    push_gh.append(f":refs/heads/{b}")
                    bb_origin.discard(b)
                    log(f"  GH del  {b} (deleted on Bitbucket)")
                else:
                    log(f"  ~ {b}: deleted on Bitbucket; kept on the mirror "
                        f"(prune_mirror_branches: false)")
            elif b in gh_origin:
                log(f"  ~ {b}: was synced to Bitbucket and deleted there; "
                    f"not resurrecting")
            else:
                push_bb.append(f"refs/gh/{b}:refs/heads/{b}")
                gh_origin.add(b)
                log(f"  GH->BB  {b} (new)")
                if jules_re.search(b):
                    new_jules.append(b)

        # Tags: Bitbucket -> GitHub, never forced.
        tag_specs = []
        if cfg["sync_tags"]:
            local_tags = bridge.local_refs(workdir, "refs/tags")
            if local_tags:
                gh_tags = {} if created else bridge.remote_tags(gh_url)
                tag_specs = [f"refs/tags/{t}:refs/tags/{t}"
                             for t in sorted(local_tags) if t not in gh_tags]
                for t in tag_specs:
                    log(f"  BB->GH  tag {t.split(':')[0][len('refs/tags/'):]}")

        if not (push_gh or push_bb or tag_specs):
            log("  in sync; nothing to do")

        if bridge.dry_run:
            log("  [dry-run] no pushes performed")
            return True

        if push_gh:
            bridge.git(workdir, "push", gh_url, *push_gh)
        if tag_specs:
            res = bridge.git(workdir, "push", gh_url, *tag_specs, check=False)
            if res.returncode != 0:
                log(f"  ! some tags were not synced (moved tags are never "
                    f"force-updated): {res.stderr.strip()[:300]}")
        if push_bb:
            bridge.git(workdir, "push", bb_url, *push_bb)

        main = bridge.bb_main_branch(ws, slug, cfg["bitbucket_main_branch"])
        if created and main in bb_branches:
            bridge.set_default_branch(owner, repo, main)

        if cfg["create_bitbucket_prs"]:
            for b in new_jules:
                try:
                    bridge.ensure_bitbucket_pr(ws, slug, b, main, owner, repo)
                except RuntimeError as e:
                    log(f"  ! could not open Bitbucket PR for {b}: {e}")

        # Drop state entries for branches that no longer exist on either side.
        live = set(bb_branches) | set(gh_branches)
        save_state(state_path, bb_origin & live, gh_origin & live)
        return True
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the sync plan without pushing or calling "
                             "any write APIs")
    args = parser.parse_args()

    if not CONFIG_FILE.exists():
        die(f"missing config file {CONFIG_FILE.name} in the repo root")
    config = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    cfg = {**DEFAULTS, **(config.get("defaults") or {})}
    repos = config.get("repos") or []
    if not repos:
        die("no repos configured: add entries under 'repos:' in jules-sync.yml")

    bridge = Bridge(dry_run=args.dry_run)

    failures = 0
    for pair in repos:
        if not isinstance(pair, dict) or "bitbucket" not in pair:
            die("each entry under 'repos:' needs a 'bitbucket: workspace/repo-slug' key")
        try:
            sync_pair(bridge, pair, {**cfg, **{k: v for k, v in pair.items()
                                               if k in DEFAULTS}})
        except RuntimeError as e:
            failures += 1
            log(f"  ! sync failed for {pair['bitbucket']}: {e}")

    if failures:
        die(f"{failures} repo(s) failed to sync")
    log("\nAll repos synced.")


if __name__ == "__main__":
    main()
