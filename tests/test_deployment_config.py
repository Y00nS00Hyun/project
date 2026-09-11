"""Static checks on the deployment configuration.

These read the compose file, the Dockerfiles and the nginx config as data. They
do not start containers -- the point is that the *declared* configuration is
safe, so a mistake is caught before anything is deployed rather than after.

The security-relevant properties asserted here are the ones that are easy to
regress by accident: a debugging session that publishes 5432 to the host, a
convenience edit that drops `:ro` from the shared-folder mount, an API key that
ends up in a tracked file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "compose.yaml"
BACKEND_DOCKERFILE = ROOT / "Dockerfile.backend"
FRONTEND_DOCKERFILE = ROOT / "frontend" / "Dockerfile"
NGINX_CONF = ROOT / "frontend" / "nginx.conf"
ENV_EXAMPLE = ROOT / ".env.example"

#: Values good enough to render the compose file. Throwaway: these never leave
#: the test process and are not credentials for anything.
RENDER_ENV = {
    "APP_ENV": "production",
    "APP_HTTP_PORT": "8080",
    "POSTGRES_DB": "docsearch",
    "POSTGRES_USER": "docsearch",
    "POSTGRES_PASSWORD": "render-only-not-a-credential",
    "DATABASE_URL": "postgresql+psycopg://docsearch:render-only@postgres:5432/docsearch",
    "SHARED_FOLDER_HOST_PATH": "/tmp/render-only-shared",
    "SHARED_ROOT": "/data/shared",
    "EMBEDDING_CACHE_HOST_PATH": "/tmp/render-only-cache",
    "EMBEDDING_CACHE_DIR": "/opt/model-cache",
}


def instructions(path: Path) -> str:
    """A Dockerfile with its comment lines removed.

    The comments deliberately *name* the things that must not happen ("No
    --reload", "built without VITE_DEBUG_USER_ID"), so a plain substring search
    over the whole file would fail on the very documentation that explains the
    guarantee.
    """
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def rendered() -> dict:
    """The compose file as Docker actually resolves it.

    Rendered rather than parsed as YAML: the interesting properties live in the
    interpolated result, and asserting on the raw text would not prove what
    Docker will do with it.
    """
    if not _docker_compose_available():
        pytest.skip("docker compose is not available on this machine")
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, **RENDER_ENV},
    )
    if result.returncode != 0:
        pytest.fail(f"docker compose config failed:\n{result.stderr}")
    return json.loads(result.stdout)


class TestComposeRenders:
    def test_config_is_valid(self, rendered):
        assert set(rendered["services"]) == {"postgres", "migrate", "backend", "frontend"}

    def test_database_volume_is_named_so_down_does_not_destroy_it(self, rendered):
        assert "postgres_data" in rendered.get("volumes", {})
        mounts = rendered["services"]["postgres"]["volumes"]
        data = [m for m in mounts if m["target"] == "/var/lib/postgresql/data"]
        assert data and data[0]["type"] == "volume"


class TestDatabaseLocale:
    """pg_trgm's word characters come from the database ctype.

    Under the plain C locale, multibyte Korean is not alphanumeric, so
    show_trgm() returns an empty array and every lexical query matches nothing
    -- silently, with no error and no log line. This was a real defect in the
    first version of this compose file.
    """

    def test_locale_is_not_plain_c(self):
        text = COMPOSE.read_text(encoding="utf-8")
        assert "--locale=C.utf8" in text, "the cluster must be initialised with a UTF-8 ctype"
        assert "--locale=C " not in text and not text.count('--locale=C"'), (
            "plain C locale disables pg_trgm for Korean"
        )

    def test_encoding_is_utf8(self):
        assert "--encoding=UTF8" in COMPOSE.read_text(encoding="utf-8")


class TestPortExposure:
    def test_only_the_frontend_publishes_a_host_port(self, rendered):
        published = {
            name: svc.get("ports") or []
            for name, svc in rendered["services"].items()
        }
        assert published["postgres"] == [], "PostgreSQL must not be reachable from the host"
        assert published["backend"] == [], "FastAPI must only be reachable through nginx"
        assert published["migrate"] == []
        assert len(published["frontend"]) == 1

    def test_frontend_publishes_the_configured_port(self, rendered):
        port = rendered["services"]["frontend"]["ports"][0]
        assert str(port["published"]) == RENDER_ENV["APP_HTTP_PORT"]
        assert port["target"] == 8080


class TestSharedFolderMount:
    def test_shared_folder_is_read_only(self, rendered):
        """The system reads the shared folder. It must not be able to write it.

        Without read_only the container could modify, delete, rename or move an
        original document -- the one thing this system must never do to the
        source of truth.
        """
        mounts = rendered["services"]["backend"]["volumes"]
        shared = [m for m in mounts if m["target"] == RENDER_ENV["SHARED_ROOT"]]
        assert shared, "shared folder is not mounted into the backend"
        assert shared[0]["read_only"] is True

    def test_model_cache_is_read_only(self, rendered):
        mounts = rendered["services"]["backend"]["volumes"]
        cache = [m for m in mounts if m["target"] == RENDER_ENV["EMBEDDING_CACHE_DIR"]]
        assert cache and cache[0]["read_only"] is True

    def test_no_broad_default_host_path(self):
        """The host path is required, with no default.

        A default of /, $HOME or the repository itself would mean a missing
        setting silently indexes the wrong tree.
        """
        text = COMPOSE.read_text(encoding="utf-8")
        assert "${SHARED_FOLDER_HOST_PATH:?" in text, "must be required, not defaulted"
        assert "${SHARED_FOLDER_HOST_PATH:-" not in text
        assert "${EMBEDDING_CACHE_HOST_PATH:?" in text


class TestClaudeDisabledByDefault:
    def test_provider_is_empty_without_configuration(self, rendered):
        """An API key alone must not enable generation.

        LLM_PROVIDER selects the provider; anything but "anthropic" leaves
        UnconfiguredProvider in place and nothing leaves the network.
        """
        env = rendered["services"]["backend"]["environment"]
        assert env.get("LLM_PROVIDER") in ("", None)
        assert env.get("ANTHROPIC_API_KEY") in ("", None)

    def test_anthropic_variables_reach_the_backend_only(self, rendered):
        for name, svc in rendered["services"].items():
            if name == "backend":
                continue
            env = svc.get("environment") or {}
            leaked = [key for key in env if "ANTHROPIC" in key.upper()]
            assert not leaked, f"{name} must not receive {leaked}"

    def test_frontend_receives_no_vite_secret(self, rendered):
        env = rendered["services"]["frontend"].get("environment") or {}
        assert not [key for key in env if key.startswith("VITE_")]
        build = rendered["services"]["frontend"].get("build") or {}
        assert not (build.get("args") or {})


class TestProductionAuthentication:
    def test_default_app_env_is_production(self):
        """production makes require_user() fail closed until SSO exists."""
        text = COMPOSE.read_text(encoding="utf-8")
        assert "APP_ENV: ${APP_ENV:-production}" in text

    def test_debug_identity_is_not_configured_anywhere(self, rendered):
        blob = json.dumps(rendered)
        assert "X-Debug-User-Id" not in blob
        assert "VITE_DEBUG_USER_ID" not in blob

    def test_frontend_build_does_not_bake_in_an_identity(self):
        assert "VITE_DEBUG_USER_ID" not in instructions(FRONTEND_DOCKERFILE)


class TestNoSecretsInTrackedFiles:
    def test_no_api_key_literals(self):
        for path in (COMPOSE, BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE, ENV_EXAMPLE, NGINX_CONF):
            text = path.read_text(encoding="utf-8")
            assert "sk-ant-" not in text, f"{path.name} contains an API key literal"

    def test_env_example_supplies_no_password(self):
        """The example must not ship a working default password."""
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            if line.startswith("POSTGRES_PASSWORD="):
                assert line.strip() == "POSTGRES_PASSWORD=", "no default password"
            if line.startswith("ANTHROPIC_API_KEY="):
                assert line.strip() == "ANTHROPIC_API_KEY="

    def test_env_is_git_ignored_but_the_example_is_not(self):
        if shutil.which("git") is None:
            pytest.skip("git is not available")

        def ignored(relative: str) -> bool:
            return subprocess.run(
                ["git", "check-ignore", "-q", relative], cwd=ROOT
            ).returncode == 0

        assert ignored(".env")
        assert not ignored(".env.example")


class TestBackendImage:
    def test_runs_without_reload(self):
        text = instructions(BACKEND_DOCKERFILE)
        assert "--reload" not in text
        assert "api.app:app" in text

    def test_runs_as_a_non_root_user(self):
        assert "USER appuser" in BACKEND_DOCKERFILE.read_text(encoding="utf-8")

    def test_dependencies_come_from_pyproject_extras(self):
        """No second dependency list to keep in step with pyproject.toml."""
        assert "'.[api,db,llm,embedding]'" in BACKEND_DOCKERFILE.read_text(encoding="utf-8")

    def test_model_download_is_disabled_at_runtime(self):
        """No startup download: a missing cache must fail loudly."""
        text = BACKEND_DOCKERFILE.read_text(encoding="utf-8")
        assert "HF_HUB_OFFLINE=1" in text
        assert "TRANSFORMERS_OFFLINE=1" in text

    def test_pinned_base_image(self):
        text = instructions(BACKEND_DOCKERFILE)
        assert "FROM python:3.12-slim-bookworm" in text
        assert ":latest" not in text


class TestFrontendImage:
    def test_builds_static_files_and_serves_them_with_nginx(self):
        text = instructions(FRONTEND_DOCKERFILE)
        assert "npm ci" in text, "must install from the lockfile"
        assert "npm run build" in text
        assert "npm run dev" not in text, "no Vite dev server in production"
        assert ":latest" not in text

    def test_node_modules_do_not_reach_the_runtime_stage(self):
        text = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")
        assert "COPY --from=build --chown=nginx:nginx /app/dist" in text


class TestNginx:
    def test_spa_fallback_is_configured(self):
        """/search, /chat/<id> and /documents/<id> have no file behind them."""
        assert "try_files $uri $uri/ /index.html;" in NGINX_CONF.read_text(encoding="utf-8")

    def test_api_is_proxied_to_the_backend_service(self):
        text = NGINX_CONF.read_text(encoding="utf-8")
        assert "location /api/" in text
        assert "backend:8000" in text

    def test_upstream_is_resolved_per_request_not_once_at_startup(self):
        """A recreated backend gets a new IP; nginx must follow it.

        An `upstream` block caches the resolution for the life of the worker,
        so `docker compose up -d backend` would leave nginx returning 502 until
        it was itself restarted. Resolving through a variable defers the lookup
        to request time.
        """
        text = NGINX_CONF.read_text(encoding="utf-8")
        assert "resolver 127.0.0.11" in text, "Docker's embedded DNS must be configured"
        assert "proxy_pass $backend_upstream" in text
        assert "upstream backend {" not in text, "a static upstream block caches the IP"

    def test_forwarding_headers_are_set(self):
        text = NGINX_CONF.read_text(encoding="utf-8")
        for header in ("Host", "X-Real-IP", "X-Forwarded-For", "X-Forwarded-Proto", "X-Request-Id"):
            assert f"proxy_set_header {header}" in text


class TestDockerIgnore:
    def test_excludes_secrets_and_caches_but_keeps_the_example(self):
        text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for pattern in (".git", ".env", "frontend/node_modules/", "__pycache__/", "artifacts/"):
            assert pattern in text
        assert "!.env.example" in text


class TestLocalAuthDefaults:
    """The authentication switches must ship closed.

    An authentication path is something a deployment turns on deliberately.
    A default of "on" would mean every new environment gets a password login
    it never asked for, and a signup form open to whoever can reach it.
    """

    def test_compose_defaults_every_switch_to_off(self):
        compose = (ROOT / "compose.yaml").read_text()
        for name in ("LOCAL_AUTH_ENABLED", "SELF_SIGNUP_ENABLED", "AUTH_COOKIE_SECURE"):
            assert f"{name}: ${{{name}:-false}}" in compose, name

    def test_the_example_env_ships_them_off(self):
        lines = (ROOT / ".env.example").read_text().splitlines()
        settings = dict(
            line.split("=", 1) for line in lines if "=" in line and not line.startswith("#")
        )
        assert settings["LOCAL_AUTH_ENABLED"] == "false"
        assert settings["SELF_SIGNUP_ENABLED"] == "false"

    def test_the_department_setting_is_gone(self):
        """Local Auth no longer has any department input at all.

        Left behind, it would be a setting that looks like it does something
        and does not -- and the thing it used to do was place new accounts
        somewhere documents might be granted.
        """
        for name in ("compose.yaml", ".env.example"):
            assert "LOCAL_AUTH_DEFAULT_DEPARTMENT_ID" not in (ROOT / name).read_text()

    def test_no_password_or_secret_is_committed_in_the_example(self):
        text = (ROOT / ".env.example").read_text()
        for line in text.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if any(word in name for word in ("PASSWORD", "SECRET", "KEY", "TOKEN")):
                assert value.strip() == "", f"{name} must ship empty"


class TestIngestionTimer:
    """The systemd units that make ingestion automatic.

    Checked here because the failure modes are silent. A timer that overlaps
    itself, or that systemd disables after a few failures, keeps existing and
    stops working -- and nobody notices until a document is missing from
    search.
    """

    UNIT_DIR = ROOT / "deploy" / "systemd"

    def unit(self, name: str) -> str:
        return (self.UNIT_DIR / name).read_text()

    def test_the_schedule_cannot_overlap_itself(self):
        timer = self.unit("docsearch-ingest.timer")
        # OnUnitActiveSec counts from when a pass *started*, so a pass slower
        # than the interval would have its successor scheduled while it is
        # still running. OnUnitInactiveSec counts from when it finished.
        assert "OnUnitInactiveSec=" in timer
        assert "OnUnitActiveSec=" not in timer

    def test_the_default_interval_is_one_minute(self):
        assert "OnUnitInactiveSec=60s" in self.unit("docsearch-ingest.timer")

    def test_it_survives_a_reboot(self):
        timer = self.unit("docsearch-ingest.timer")
        assert "WantedBy=timers.target" in timer
        assert "install.sh" in (self.UNIT_DIR / "install.sh").name
        assert "enable --now docsearch-ingest.timer" in self.unit("install.sh")

    @staticmethod
    def sections(text: str) -> dict[str, list[str]]:
        """Split a unit file into its sections.

        Directives are section-scoped, and systemd ignores one written under
        the wrong heading -- logging "Unknown key name" and carrying on without
        it. A test that only greps for the line passes while the setting does
        nothing, which is how StartLimitIntervalSec sat in [Service] and was
        silently dropped by systemd 249.
        """
        found: dict[str, list[str]] = {}
        current = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                current = stripped[1:-1]
                found.setdefault(current, [])
            elif stripped and not stripped.startswith("#"):
                found.setdefault(current, []).append(stripped)
        return found

    def test_repeated_failures_cannot_disable_the_unit(self):
        # systemd's default rate limiter refuses to start a unit that failed
        # several times in quick succession -- exactly when the next attempt
        # matters most.
        sections = self.sections(self.unit("docsearch-ingest.service"))
        assert "StartLimitIntervalSec=0" in sections["Unit"]
        assert not any(d.startswith("StartLimitIntervalSec") for d in sections["Service"])

    def test_every_directive_sits_in_a_section_systemd_reads_it_from(self):
        """Guards the whole file against the same mistake.

        Not exhaustive about systemd's grammar -- it checks the directives this
        deployment actually relies on, each against the section systemd looks
        for it in.
        """
        service = self.sections(self.unit("docsearch-ingest.service"))
        timer = self.sections(self.unit("docsearch-ingest.timer"))

        for directive in ("Type=", "User=", "ExecStart=", "TimeoutStartSec=",
                          "StandardOutput=", "SyslogIdentifier="):
            assert any(d.startswith(directive) for d in service["Service"]), directive
        for directive in ("After=", "Wants=", "StartLimitIntervalSec="):
            assert any(d.startswith(directive) for d in service["Unit"]), directive

        for directive in ("Unit=", "OnBootSec=", "OnUnitInactiveSec=", "Persistent="):
            assert any(d.startswith(directive) for d in timer["Timer"]), directive
        assert "WantedBy=timers.target" in timer["Install"]

    def test_a_wedged_pass_cannot_hold_the_lock_forever(self):
        assert "TimeoutStartSec=" in self.unit("docsearch-ingest.service")

    def test_a_second_run_is_locked_out_rather_than_queued(self):
        script = self.unit("docsearch-ingest.sh")
        assert "flock --nonblock" in script
        # Exit 0, not a failure: a skipped tick is the lock working, and
        # marking the unit failed for it would train an operator to ignore the
        # unit's state.
        assert "exit 0" in script

    def test_the_service_is_only_ever_started_by_the_timer(self):
        # No [Install] section means `systemctl enable` on the service itself
        # is refused, so it cannot be accidentally set to run at boot on its
        # own schedule.
        assert "[Install]" not in self.unit("docsearch-ingest.service")

    def test_it_reuses_the_running_container(self):
        script = self.unit("docsearch-ingest.sh")
        # `compose run` would start a second container and reload the embedding
        # model on every tick.
        assert "compose exec -T backend" in script
        assert "compose run" not in script

    def test_the_log_goes_to_the_journal(self):
        service = self.unit("docsearch-ingest.service")
        assert "StandardOutput=journal" in service
        assert "SyslogIdentifier=" in service

    def test_it_does_not_run_as_root(self):
        service = self.unit("docsearch-ingest.service")
        assert "User=" in service
        assert "User=root" not in service
