#!/usr/bin/env python3

import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path

USERNAME = os.getenv("GITHUB_USERNAME")
TOKEN = os.getenv("MONITOR_GITHUB_TOKEN")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
ALERT_EMAIL = os.getenv("ALERT_EMAIL")
FROM_EMAIL = os.getenv("FROM_EMAIL") or "GitHub Repo Pulse <onboarding@resend.dev>"
SEND_QUIET_DIGEST = os.getenv("SEND_QUIET_DIGEST", "true").lower() in {"1", "true", "yes", "on"}
DIGEST_TITLE = os.getenv("DIGEST_TITLE", "GitHub Repo Pulse")
STATE_PATH = Path("data/state.json")
API_VERSION = "2026-03-10"


def require_config():
    missing = []
    for name, value in {
        "GITHUB_USERNAME": USERNAME,
        "MONITOR_GITHUB_TOKEN": TOKEN,
        "RESEND_API_KEY": RESEND_API_KEY,
        "ALERT_EMAIL": ALERT_EMAIL,
    }.items():
        if not value:
            missing.append(name)

    if missing:
        raise SystemExit(
            "Missing required configuration: " + ", ".join(missing) +
            ". See README.md for setup instructions."
        )


def request_json(url, *, headers=None, method="GET", payload=None):
    base_headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-repo-pulse",
    }

    if "api.github.com" in url:
        base_headers["Authorization"] = f"Bearer {TOKEN}"
        base_headers["X-GitHub-Api-Version"] = API_VERSION

    if headers:
        base_headers.update(headers)

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        base_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, headers=base_headers, method=method, data=data)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {url} failed: HTTP {exc.code}: {body}"
        ) from exc


def paginated(url, headers=None):
    results = []
    page = 1
    joiner = "&" if "?" in url else "?"

    while True:
        batch = request_json(
            f"{url}{joiner}per_page=100&page={page}",
            headers=headers,
        )
        if not isinstance(batch, list):
            raise RuntimeError(f"Expected list from {url}, got {type(batch).__name__}")
        results.extend(batch)
        if len(batch) < 100:
            return results
        page += 1


def load_state():
    if not STATE_PATH.exists():
        return None
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not state or not state.get("captured_at"):
            return None
        return state
    except json.JSONDecodeError:
        return None


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def collect():
    followers = paginated(f"https://api.github.com/users/{USERNAME}/followers")
    follower_logins = sorted(item["login"] for item in followers)

    repos = paginated(
        f"https://api.github.com/users/{USERNAME}/repos?type=owner&sort=updated&direction=desc"
    )

    repo_state = {}
    warnings = []

    for repo in repos:
        name = repo["name"]
        full_name = repo["full_name"]
        stars = []
        forks = []
        subscribers = []

        try:
            stars_raw = paginated(
                f"https://api.github.com/repos/{full_name}/stargazers",
                headers={"Accept": "application/vnd.github.star+json"},
            )
            for item in stars_raw:
                user = item.get("user") or item
                if user and user.get("login"):
                    stars.append(user["login"])
        except Exception as exc:
            warnings.append(f"{full_name}: could not list stargazer identities ({exc})")

        try:
            forks_raw = paginated(
                f"https://api.github.com/repos/{full_name}/forks?sort=newest"
            )
            forks = sorted(
                item["owner"]["login"]
                for item in forks_raw
                if item.get("owner") and item["owner"].get("login")
            )
        except Exception as exc:
            warnings.append(f"{full_name}: could not list fork identities ({exc})")

        try:
            watchers_raw = paginated(
                f"https://api.github.com/repos/{full_name}/subscribers"
            )
            subscribers = sorted(
                item["login"] for item in watchers_raw if item.get("login")
            )
        except Exception as exc:
            warnings.append(f"{full_name}: could not list watcher identities ({exc})")

        repo_state[name] = {
            "url": repo["html_url"],
            "star_count": repo.get("stargazers_count", 0),
            "fork_count": repo.get("forks_count", 0),
            "watcher_count": repo.get("subscribers_count", 0),
            "stargazers": sorted(stars),
            "fork_owners": forks,
            "watchers": subscribers,
        }

    return {
        "username": USERNAME,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "followers": follower_logins,
        "repos": repo_state,
        "warnings": warnings,
    }


def added(old, new):
    return sorted(set(new or []) - set(old or []))


def build_changes(old, new):
    if not old:
        return []

    changes = []

    for login in added(old.get("followers"), new.get("followers")):
        changes.append({
            "kind": "follower",
            "person": login,
            "label": f"@{login}",
            "url": f"https://github.com/{login}",
        })

    old_repos = old.get("repos", {})

    for repo_name, current in new.get("repos", {}).items():
        prior = old_repos.get(repo_name, {})
        repo_url = current["url"]

        for login in added(prior.get("stargazers"), current.get("stargazers")):
            changes.append({
                "kind": "star",
                "repo": repo_name,
                "person": login,
                "label": f"@{login} starred {repo_name}",
                "url": f"https://github.com/{login}",
                "repo_url": repo_url,
            })

        for login in added(prior.get("fork_owners"), current.get("fork_owners")):
            changes.append({
                "kind": "fork",
                "repo": repo_name,
                "person": login,
                "label": f"@{login} forked {repo_name}",
                "url": f"https://github.com/{login}",
                "repo_url": repo_url,
            })

        for login in added(prior.get("watchers"), current.get("watchers")):
            changes.append({
                "kind": "watch",
                "repo": repo_name,
                "person": login,
                "label": f"@{login} is watching {repo_name}",
                "url": f"https://github.com/{login}",
                "repo_url": repo_url,
            })

        for field, kind, noun in [
            ("star_count", "star", "star"),
            ("fork_count", "fork", "fork"),
            ("watcher_count", "watch", "watcher"),
        ]:
            before = int(prior.get(field, 0) or 0)
            after = int(current.get(field, 0) or 0)
            if after <= before:
                continue

            known = sum(
                1
                for change in changes
                if change["kind"] == kind and change.get("repo_url") == repo_url
            )
            missing = max(0, (after - before) - known)
            if missing:
                changes.append({
                    "kind": kind,
                    "repo": repo_name,
                    "person": None,
                    "count": missing,
                    "label": f"{repo_name}: +{missing} new {noun}{'' if missing == 1 else 's'}",
                    "url": repo_url,
                    "repo_url": repo_url,
                })

    return changes


