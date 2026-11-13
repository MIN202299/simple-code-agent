"""Tests for auto mode: which shell commands count as low-risk, and what happens to the rest."""

from __future__ import annotations

import asyncio

import pytest
from agents import set_tracing_disabled
from agents.testing import assistant_message, function_call

from nori import risk
from nori.permissions import Permissions
from nori.risk import assess_shell
from test_features import dialog, make_session, outputs, run_tui, shell, write

set_tracing_disabled(True)

LOW_RISK = [
    # looking at things
    "ls -la", "cat src/app.py", "head -n 20 README.md", "wc -l *.py", "pwd", "echo hello",
    "grep -rn TODO src", "rg 'def main' -g '*.py'", "find . -name '*.py' -type f", "tree -L 2",
    "diff a.txt b.txt", "sort names.txt | uniq -c | head", "cat package.json | jq .scripts",
    "sed -n '1,20p' file.txt", "du -sh .", "which python", "date", "ps aux",
    "cat missing.txt 2>/dev/null", "ls > /dev/null 2>&1", "cd src && ls", "test -f a.txt && echo yes",
    # git: reading, and local reversible steps
    "git status", "git status --short", "git log --oneline -n 5", "git diff HEAD~1", "git show HEAD",
    "git branch", "git branch -a", "git branch --show-current", "git remote -v", "git tag -l",
    "git stash list", "git config --get user.name", "git blame README.md", "git fetch",
    "git -C sub status", "git --no-pager log", "git add -A", "git add src/app.py",
    "git commit -m 'fix: typo'", "git stash",
    # tests, linters, builds
    "pytest", "pytest -q tests/test_app.py -k login", "python -m pytest -x", "python3 -m unittest",
    "ruff check .", "ruff format --check", "black --check src", "mypy src", "eslint src --ext .ts",
    "tsc --noEmit", "prettier --check .", "npm test", "npm run lint", "pnpm run build", "yarn test",
    "cargo test", "cargo check", "cargo clippy -- -D warnings", "go test ./...", "go vet ./...",
    "make test", "make lint", "npx eslint .", "uv run pytest -q", "uv run ruff check",
    "timeout 60 pytest", "time pytest", "python --version", "node --version", "pip list",
    "python -m pip list", "docker ps", "npm ls",
    # creating things inside the workspace
    "mkdir -p src/utils", "touch notes.md", "cp config.example.json config.json",
]

