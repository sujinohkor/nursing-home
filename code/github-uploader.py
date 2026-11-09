#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub 폴더 URL + 로컬 폴더를 지정해 하위 파일 전체를 업로드합니다.

필요 패키지: pip install requests
Git 설치 불필요. 업로드 대상 GitHub 소유자는 sujinohkor로 제한합니다.

- GitHub URL에서 OWNER/REPO/BRANCH/TARGET_DIR 자동 추출
- 브랜치 이름에 / 가 있어도 GitHub API로 실제 브랜치 확인
- 로컬 폴더 자체가 아니라 폴더 안의 파일과 폴더 구조를 업로드
- 파일 형식 제한 없음. 비어 있는 폴더는 Git에 저장할 수 없음
- 동일한 파일은 건너뛰고, 달라진 파일은 덮어쓰기 전 확인
- 삭제는 하지 않으며, 중단 후 다시 실행하면 완료된 내용은 건너뜀
- 작은 파일은 GraphQL로 묶어서 커밋, 큰 파일은 REST Git Database API 사용
- Windows 클립보드의 PAT를 기본 사용하여 getpass 붙여넣기 오류 회피
- 토큰 출처를 명시하고 GitHub 인증을 업로드 전에 확인
"""

import argparse
import base64
import getpass
import hashlib
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import requests


ALLOWED_OWNER = "sujinohkor"
API_BASE = "https://api.github.com"
GRAPHQL_URL = f"{API_BASE}/graphql"
BATCH_SIZE = 150
MAX_GRAPHQL_BATCH_BYTES = 3 * 1024 * 1024  # 바이너리 기준 3 MiB (base64 약 4 MiB)
MAX_FILE_BYTES = 100 * 1024 * 1024  # GitHub 일반 Git 파일 상한
MAX_RETRIES = 5
REQUEST_DELAY = 0.5


@dataclass(frozen=True)
class Destination:
    owner: str
    repo: str
    branch: str
    target_dir: str  # 루트는 빈 문자열

    @property
    def full_name(self):
        return f"{self.owner}/{self.repo}"

    @property
    def browser_url(self):
        base = f"https://github.com/{self.full_name}/tree/{quote(self.branch, safe='/')}"
        if self.target_dir:
            base += "/" + "/".join(quote(p, safe="") for p in self.target_dir.split("/"))
        return base


@dataclass(frozen=True)
class LocalFile:
    source: Path
    remote_path: str
    size: int
    sha: str
    action: str  # add 또는 update


class GitHubError(RuntimeError):
    pass


def log(message, level="INFO"):
    print(f"[{level}] {message}", flush=True)


def get_clipboard_token():
    """클립보드를 직접 읽습니다. 사용자의 명시적 Enter 이후에만 호출합니다."""
    try:
        import tkinter as tk
    except ImportError as exc:
        raise ValueError(
            "Python에 tkinter가 없습니다. Windows 표준 Python 설치를 확인하거나 "
            "--auth-source getpass를 사용하세요."
        ) from exc

    window = None
    try:
        window = tk.Tk()
        window.withdraw()
        window.update()
        try:
            return window.clipboard_get().strip()
        except tk.TclError as exc:
            raise ValueError(
                "클립보드에서 텍스트를 읽지 못했습니다. "
                "GitHub PAT를 복사한 다음 다시 실행하세요."
            ) from exc
    except tk.TclError as exc:
        raise ValueError(
            "Windows 클립보드를 열 수 없습니다. "
            "일반 데스크톱에서 실행하거나 --auth-source getpass를 사용하세요."
        ) from exc
    finally:
        if window is not None:
            window.destroy()


def acquire_token(auth_source):
    """인증 방식을 명시합니다. 환경변수는 사용자가 요청한 경우에만 읽습니다."""
    if auth_source == "clipboard":
        print("\nGitHub의 실제 PAT(github_pat_...)를 클립보드에 복사하세요.")
        print("토큰 이름이 아닌 실제 토큰 문자열이어야 합니다.")
        input("PAT를 복사했다면 Enter를 누르세요: ")
        token = get_clipboard_token()
        log("인증 방식: Windows 클립보드 (GITHUB_TOKEN 환경변수는 사용하지 않음)")
    elif auth_source == "getpass":
        token = getpass.getpass("GitHub Fine-grained PAT (입력 숨김): ")
        log("인증 방식: 터미널 직접 입력")
    elif auth_source == "env":
        token = os.environ.get("GITHUB_TOKEN", "")
        log("인증 방식: GITHUB_TOKEN 환경변수")
    else:
        raise ValueError(f"알 수 없는 인증 방식: {auth_source}")

    token = token.strip()
    if not token:
        raise ValueError(
            "PAT 문자열이 비어 있습니다. 실제 토큰을 다시 복사하세요. "
            "GitHub 토큰 이름은 사용할 수 없습니다."
        )
    if any(ch.isspace() for ch in token):
        raise ValueError("PAT에 공백 또는 줄바꿈이 포함되어 있습니다. 토큰 문자열만 복사하세요.")
    if not token.startswith(("github_pat_", "ghp_")):
        raise ValueError(
            "클립보드/입력값이 GitHub PAT 형식이 아닙니다. "
            "github_pat_로 시작하는 실제 토큰 문자열을 확인하세요."
        )
    return token


def explain_http_error(response):
    try:
        data = response.json()
        msg = data.get("message", "")
        details = data.get("errors")
        if details:
            msg += f" | {str(details)[:350]}"
    except ValueError:
        msg = response.text[:350]
    detail = f"GitHub HTTP {response.status_code}: {msg.strip()}"
    if response.status_code == 403 and "resource not accessible by personal access token" in msg.lower():
        detail += " | PAT의 Repository permissions > Contents: Read and write 권한을 확인하세요."
    return detail


class GitHubClient:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sujinohkor-folder-uploader",
        })

    def request(self, method, endpoint, *, body=None, params=None, timeout=180):
        url = endpoint if endpoint.startswith("https://") else API_BASE + endpoint
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.request(
                    method, url, json=body, params=params, timeout=timeout
                )
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise GitHubError(f"네트워크 요청 실패: {exc}") from exc
                wait = min(30, 2 ** attempt)
                log(f"네트워크 오류. {wait}초 후 재시도 ({attempt}/{MAX_RETRIES})", "WARN")
                time.sleep(wait)
                continue

            retryable = response.status_code in (429, 500, 502, 503, 504)
            if response.status_code == 403 and (
                response.headers.get("X-RateLimit-Remaining") == "0"
                or "secondary rate limit" in response.text.lower()
            ):
                retryable = True

            if retryable and attempt < MAX_RETRIES:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait = min(60, max(1, int(retry_after)))
                except ValueError:
                    wait = min(30, 2 ** attempt)
                log(f"{explain_http_error(response)}. {wait}초 후 재시도", "WARN")
                time.sleep(wait)
                continue

            if not response.ok:
                raise GitHubError(explain_http_error(response))
            try:
                return response.json()
            except ValueError as exc:
                raise GitHubError("GitHub 응답이 유효한 JSON이 아닙니다.") from exc

        raise GitHubError("GitHub API 요청 실패")

    def graphql(self, query, variables):
        result = self.request("POST", GRAPHQL_URL, body={"query": query, "variables": variables})
        if result.get("errors"):
            messages = "; ".join(e.get("message", str(e)) for e in result["errors"])
            if "resource not accessible by personal access token" in messages.lower():
                messages += " | PAT의 Repository permissions > Contents: Read and write 권한을 확인하세요."
            raise GitHubError(f"GraphQL 오류: {messages}")
        return result.get("data") or {}

    def verify_authentication(self):
        """파일 업로드 전에 REST /user로 토큰이 유효한지 확인합니다."""
        try:
            data = self.request("GET", "/user", timeout=20)
        except GitHubError as exc:
            if "GitHub HTTP 401:" in str(exc):
                raise GitHubError(
                    "PAT 인증 실패(HTTP 401). 클립보드에 현재 유효한 실제 PAT가 "
                    "복사되어 있는지 확인하세요. 이 프로그램은 기본적으로 "
                    "GITHUB_TOKEN 환경변수를 사용하지 않습니다."
                ) from exc
            raise
        login = data.get("login")
        if not login:
            raise GitHubError("GitHub 인증 응답에 사용자 이름이 없습니다.")
        if login.lower() != ALLOWED_OWNER.lower():
            raise GitHubError(
                f"PAT의 GitHub 계정({login})이 허용된 OWNER({ALLOWED_OWNER})와 다릅니다."
            )
        return login

    def repo_metadata(self, owner, repo):
        return self.request("GET", f"/repos/{quote(owner)}/{quote(repo)}")

    def branch_state(self, owner, repo, branch):
        query = """
        query($owner: String!, $repo: String!, $ref: String!) {
          repository(owner: $owner, name: $repo) {
            nameWithOwner
            ref(qualifiedName: $ref) {
              target {
                oid
                ... on Commit { tree { oid } }
              }
            }
          }
        }
        """
        data = self.graphql(query, {
            "owner": owner, "repo": repo, "ref": f"refs/heads/{branch}"
        })
        repository = data.get("repository")
        if repository is None:
            raise GitHubError(f"저장소를 찾지 못했습니다: {owner}/{repo}")
        ref = repository.get("ref")
        if ref is None:
            return None
        target = ref.get("target") or {}
        tree = target.get("tree") or {}
        if not tree.get("oid"):
            raise GitHubError(f"브랜치의 커밋 트리를 읽지 못했습니다: {branch}")
        return {"head_oid": target["oid"], "tree_oid": tree["oid"]}

    def tree(self, owner, repo, sha, recursive=False):
        endpoint = f"/repos/{quote(owner)}/{quote(repo)}/git/trees/{quote(sha)}"
        return self.request("GET", endpoint, params={"recursive": "1"} if recursive else None)


COMMIT_MUTATION = """
mutation(
  $repositoryNameWithOwner: String!,
  $branchName: String!,
  $expectedHeadOid: GitObjectID!,
  $message: String!,
  $additions: [FileAddition!]
) {
  createCommitOnBranch(input: {
    branch: {
      repositoryNameWithOwner: $repositoryNameWithOwner,
      branchName: $branchName
    },
    expectedHeadOid: $expectedHeadOid,
    message: { headline: $message },
    fileChanges: { additions: $additions }
  }) {
    commit { oid url tree { oid } }
  }
}
"""


def parse_github_url(url):
    """문법만 확인. branch/target 분리는 실제 저장소 API에서 확인합니다."""
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() not in ("github.com", "www.github.com"):
        raise ValueError("https://github.com/OWNER/REPO/tree/BRANCH/PATH 형식의 URL이 필요합니다.")

    parts = [unquote(x) for x in parsed.path.strip("/").split("/")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("GitHub URL에서 OWNER/REPO를 읽지 못했습니다.")
    if any(p in (".", "..") or "\\" in p or "\x00" in p or "/" in p for p in parts):
        raise ValueError("경로에 유효하지 않은 구성요소가 있습니다.")

    owner, repo = parts[:2]
    if owner.lower() != ALLOWED_OWNER.lower():
        raise ValueError(f"이 프로그램은 OWNER={ALLOWED_OWNER} 저장소만 허용합니다.")
    if repo.endswith(".git"):
        repo = repo[:-4]

    if len(parts) == 2:
        return owner, repo, []
    if len(parts) < 4 or parts[2] != "tree":
        raise ValueError("폴더 URL은 /tree/브랜치/경로 형식이어야 합니다. 파일(blob) URL은 지원하지 않습니다.")
    return owner, repo, parts[3:]


def resolve_destination(client, url):
    owner, repo, tail = parse_github_url(url)
    metadata = client.repo_metadata(owner, repo)
    if not tail:
        branch = metadata.get("default_branch")
        if not branch:
            raise GitHubError("기본 브랜치가 없습니다. 커밋이 생성된 저장소가 필요합니다.")
        state = client.branch_state(owner, repo, branch)
        if not state:
            raise GitHubError(f"기본 브랜치를 찾지 못했습니다: {branch}")
        return Destination(owner, repo, branch, ""), state

    # 폴더 이름과 슬래시가 포함된 브랜치명을 구분하기 위해 가장 긴 후보부터 조회합니다.
    for n in range(len(tail), 0, -1):
        branch = "/".join(tail[:n])
        state = client.branch_state(owner, repo, branch)
        if state:
            target_dir = "/".join(tail[n:])
            return Destination(owner, repo, branch, target_dir), state
    raise GitHubError("URL에서 실제 존재하는 브랜치를 찾지 못했습니다.")


def remote_entries(client, dest, root_tree_oid):
    """대상 폴더의 파일만 조회. 재귀 응답이 잘리면 비재귀 탐색으로 복구합니다."""
    current_sha = root_tree_oid
    if dest.target_dir:
        for segment in dest.target_dir.split("/"):
            entries = client.tree(dest.owner, dest.repo, current_sha).get("tree", [])
            found = next((e for e in entries if e.get("path") == segment), None)
            if not found:
                return {}
            if found.get("type") != "tree":
                raise GitHubError(f"대상 경로가 폴더가 아닙니다: {dest.target_dir}")
            current_sha = found["sha"]

    tree = client.tree(dest.owner, dest.repo, current_sha, recursive=True)
    if not tree.get("truncated"):
        return {
            e["path"]: e
            for e in tree.get("tree", [])
            if e.get("type") != "tree"
        }

    log("원격 폴더 목록이 커서 폴더별로 나누어 조회합니다.", "WARN")
    found = {}
    stack = [("", current_sha)]
    while stack:
        prefix, sha = stack.pop()
        current = client.tree(dest.owner, dest.repo, sha)
        if current.get("truncated"):
            raise GitHubError("GitHub 비재귀 트리 응답까지 잘렸습니다. 안전하게 중단합니다.")
        for item in current.get("tree", []):
            name = f"{prefix}{item['path']}"
            if item.get("type") == "tree":
                stack.append((name + "/", item["sha"]))
            else:
                found[name] = item
    return found


def git_blob_hash(path):
    size = path.stat().st_size
    digest = hashlib.sha1()
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def scan_local_files(source):
    root = Path(os.path.expandvars(os.path.expanduser(source.strip().strip('"').strip("'")))).resolve()
    if not root.is_dir():
        raise ValueError(f"로컬 폴더가 없거나 폴더가 아닙니다: {root}")

    files = []
    empty_dir_count = 0
    skipped_links = 0
    skipped_git_dirs = 0

    def onerror(exc):
        raise OSError(f"폴더를 읽지 못했습니다: {exc}") from exc

    for current, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
        location = Path(current)
        kept_dirs = []
        for name in dirs:
            entry = location / name
            if entry.is_symlink():
                skipped_links += 1
            elif name.lower() == ".git":
                skipped_git_dirs += 1
            else:
                kept_dirs.append(name)
        dirs[:] = kept_dirs

        if not dirs and not names:
            empty_dir_count += 1
        for name in names:
            path = location / name
            if path.is_symlink():
                skipped_links += 1
                continue
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(part in (".", "..", ".git") for part in rel.split("/")):
                raise ValueError(f"Git에 저장할 수 없는 파일 경로: {rel}")
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError(f"100 MiB를 초과하는 파일이 있습니다: {rel} ({size:,} bytes)")
            files.append((path, rel, size))

    files.sort(key=lambda row: row[1].casefold())
    return root, files, empty_dir_count, skipped_links, skipped_git_dirs


def plan_changes(local_rows, dest, existing):
    # existing: target_dir 기준 상대 경로 -> Git tree item
    changes = []
    unchanged = 0
    additions = 0
    updates = 0
    keys = set(existing)
    # 기존 파일명/폴더명 충돌을 빠르게 찾기 위한 상위 디렉터리 인덱스.
    # 수만 개 파일이 있어도 파일마다 원격 전체 목록을 다시 검색하지 않습니다.
    remote_directories = set()
    for remote_path in keys:
        parts = remote_path.split("/")
        for i in range(1, len(parts)):
            remote_directories.add("/".join(parts[:i]))
    for path, rel, size in local_rows:
        if rel in existing:
            remote = existing[rel]
            if remote.get("type") != "blob" or remote.get("mode") == "120000":
                raise ValueError(f"원격 파일 타입이 충돌합니다: {rel}")
        # 파일과 디렉터리 이름 충돌은 덮어쓰기 전에 차단합니다.
        prefix = ""
        for part in rel.split("/")[:-1]:
            prefix = f"{prefix}/{part}" if prefix else part
            if prefix in keys:
                raise ValueError(f"원격에 파일이 있어서 하위 폴더를 만들 수 없습니다: {prefix}")
        if rel in remote_directories:
            raise ValueError(f"원격 폴더와 로컬 파일이 충돌합니다: {rel}")

        sha = git_blob_hash(path)
        if rel in existing and existing[rel]["sha"] == sha:
            unchanged += 1
            continue
        action = "update" if rel in existing else "add"
        additions += action == "add"
        updates += action == "update"
        remote_path = f"{dest.target_dir}/{rel}" if dest.target_dir else rel
        changes.append(LocalFile(path, remote_path, size, sha, action))
    return changes, additions, updates, unchanged


def make_batches(changes):
    """작은 파일은 묶고, 큰 파일은 REST로 별도 처리할 배치로 나눕니다."""
    batches = []
    batch = []
    total_bytes = 0
    for file in changes:
        if file.size > MAX_GRAPHQL_BATCH_BYTES:
            if batch:
                batches.append(("graphql", batch))
                batch = []
                total_bytes = 0
            batches.append(("rest", [file]))
            continue
        if batch and (len(batch) >= BATCH_SIZE or total_bytes + file.size > MAX_GRAPHQL_BATCH_BYTES):
            batches.append(("graphql", batch))
            batch = []
            total_bytes = 0
        batch.append(file)
        total_bytes += file.size
    if batch:
        batches.append(("graphql", batch))
    return batches


def read_checked(file):
    data = file.source.read_bytes()
    check = hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()
    if check != file.sha:
        raise ValueError(f"업로드 직전에 파일 내용이 변경되었습니다: {file.source}")
    return base64.b64encode(data).decode("ascii")


def upload_graphql_batch(client, dest, state, files, batch_number, batch_total):
    additions = [{"path": file.remote_path, "contents": read_checked(file)} for file in files]
    data = client.graphql(COMMIT_MUTATION, {
        "repositoryNameWithOwner": dest.full_name,
        "branchName": dest.branch,
        "expectedHeadOid": state["head_oid"],
        "message": f"Upload files ({batch_number}/{batch_total}) to {dest.target_dir or '/'}",
        "additions": additions,
    })
    commit = (data.get("createCommitOnBranch") or {}).get("commit") or {}
    if not commit.get("oid") or not (commit.get("tree") or {}).get("oid"):
        raise GitHubError("커밋 응답에서 SHA를 읽지 못했습니다.")
    return {"head_oid": commit["oid"], "tree_oid": commit["tree"]["oid"]}, commit.get("url")


def upload_rest_large_file(client, dest, state, file):
    """큰 파일은 Blob -> Tree -> Commit -> Ref 절차로 별도 커밋합니다."""
    content = read_checked(file)
    prefix = f"/repos/{quote(dest.owner)}/{quote(dest.repo)}"
    blob = client.request("POST", f"{prefix}/git/blobs", body={
        "content": content, "encoding": "base64"
    }, timeout=300)
    if blob.get("sha") != file.sha:
        raise GitHubError(f"업로드한 Blob의 해시가 일치하지 않습니다: {file.remote_path}")

    tree = client.request("POST", f"{prefix}/git/trees", body={
        "base_tree": state["tree_oid"],
        "tree": [{
            "path": file.remote_path, "mode": "100644", "type": "blob", "sha": blob["sha"]
        }],
    })
    new_tree_sha = tree["sha"]
    commit = client.request("POST", f"{prefix}/git/commits", body={
        "message": f"Upload {file.remote_path}",
        "tree": new_tree_sha,
        "parents": [state["head_oid"]],
    })
    new_head_sha = commit["sha"]
    escaped_branch = quote(dest.branch, safe="/")
    client.request("PATCH", f"{prefix}/git/refs/heads/{escaped_branch}", body={
        "sha": new_head_sha, "force": False
    })
    return {"head_oid": new_head_sha, "tree_oid": new_tree_sha}, commit.get("html_url")


def batch_was_committed(client, dest, files):
    """타임아웃 직후 서버에서는 커밋이 성공했을 수 있으므로 안전하게 조회."""
    current = client.branch_state(dest.owner, dest.repo, dest.branch)
    if current is None:
        raise GitHubError("업로드 중 브랜치를 찾지 못했습니다.")
    remote = remote_entries(client, dest, current["tree_oid"])
    for file in files:
        rel = file.remote_path[len(dest.target_dir) + 1:] if dest.target_dir else file.remote_path
        if (remote.get(rel) or {}).get("sha") != file.sha:
            return None
    return current


def main(argv=None):
    parser = argparse.ArgumentParser(description="GitHub URL과 로컬 폴더를 지정하는 전체 파일 업로더")
    parser.add_argument("--url", help="GitHub의 저장소 또는 폴더 URL")
    parser.add_argument("--source", help="업로드할 로컬 폴더")
    parser.add_argument(
        "--auth-source", choices=("clipboard", "getpass", "env"),
        default="clipboard",
        help="PAT 읽기 방식 (기본: clipboard, 대안: getpass 또는 env)",
    )
    args = parser.parse_args(argv)

    print("=" * 68)
    print("GitHub Folder Uploader | 모든 파일 형식 및 하위 폴더 지원")
    print("=" * 68)
    url = (args.url or input("GitHub 저장소/폴더 URL: ")).strip()
    folder = args.source or input("로컬 폴더 경로: ").strip()
    if not url or not folder:
        raise ValueError("GitHub URL과 로컬 폴더 경로를 모두 입력해야 합니다.")

    # 토큰 입력 전에 OWNER 제한과 로컬 경로를 우선 검사합니다.
    parse_github_url(url)
    root, local_rows, empty_dirs, skipped_links, skipped_git_dirs = scan_local_files(folder)
    if not local_rows:
        log("업로드할 파일이 없습니다. Git은 빈 폴더만 별도로 저장할 수 없습니다.", "WARN")
        return
    if skipped_links:
        log(f"심볼릭 링크 {skipped_links}개는 안전을 위해 제외했습니다.", "WARN")
    if skipped_git_dirs:
        log(f".git 폴더 {skipped_git_dirs}개는 제외했습니다.", "WARN")
    if empty_dirs:
        log(f"빈 폴더 {empty_dirs}개는 Git 특성상 업로드되지 않습니다.", "WARN")

    token = acquire_token(args.auth_source)
    client = GitHubClient(token)
    del token  # 이후 토큰은 세션 헤더 내부에서만 사용
    try:
        log("GitHub PAT 인증을 먼저 확인합니다...")
        login = client.verify_authentication()
        log(f"GitHub 인증 성공: {login}", "OK")
        log("GitHub URL에서 브랜치와 대상 경로를 확인합니다...")
        dest, state = resolve_destination(client, url)
        remote = remote_entries(client, dest, state["tree_oid"])
        changes, additions, updates, unchanged = plan_changes(local_rows, dest, remote)
        batches = make_batches(changes)

        print("\n" + "-" * 68)
        print(f"OWNER       : {dest.owner}")
        print(f"REPO        : {dest.repo}")
        print(f"BRANCH      : {dest.branch}")
        print(f"TARGET_DIR  : {dest.target_dir or '(저장소 루트)'}")
        print(f"LOCAL       : {root}")
        print(f"대상 URL    : {dest.browser_url}")
        print("-" * 68)
        print(f"로컬 파일   : {len(local_rows):,}개")
        print(f"새 파일     : {additions:,}개")
        print(f"변경 파일   : {updates:,}개 (원격 파일 내용 덮어쓰기)")
        print(f"동일 파일   : {unchanged:,}개 (건너뜀)")
        print(f"예상 커밋   : {len(batches):,}개")
        print("원격 파일 삭제: 없음")
        print("-" * 68)

        if not changes:
            log("업로드할 새 파일이나 변경 파일이 없습니다.", "OK")
            return

        if input("위 위치에 업로드하시겠습니까? YES 입력: ").strip() != "YES":
            log("사용자가 업로드를 취소했습니다.")
            return

        start_time = time.monotonic()
        processed = 0
        total_bytes = 0
        for idx, (method, batch) in enumerate(batches, 1):
            current_bytes = sum(f.size for f in batch)
            log(f"커밋 {idx}/{len(batches)} | {len(batch)}개 파일 | {current_bytes / 1024**2:.2f} MiB | {method}")
            try:
                if method == "graphql":
                    state, commit_url = upload_graphql_batch(client, dest, state, batch, idx, len(batches))
                else:
                    state, commit_url = upload_rest_large_file(client, dest, state, batch[0])
            except (GitHubError, requests.RequestException) as exc:
                log("실패/타임아웃 이후 원격 커밋 여부를 재확인합니다...", "WARN")
                try:
                    recovered = batch_was_committed(client, dest, batch)
                except Exception as verify_exc:
                    raise GitHubError(
                        f"업로드 상태 확인도 실패했습니다: {verify_exc}. 다시 실행하면 완료 파일을 건너뜁니다."
                    ) from exc
                if not recovered:
                    raise GitHubError(f"커밋을 완료하지 못했습니다: {exc}. 다시 실행하면 완료 파일을 건너뜁니다.") from exc
                state, commit_url = recovered, "(원격 파일 확인으로 성공 판정)"
                log("서버에 파일이 존재하는 것을 확인했습니다. 다음 커밋을 진행합니다.", "WARN")

            processed += len(batch)
            total_bytes += current_bytes
            percent = processed / len(changes) * 100
            log(f"완료 {processed:,}/{len(changes):,} ({percent:.1f}%) | {commit_url or state['head_oid']}", "OK")
            if idx < len(batches):
                time.sleep(REQUEST_DELAY)

        elapsed = time.monotonic() - start_time
        print("\n" + "=" * 68)
        log(f"업로드 완료 | {processed:,}개 | {total_bytes / 1024**2:.2f} MiB | {elapsed:.1f}초", "OK")
        print(dest.browser_url)
        print("=" * 68)
    finally:
        client.session.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[WARN] 중단되었습니다. 다시 실행하면 동일한 파일은 건너뜁니다.")
        sys.exit(130)
    except (GitHubError, ValueError, OSError, EOFError) as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