def group_by_repo(changes, kind):
    grouped = defaultdict(list)
    for change in changes:
        if change["kind"] == kind:
            grouped[change.get("repo", "Unknown repository")].append(change)
    return grouped


def repo_section(title, icon, changes, kind):
    grouped = group_by_repo(changes, kind)
    if not grouped:
        return f"<h3>{icon} {title}</h3><p style='color:#666'>No changes</p>"

    html = f"<h3>{icon} {title}</h3>"
    for repo, items in sorted(grouped.items()):
        repo_url = items[0].get("repo_url") or items[0].get("url")
        total = 0
        people = []
        for item in items:
            if item.get("person"):
                total += 1
                people.append(item["person"])
            else:
                total += int(item.get("count", 1))

        html += (
            "<div style='margin:0 0 16px;padding:12px 14px;background:#f6f8fa;border-radius:8px'>"
            f"<strong><a href='{escape(repo_url)}'>{escape(repo)}</a></strong> "
            f"<strong>+{total}</strong>"
        )

        if people:
            links = [
                f"<a href='https://github.com/{escape(person)}'>@{escape(person)}</a>"
                for person in people
            ]
            html += f"<div style='margin-top:6px'>{', '.join(links)}</div>"
        html += "</div>"

    return html


def send_email(changes, state):
    followers = [c for c in changes if c["kind"] == "follower"]

    if followers:
        follower_html = "<h3>👤 New followers</h3>"
        for follower in followers:
            login = follower["person"]
            follower_html += (
                "<div style='margin:0 0 8px;padding:10px 14px;background:#f6f8fa;border-radius:8px'>"
                f"<a href='https://github.com/{escape(login)}'>@{escape(login)}</a>"
                "</div>"
            )
    else:
        follower_html = "<h3>👤 New followers</h3><p style='color:#666'>No changes</p>"

    star_html = repo_section("Stars", "⭐", changes, "star")
    fork_html = repo_section("Forks", "🍴", changes, "fork")
    watch_html = repo_section("Watchers", "👀", changes, "watch")

    total_stars = sum(repo["star_count"] for repo in state["repos"].values())
    total_forks = sum(repo["fork_count"] for repo in state["repos"].values())
    total_watchers = sum(repo["watcher_count"] for repo in state["repos"].values())
    total_followers = len(state["followers"])
    event_count = len(changes)

    if event_count:
        subject = f"{DIGEST_TITLE}: {event_count} new signal{'' if event_count == 1 else 's'}"
        intro = (
            f"You picked up <strong>{event_count}</strong> new GitHub engagement "
            f"signal{'' if event_count == 1 else 's'} since the previous check."
        )
    else:
        subject = f"{DIGEST_TITLE}: quiet day"
        intro = "No new followers, stars, forks, or watchers since the previous check."

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;max-width:680px;margin:auto;line-height:1.5;color:#24292f">
      <h2 style="margin-bottom:4px">{escape(DIGEST_TITLE)}</h2>
      <p style="color:#57606a;margin-top:0"><a href="https://github.com/{escape(USERNAME)}">@{escape(USERNAME)}</a></p>
      <p>{intro}</p>
      <hr style="border:0;border-top:1px solid #d0d7de;margin:24px 0">
      {follower_html}
      {star_html}
      {fork_html}
      {watch_html}
      <hr style="border:0;border-top:1px solid #d0d7de;margin:28px 0">
      <h3>Current totals</h3>
      <table style="border-collapse:collapse;width:100%">
        <tr><td style="padding:6px 0">Followers</td><td style="padding:6px 0;text-align:right;font-weight:bold">{total_followers}</td></tr>
        <tr><td style="padding:6px 0">Stars</td><td style="padding:6px 0;text-align:right;font-weight:bold">{total_stars}</td></tr>
        <tr><td style="padding:6px 0">Forks</td><td style="padding:6px 0;text-align:right;font-weight:bold">{total_forks}</td></tr>
        <tr><td style="padding:6px 0">Watchers</td><td style="padding:6px 0;text-align:right;font-weight:bold">{total_watchers}</td></tr>
      </table>
      <p style="font-size:12px;color:#8c959f;margin-top:28px">Sent automatically by GitHub Repo Pulse.</p>
    </div>
    """

    payload = {
        "from": FROM_EMAIL,
        "to": [ALERT_EMAIL],
        "subject": subject,
        "html": html,
    }

    request_json(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        method="POST",
        payload=payload,
    )


def main():
    require_config()
    old = load_state()
    current = collect()

    if old is None:
        save_state(current)
        print("Baseline created. No email sent on first run.")
        return

    changes = build_changes(old, current)
    save_state(current)

    if changes or SEND_QUIET_DIGEST:
        send_email(changes, current)
        if changes:
            print(f"Sent digest with {len(changes)} change(s).")
        else:
            print("Sent quiet-day digest.")
    else:
        print("No new engagement. Quiet-day digest disabled.")

    if current.get("warnings"):
        print("\nWarnings:", file=sys.stderr)
        for warning in current["warnings"]:
            print(" -", warning, file=sys.stderr)


if __name__ == "__main__":
    main()