HIGH_OR_UNKNOWN = {
    # destructive
    "rm file.txt": risk.DESTRUCTIVE, "rm -rf build": risk.DESTRUCTIVE, "mv a b": risk.DESTRUCTIVE,
    "chmod +x run.sh": risk.DESTRUCTIVE, "kill 1234": risk.DESTRUCTIVE, "truncate -s 0 log": risk.DESTRUCTIVE,
    "find . -name '*.pyc' -delete": risk.DESTRUCTIVE, "find . -exec rm {} +": risk.DESTRUCTIVE,
    "sed -i 's/a/b/' file.txt": risk.DESTRUCTIVE, "sed -i.bak 's/a/b/' f": risk.DESTRUCTIVE,
    "sort -o out.txt in.txt": risk.DESTRUCTIVE,
    # network
    "curl https://example.com": risk.NETWORK, "wget http://x/y.sh": risk.NETWORK,
    "ssh host uptime": risk.NETWORK, "scp a host:b": risk.NETWORK, "gh pr create": risk.NETWORK,
    # privileges / arbitrary code
    "sudo ls": risk.PRIVILEGED, "bash script.sh": risk.PRIVILEGED, "sh -c 'ls'": risk.PRIVILEGED,
    "eval ls": risk.PRIVILEGED, "xargs rm": risk.PRIVILEGED, "env FOO=1 ls": risk.PRIVILEGED,
    # installs
    "npm install": risk.INSTALLS, "npm install left-pad": risk.INSTALLS, "pip install requests": risk.INSTALLS,
    "python -m pip install x": risk.INSTALLS, "brew install jq": risk.INSTALLS, "npm publish": risk.INSTALLS,
    "yarn add react": risk.INSTALLS, "apt-get install -y x": risk.INSTALLS,
    # git that rewrites, discards or publishes
    "git push": risk.GIT_HISTORY, "git push --force": risk.GIT_HISTORY, "git reset --hard": risk.GIT_HISTORY,
    "git clean -fd": risk.GIT_HISTORY, "git checkout -- .": risk.GIT_HISTORY, "git checkout main": risk.GIT_HISTORY,
    "git rebase main": risk.GIT_HISTORY, "git merge feature": risk.GIT_HISTORY, "git pull": risk.GIT_HISTORY,
    "git branch -D feature": risk.GIT_HISTORY, "git branch new-branch": risk.GIT_HISTORY,
    "git tag v1.0": risk.GIT_HISTORY, "git remote add origin x": risk.GIT_HISTORY,
    "git config user.name x": risk.GIT_HISTORY, "git commit --amend": risk.GIT_HISTORY,
    "git commit --no-verify -m x": risk.GIT_HISTORY, "git stash drop": risk.GIT_HISTORY,
    "git stash pop": risk.GIT_HISTORY, "git restore .": risk.GIT_HISTORY,
    # things that hide what runs
    "echo $(rm -rf .)": risk.HIDDEN, "echo `whoami`": risk.HIDDEN, "cat $HOME/.ssh/id_rsa": risk.HIDDEN,
    "ls $DIR": risk.HIDDEN, "FOO=bar ls": risk.HIDDEN, "ls &": risk.HIDDEN, "(ls)": risk.HIDDEN,
    "cat <<EOF\nx\nEOF": risk.HIDDEN, "ls\nrm -rf .": risk.HIDDEN, "diff <(ls a) <(ls b)": risk.HIDDEN,
    "echo 'unterminated": risk.HIDDEN, "ls &&": risk.HIDDEN,
    # writing through redirection
    "echo hi > out.txt": risk.REDIRECTS, "cat a >> b": risk.REDIRECTS, "ls 2> errors.log": risk.REDIRECTS,
    "sort < in.txt": risk.REDIRECTS,
    # leaving the workspace
    "cat /etc/passwd": risk.OUTSIDE, "ls ~": risk.OUTSIDE, "cat ~/.ssh/id_rsa": risk.OUTSIDE,
    "cat ../secret.txt": risk.OUTSIDE, "ls sub/../../other": risk.OUTSIDE, "cp a.txt /tmp/a.txt": risk.OUTSIDE,
    "grep -r key --include=*.env /home": risk.OUTSIDE, "cd / && ls": risk.OUTSIDE,
    "git -C /other status": risk.OUTSIDE, "mkdir /opt/x": risk.OUTSIDE, "touch ../x": risk.OUTSIDE,
    "find / -name id_rsa": risk.OUTSIDE,
    # simply not known to be safe
    "python script.py": risk.NOT_KNOWN, "python -c 'print(1)'": risk.NOT_KNOWN, "node build.js": risk.NOT_KNOWN,
    "./deploy.sh": risk.NOT_KNOWN, "npm run deploy": risk.NOT_KNOWN, "npm start": risk.NOT_KNOWN,
    "make install": risk.INSTALLS, "make": risk.NOT_KNOWN, "awk '{print $1}' f": risk.HIDDEN,
    "awk -f prog.awk f": risk.NOT_KNOWN, "docker run ubuntu": risk.NOT_KNOWN, "kubectl apply -f x.yaml": risk.NOT_KNOWN,
    "terraform apply": risk.NOT_KNOWN, "tee out.txt": risk.NOT_KNOWN, "npx create-react-app x": risk.NOT_KNOWN,
    "uv run python deploy.py": risk.NOT_KNOWN, "frobnicate --all": risk.NOT_KNOWN, "": risk.NOT_KNOWN,
    "cargo run": risk.NOT_KNOWN, "go run main.go": risk.NOT_KNOWN, "cargo publish": risk.INSTALLS,
}


@pytest.mark.parametrize("command", LOW_RISK)
def test_low_risk_commands(tmp_path, command):
    assert assess_shell(command, tmp_path).low, assess_shell(command, tmp_path)


@pytest.mark.parametrize("command", HIGH_OR_UNKNOWN)
def test_commands_that_still_ask(tmp_path, command):
    assessed = assess_shell(command, tmp_path)
    assert not assessed.low and assessed.reason == HIGH_OR_UNKNOWN[command], (command, assessed)


