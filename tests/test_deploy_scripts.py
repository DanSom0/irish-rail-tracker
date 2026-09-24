"""Exercise deployment scripts with local commands; never contact a server or AWS."""

import gzip
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    shutil.copy(ROOT / ".env.production.example", tmp_path / ".env")
    with (tmp_path / ".env").open("a") as env:
        env.write("IMAGE_TAG=latest\n")  # Older server configuration.
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    monkeypatch.setenv("CHECK_DIR", str(tmp_path))
    # Advance Bash's own deadline clock without waiting a minute in each test.
    (tmp_path / "bash_env").write_text("sleep() { SECONDS=$((SECONDS + $1)); }\n")
    monkeypatch.setenv("BASH_ENV", str(tmp_path / "bash_env"))
    return tmp_path


def command(deployment, name, body):
    path = deployment / "bin" / name
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
    path.chmod(0o755)


def run(deployment, name, *args):
    return subprocess.run(
        ["bash", str(deployment / "scripts" / name), *args],
        cwd="/", capture_output=True, text=True, timeout=10, check=False,
    )


@pytest.mark.parametrize("dump_status,upload_status", [(0, 0), (1, 0), (0, 1)])
def test_backup_uploads_only_complete_dumps(deployment, monkeypatch, dump_status, upload_status):
    monkeypatch.setenv("DUMP_STATUS", str(dump_status))
    monkeypatch.setenv("UPLOAD_STATUS", str(upload_status))
    command(deployment, "docker", '''
printf '%s\\n' "$*" >> "$CHECK_DIR/docker.log"
if [[ $1 == compose ]]; then
  printf '%s\\n' '-- test SQL dump'
  exit "$DUMP_STATUS"
fi
while [[ $1 != --volume ]]; do shift; done
cp "${2%%:*}/"*.sql.gz "$CHECK_DIR/upload.sql.gz"
exit "$UPLOAD_STATUS"
''')
    result = run(deployment, "backup.sh")
    assert (result.returncode == 0) == (dump_status == upload_status == 0)
    uploaded = deployment / "upload.sql.gz"
    assert uploaded.exists() == (dump_status == 0)
    if uploaded.exists():
        assert gzip.decompress(uploaded.read_bytes()) == b"-- test SQL dump\n"
        log = (deployment / "docker.log").read_text()
        assert "AWS_EC2_METADATA_V1_DISABLED=true" in log
        assert "s3://replace_with_backup_bucket_name/backups/" in log
    assert ("[backup] Uploaded" in result.stdout) == (result.returncode == 0)


@pytest.mark.parametrize("existing", [False, True])
def test_cron_installation_is_idempotent_and_preserves_other_jobs(deployment, existing):
    cron = deployment / "crontab"
    original = "15 1 * * * echo unrelated\n" if existing else ""
    if existing:
        cron.write_text(original)
    command(deployment, "crontab", '''
if [[ $1 == -l ]]; then
  if [[ ! -f "$CHECK_DIR/crontab" ]]; then
    echo 'no crontab for test-user' >&2
    exit 1
  fi
  cat "$CHECK_DIR/crontab"
else
  cp "$1" "$CHECK_DIR/crontab"
fi
''')
    for _ in range(2):
        result = run(deployment, "install-cron.sh")
        assert result.returncode == 0, result.stderr
    contents = cron.read_text()
    assert contents.startswith(original)
    assert contents.count("# irish-rail-tracker-backup") == 1
    assert "0 3 * * * /opt/irish-rail-tracker/scripts/backup.sh" in contents


def test_cron_read_errors_do_not_replace_existing_jobs(deployment):
    command(deployment, "crontab", '''
[[ $1 == -l ]] || touch "$CHECK_DIR/replaced"
echo 'permission denied' >&2
exit 1
''')
    assert run(deployment, "install-cron.sh").returncode != 0
    assert not (deployment / "replaced").exists()


@pytest.mark.parametrize("cached", [True, False])
def test_rollback_uses_the_requested_sha(deployment, cached):
    sha = "a" * 40
    command(deployment, "docker", f'''
printf '%s %s\\n' "$IMAGE_TAG" "$*" >> "$CHECK_DIR/docker.log"
if [[ $1 == image ]]; then exit {0 if cached else 1}; fi
''')
    command(deployment, "curl", "printf 200")
    result = run(deployment, "rollback.sh", sha)
    assert result.returncode == 0, result.stderr
    log = (deployment / "docker.log").read_text()
    assert all(line.startswith(sha) for line in log.splitlines())
    assert ("pull web worker" in log) == (not cached)
    assert "up -d" in log
    assert (deployment / ".env").read_text().count(f"IMAGE_TAG={sha}\n") == 1
    assert "IMAGE_TAG=latest" not in (deployment / ".env").read_text()


def test_rollback_rejects_invalid_tags_before_running_docker(deployment):
    command(deployment, "docker", 'touch "$CHECK_DIR/called"')
    for args in [(), ("latest",), ("a" * 40, "extra")]:
        assert run(deployment, "rollback.sh", *args).returncode != 0
    assert not (deployment / "called").exists()


def test_pin_image_tag_on_first_deploy(deployment):
    env = deployment / ".env"
    env.write_text(env.read_text().replace("IMAGE_TAG=latest\n", ""))
    sha = "b" * 40
    result = run(deployment, "pin-image-tag.sh", sha)
    assert result.returncode == 0, result.stderr
    assert f"IMAGE_TAG={sha}\n" in env.read_text()
    assert "POSTGRES_DB=replace_with_database_name" in env.read_text()
    assert env.stat().st_mode & 0o777 == 0o600


def test_healthcheck_retries_until_exactly_200(deployment):
    command(deployment, "curl", '''
count=$(cat "$CHECK_DIR/attempts" 2>/dev/null || echo 0)
echo $((count + 1)) > "$CHECK_DIR/attempts"
case $count in
  0) printf 301;;
  1) printf 204;;
  2) printf 503;;
  3) printf 000; exit 7;;
  *) printf 200;;
esac
''')
    assert run(deployment, "healthcheck.sh", "http://localhost/health").returncode == 0
    assert (deployment / "attempts").read_text().strip() == "5"


def test_healthcheck_and_rollback_fail_when_never_healthy(deployment):
    command(deployment, "curl", "printf 503")
    command(deployment, "docker", "exit 0")
    result = run(deployment, "rollback.sh", "a" * 40)
    assert result.returncode != 0
    assert "endpoint did not return HTTP 200" in result.stderr
    assert "IMAGE_TAG=" + "a" * 40 in (deployment / ".env").read_text()
