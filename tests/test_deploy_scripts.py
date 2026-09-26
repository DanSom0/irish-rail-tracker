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
    path = deployment / name if "/" in name else deployment / "scripts" / name
    return subprocess.run(
        ["bash", str(path), *args],
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
    assert "0 3 * * * /opt/irish-rail-tracker/scripts/nightly-backup.sh #" in contents


def test_cron_read_errors_do_not_replace_existing_jobs(deployment):
    command(deployment, "crontab", '''
[[ $1 == -l ]] || touch "$CHECK_DIR/replaced"
echo 'permission denied' >&2
exit 1
''')
    assert run(deployment, "install-cron.sh").returncode != 0
    assert not (deployment / "replaced").exists()


OLD, NEW = "a" * 40, "b" * 40
# A harmless Compose change between two releases, as a new environment variable would be.
OLD_COMPOSE = (ROOT / "docker-compose.prod.yml").read_text()
NEW_COMPOSE = OLD_COMPOSE.replace(
    "      IRISH_RAIL_API_URL:\n", "      IRISH_RAIL_API_URL:\n      RELEASE_CHECK: new\n")
assert NEW_COMPOSE != OLD_COMPOSE


def add_release(deployment, sha, compose):
    bundle = deployment / "releases" / sha
    shutil.copytree(ROOT / "scripts", bundle / "scripts")
    (bundle / "docker-compose.prod.yml").write_text(compose)


def activate(deployment, sha):
    """Lay out a server as a successful deploy of sha leaves it."""
    (deployment / "current").unlink(missing_ok=True)
    (deployment / "current").symlink_to(f"releases/{sha}")
    for name in ["docker-compose.prod.yml", "scripts"]:
        path = deployment / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
        path.symlink_to(f"current/{name}")
    env = deployment / ".env"
    lines = [line for line in env.read_text().splitlines() if not line.startswith("IMAGE_TAG=")]
    env.write_text("\n".join([*lines, f"IMAGE_TAG={sha}", ""]))


@pytest.fixture
def server(deployment, monkeypatch):
    """Two releases whose Compose files differ; the new one is current."""
    add_release(deployment, OLD, OLD_COMPOSE)
    add_release(deployment, NEW, NEW_COMPOSE)
    activate(deployment, NEW)
    monkeypatch.setenv("HEALTHY", f"{OLD} {NEW}")
    monkeypatch.setenv("CACHED", "1")
    # Docker records the Compose file and image tag it last started; curl reports HTTP 200
    # only while a release listed in HEALTHY is running.
    command(deployment, "docker", '''
printf '%s %s\\n' "${IMAGE_TAG:-}" "$*" >> "$CHECK_DIR/docker.log"
if [[ $1 == image ]]; then exit $((CACHED == 1 ? 0 : 1)); fi
if [[ " $* " == *" up -d "* ]]; then
  while [[ $1 != -f ]]; do shift; done
  cp "$2" "$CHECK_DIR/running.yml"
  printf '%s' "$IMAGE_TAG" > "$CHECK_DIR/running.tag"
fi
''')
    command(deployment, "curl", '''
if [[ " $HEALTHY " == *" $(cat "$CHECK_DIR/running.tag") "* ]]; then printf 200; else printf 503; fi
''')
    return deployment


def running(server):
    return (server / "running.tag").read_text(), (server / "running.yml").read_text()


def assert_current(server, sha, compose):
    assert os.readlink(server / "current") == f"releases/{sha}"
    assert (server / "docker-compose.prod.yml").read_text() == compose
    assert (server / "scripts" / "rollback.sh").exists()
    env = (server / ".env").read_text()
    assert env.count("IMAGE_TAG=") == env.count(f"IMAGE_TAG={sha}\n") == 1


@pytest.mark.parametrize("cached", [True, False])
def test_rollback_restores_the_older_compose_file_with_its_image(server, monkeypatch, cached):
    monkeypatch.setenv("CACHED", "1" if cached else "0")
    result = run(server, "rollback.sh", OLD)  # Through the scripts link, as an operator would.
    assert result.returncode == 0, result.stderr
    assert running(server) == (OLD, OLD_COMPOSE)
    assert_current(server, OLD, OLD_COMPOSE)
    log = (server / "docker.log").read_text()
    assert all(line.startswith(OLD) for line in log.splitlines())
    assert (f"-f releases/{OLD}/docker-compose.prod.yml pull web worker" in log) == (not cached)
    assert "up -d --remove-orphans" in log
    assert (server / "releases" / NEW).is_dir()  # Kept to roll forward again.


def test_failed_health_check_leaves_the_previous_release_active(server, monkeypatch):
    activate(server, OLD)
    monkeypatch.setenv("HEALTHY", OLD)
    result = run(server, f"releases/{NEW}/scripts/activate-release.sh", "--pull", NEW)
    assert result.returncode != 0
    assert "endpoint did not return HTTP 200" in result.stderr
    assert f"{OLD} is running and still current" in result.stderr
    log = (server / "docker.log").read_text()
    assert f"{NEW} compose --env-file .env -f releases/{NEW}/docker-compose.prod.yml pull\n" in log
    assert running(server) == (OLD, OLD_COMPOSE)
    assert_current(server, OLD, OLD_COMPOSE)


def test_rollback_without_a_bundle_changes_nothing(server):
    result = run(server, "rollback.sh", "c" * 40)
    assert result.returncode != 0
    assert "No release bundle" in result.stderr
    assert not (server / "docker.log").exists()
    assert_current(server, NEW, NEW_COMPOSE)


@pytest.mark.parametrize("healthy", [True, False])
def test_first_bundled_deploy_keeps_the_running_files_as_a_release(server, monkeypatch, healthy):
    for name in ["current", "docker-compose.prod.yml", "scripts"]:
        (server / name).unlink()
    shutil.rmtree(server / "releases" / OLD)
    # Before release bundles, deploy copied the running release's files to the top level.
    (server / "docker-compose.prod.yml").write_text(OLD_COMPOSE)
    shutil.copytree(ROOT / "scripts", server / "scripts")
    env = server / ".env"
    env.write_text(env.read_text().replace(f"IMAGE_TAG={NEW}\n", f"IMAGE_TAG={OLD}\n"))
    if not healthy:
        monkeypatch.setenv("HEALTHY", OLD)
    result = run(server, f"releases/{NEW}/scripts/activate-release.sh", "--pull", NEW)
    assert (result.returncode == 0) == healthy, result.stderr
    assert (server / "releases" / OLD / "docker-compose.prod.yml").read_text() == OLD_COMPOSE
    if healthy:
        assert running(server) == (NEW, NEW_COMPOSE)
        assert_current(server, NEW, NEW_COMPOSE)
        assert (server / "scripts").is_symlink()
    else:
        assert running(server) == (OLD, OLD_COMPOSE)
        assert os.readlink(server / "current") == f"releases/{OLD}"
        assert (server / "docker-compose.prod.yml").read_text() == OLD_COMPOSE
        assert f"IMAGE_TAG={OLD}\n" in env.read_text()


def test_nightly_backup_from_a_release_uses_the_shared_directory(server):
    (server / "releases" / NEW / "scripts" / "backup.sh").write_text(
        'source "$(dirname "${BASH_SOURCE[0]}")/app-dir.sh"; pwd -P\n')
    assert run(server, "nightly-backup.sh").returncode == 0  # The path cron runs.
    assert (server / "backup.log").read_text().strip() == str(server.resolve())
    assert not (server / "releases" / NEW / "backup.log").exists()


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


def test_healthcheck_fails_when_never_healthy(deployment):
    command(deployment, "curl", "printf 503")
    result = run(deployment, "healthcheck.sh", "http://localhost/health")
    assert result.returncode != 0
    assert "endpoint did not return HTTP 200" in result.stderr


@pytest.mark.parametrize("size,rotated", [(1024 * 1024, False), (1024 * 1024 + 1, True)])
def test_nightly_backup_rotates_log_above_one_mebibyte(deployment, size, rotated):
    (deployment / "scripts" / "backup.sh").write_text('echo "[backup] ran"; echo oops >&2\n')
    log = deployment / "backup.log"
    log.write_text("x" * (size - 1) + "\n")
    (deployment / "backup.log.1").write_text("oldest\n")
    result = run(deployment, "nightly-backup.sh")
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""  # Cron output goes to the log.
    assert log.read_text().endswith("[backup] ran\noops\n")
    assert (log.stat().st_size < 100) == rotated
    assert ((deployment / "backup.log.1").stat().st_size == size) == rotated


def test_nightly_backup_starts_a_log_and_keeps_backup_status(deployment):
    (deployment / "scripts" / "backup.sh").write_text('echo "[backup] Failed"; exit 3\n')
    assert run(deployment, "nightly-backup.sh").returncode == 3
    assert (deployment / "backup.log").read_text() == "[backup] Failed\n"


RELEASES = [  # Newest first, as the retention script sorts them.
    ("2026-09-26 03:00:00 +0000 UTC", "d" * 40),
    ("2026-09-25 03:00:00 +0000 UTC", "latest"),
    ("2026-09-25 03:00:00 +0000 UTC", "c" * 40),
    ("2026-09-24 03:00:00 +0000 UTC", "b" * 40),
    ("2026-09-23 03:00:00 +0000 UTC", "a" * 40),
    ("2026-09-22 03:00:00 +0000 UTC", "<none>"),
]


def image_commands(deployment, failing=""):
    listing = "\n".join(f"{created}\t{tag}" for created, tag in reversed(RELEASES))
    command(deployment, "docker", f'''
if [[ $1 == image && $2 == ls ]]; then printf '%b\\n' '{listing}'; exit; fi
printf '%s\\n' "$*" >> "$CHECK_DIR/docker.log"
[[ $3 != *:{failing or "never"} ]]
''')


def removed(deployment):
    log = deployment / "docker.log"
    lines = log.read_text().splitlines() if log.exists() else []
    assert all(line.startswith("image rm ghcr.io/dansom0/irish-rail-tracker:") for line in lines)
    return {line.rsplit(":", 1)[1] for line in lines}


@pytest.mark.parametrize("current,kept", [
    ("d" * 40, {"c" * 40, "b" * 40}),  # After a deploy.
    ("a" * 40, {"d" * 40, "c" * 40}),  # After rolling back to an older release.
])
def test_prune_images_keeps_current_and_two_previous_releases(deployment, current, kept):
    releases = {tag for _, tag in RELEASES if len(tag) == 40}
    for sha in releases | {"e" * 40}:  # The last bundle's image is no longer on the server.
        add_release(deployment, sha, OLD_COMPOSE)
    (deployment / "releases" / ".incoming-unrelated").mkdir()
    activate(deployment, current)
    image_commands(deployment)
    result = run(deployment, "prune-images.sh")
    assert result.returncode == 0, result.stderr
    assert removed(deployment) == (releases - kept - {current}) | {"latest"}
    assert "Kept " + current + " and 2 previous release(s)" in result.stdout
    # Bundles are kept for exactly the releases whose images are kept.
    bundles = {path.name for path in (deployment / "releases").iterdir()}
    assert bundles == kept | {current, ".incoming-unrelated"}


def test_prune_images_keeps_all_bundles_if_images_cannot_be_listed(deployment):
    for sha in ["a" * 40, "b" * 40, "c" * 40, "d" * 40]:
        add_release(deployment, sha, OLD_COMPOSE)
    activate(deployment, "d" * 40)
    command(deployment, "docker", "exit 1")
    result = run(deployment, "prune-images.sh")
    assert result.returncode != 0
    assert "nothing removed" in result.stderr
    assert len(list((deployment / "releases").iterdir())) == 4


def test_prune_images_refuses_without_a_pinned_sha(deployment):
    image_commands(deployment)  # The fixture's .env ends with IMAGE_TAG=latest.
    result = run(deployment, "prune-images.sh")
    assert result.returncode != 0
    assert removed(deployment) == set()


def test_prune_images_continues_after_a_failed_removal(deployment):
    env = deployment / ".env"
    env.write_text(env.read_text() + f"IMAGE_TAG={'d' * 40}\n")
    image_commands(deployment, failing="latest")
    result = run(deployment, "prune-images.sh")
    assert result.returncode == 0  # Retention never fails a running deploy.
    assert removed(deployment) == {"latest", "a" * 40}
    assert "could not be removed" in result.stderr