def test_one_risky_part_makes_the_whole_command_ask(tmp_path):
    for command in ("ls && rm -rf .", "git status; git push", "cat a | xargs rm", "pytest || curl x",
                    "ls | tee out.txt", "git status && ./deploy.sh", "cd src && rm x"):
        assert not assess_shell(command, tmp_path).low, command
    assert assess_shell("git status && git diff | head -50; pytest -q", tmp_path).low


def test_paths_inside_the_workspace_are_fine_even_when_absolute(tmp_path):
    (tmp_path / "src").mkdir()
    assert assess_shell(f"cat {tmp_path}/src/a.py", tmp_path).low
    assert assess_shell("cat src/../README.md", tmp_path).low  # `..` that stays inside
    assert not assess_shell(f"cat {tmp_path.parent}/other.txt", tmp_path).low


def test_auto_mode_decisions(tmp_path):
    permissions = Permissions(tmp_path, "auto")
    assert permissions.decide("write_file", {"path": "a.py"}) == "allow"
    assert permissions.decide("edit_file", {}) == "allow"
    assert permissions.decide("run_shell", {"command": "pytest -q"}) == "allow"
    assert permissions.decide("run_shell", {"command": "git push"}) == "ask"
    assert permissions.decide("run_shell", {"command": "pytest", "background": True}) == "ask"
    assert permissions.decide("web_fetch", {"url": "https://example.com"}) == "ask"
    assert permissions.decide("some_mcp_tool", {}) == "ask"
    assert permissions.auto_reason("run_shell", {"command": "git push"}) == risk.GIT_HISTORY
    assert "background" in permissions.auto_reason("run_shell", {"command": "x", "background": True})
    # rules still apply on top, and other modes give no auto reason
    permissions.add_rule("run_shell:git push")
    assert permissions.decide("run_shell", {"command": "git push origin main"}) == "allow"
    permissions.set_mode("default")
    assert permissions.decide("run_shell", {"command": "ls"}) == "ask"
    assert permissions.auto_reason("run_shell", {"command": "rm x"}) == ""


def test_auto_mode_runs_low_risk_work_and_asks_about_the_rest(tmp_path):
    session, _ = make_session(
        tmp_path,
        [
            [shell("git status", "c1")],
            [write("a.txt", "1")],
            [shell("cat a.txt && ls", "c3")],
            [shell("rm a.txt", "c4")],            # asks; the user says no
            [shell("curl https://x.example", "c5")],  # asks; the user says yes
            [assistant_message("done")],
        ],
        answers=["no", "yes"],
        mode="auto",
    )
    asyncio.run(session.run_turn("go"))

    assert [name for name, _rule in session.ui.asked] == ["run_shell", "run_shell"]
    results = outputs(session)
    assert "1a.txt" in results[2] and "rejected" in results[3]
    assert (session.context.workspace / "a.txt").exists()  # the rm was not run
    assert "Auto mode" in session.agent.model.calls[0].system_instructions


def test_bypass_mode_and_yes_flag_approve_everything(tmp_path):
    session, _ = make_session(
        tmp_path, [[write("a.txt", "1")], [shell("rm a.txt", "c2")], [assistant_message("done")]],
        mode="bypass",
    )  # fmt: skip
    asyncio.run(session.run_turn("go"))
    assert session.ui.asked == [] and not (session.context.workspace / "a.txt").exists()

    legacy, _ = make_session(tmp_path, [], auto_approve=True)  # what -y maps to
    assert legacy.mode == "bypass" and legacy.context.auto_approve


def test_tui_auto_mode_dialog_explains_why_it_asks(tmp_path):
    steps = [[shell("ls", "c1")], [shell("git push origin main", "c2")], [assistant_message("done")]]

    async def scenario(h):
        tui = h.tui
        h.send("\x1b[Z\x1b[Z")  # Shift+Tab twice: default -> accept-edits -> auto
        await h.until(lambda: tui.session.mode == "auto")
        assert "auto 模式" in "".join(text for _s, text in tui._footer_text())
        h.send("go\r")
        await h.until(lambda: tui._approval is not None)  # `ls` ran without a dialog
        text = dialog(tui)
        assert "git push origin main" in text
        assert "auto 模式询问的原因： 会改动 git 历史、工作区或远程仓库" in text
        h.send("\x1b")
        await h.idle()

    h = run_tui(tmp_path, steps, scenario)
    assert "● Bash(ls)" in h.text and h.tui.session.mode == "auto"
