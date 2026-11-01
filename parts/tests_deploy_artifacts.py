"""Static guard rails over the real deployment files: they must keep their safety properties."""
import functools
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
INFRA = ROOT / "infra"
ROOTLESS_PLAYBOOKS = ["deploy", "status", "backup", "fetch-backup", "restore-drill", "restore", "rollback"]
LEGACY = {"deploy-playbook.yaml", "deploy-playbook-podman-quadlet.yaml"}      # rootful; removed in phase R3
TAX_WORDS = ("tax", "healthcare", "kk-tool", "kktool")


def read(rel):
    return (ROOT / rel).read_text()


def group_vars():
    return yaml.safe_load(read("infra/group_vars/partsdb_rootless.yml"))


def code_only(text):
    """The text without comments (whole-line and trailing), for checks on what is actually configured."""
    return "\n".join(re.sub(r"\s+#.*$", "", l) for l in text.splitlines() if l.strip() and not l.strip().startswith("#"))


def active_lines(rel):
    return [l for l in read(rel).splitlines() if l.strip() and not l.strip().startswith("#")]


TEMPLATES = ("parts-db-web.container.j2", "parts-db-backup.service.j2", "parts-db-backup.timer.j2", "manage.sh.j2")


@functools.lru_cache(maxsize=None)
def _render_all():
    """Render every template once with Ansible's own engine, the real group_vars and a fake host."""
    out = Path(tempfile.mkdtemp())
    tasks = "".join(
        f"    - ansible.builtin.template: {{src: '{INFRA}/templates/{t}', dest: '{out}/{t}'}}\n" for t in TEMPLATES)
    play = out / "render.yaml"
    play.write_text(
        "- hosts: localhost\n  gather_facts: false\n"
        f"  vars_files: ['{INFRA}/group_vars/partsdb_rootless.yml']\n"
        "  vars: {ansible_host: host.example.invalid}\n"
        f"  tasks:\n{tasks}"
    )
    ansible = ROOT / ".venv" / "bin" / "ansible-playbook"
    cmd = [str(ansible) if ansible.exists() else "ansible-playbook", "-i", "localhost,", "-c", "local", str(play)]
    subprocess.run(cmd, check=True, capture_output=True)
    return {t: (out / t).read_text() for t in TEMPLATES}


def rendered(template):
    return _render_all()[template]


class DockerfileTests(SimpleTestCase):
    def setUp(self):
        self.text = read("Dockerfile")
        self.lines = active_lines("Dockerfile")

    def test_runs_as_unprivileged_user_with_matching_uid(self):
        self.assertEqual([l for l in self.lines if l.startswith("USER ")], ["USER 10001:10001"])
        self.assertIn("--uid 10001", self.text)
        self.assertEqual(group_vars()["app_uid"], 10001)

    def test_static_files_are_collected_at_build_time(self):
        self.assertIn("python manage.py collectstatic --noinput", self.text)
        self.assertLess(self.text.index("collectstatic"), self.text.index("USER 10001"))

    def test_only_application_code_is_copied(self):
        sources = {src for l in self.lines if l.startswith("COPY ") for src in l.split()[1:-1]}
        self.assertEqual(sources, {"requirements.txt", "manage.py", "healthcheck.py", "config", "parts", "parsers"})

    def test_uses_gunicorn_and_bakes_in_no_secrets(self):
        self.assertIn("gunicorn", self.text)
        self.assertNotIn("runserver", self.text)
        for forbidden in ("SECRET_KEY", "PASSWORD", "TOKEN"):
            self.assertNotIn(forbidden, self.text)

    def test_ignore_files_keep_private_data_out_of_the_build_context(self):
        for name in (".containerignore", ".dockerignore"):
            entries = set(read(name).split())
            for must in (".git", ".venv", "local", "*.sqlite3", ".env", "infra", "docs", "media", "backups", "CLAUDE.md"):
                self.assertIn(must, entries, f"{name} lacks {must}")

    def test_health_script_is_copied_and_targets_the_health_endpoint(self):
        self.assertIn("COPY manage.py healthcheck.py", self.text)
        self.assertIn("/health/", read("healthcheck.py"))


class QuadletTests(SimpleTestCase):
    def setUp(self):
        self.template = read("infra/templates/parts-db-web.container.j2")
        self.unit = rendered("parts-db-web.container.j2")

    def test_secret_is_never_in_the_unit_file(self):
        self.assertIn("EnvironmentFile=/home/homeapps/parts-db/config/env", self.unit)
        self.assertNotIn("SECRET_KEY", code_only(self.unit))

    def test_joins_the_gateway_network_with_the_expected_name_and_user_mapping(self):
        self.assertEqual(re.findall(r"^Network=(.+)$", self.unit, re.M), ["gateway.network"])
        self.assertEqual(re.findall(r"^ContainerName=(.+)$", self.unit, re.M), ["parts-db-web"])
        self.assertIn("UserNS=keep-id:uid=10001,gid=10001", self.unit)
        self.assertIn("User=10001", self.unit)

    def test_published_port_is_loopback_only_by_default(self):
        self.assertEqual(group_vars()["listen_address"], "127.0.0.1")
        ports = re.findall(r"^PublishPort=(.+)$", self.unit, re.M)
        self.assertEqual(ports, ["127.0.0.1:8891:8000"])
        self.assertEqual(re.findall(r"^PublishPort=(.+)$", self.template, re.M), ["{{ listen_address }}:{{ app_port }}:8000"])

    def test_hardening(self):
        for line in ("ReadOnly=true", "NoNewPrivileges=true", "DropCapability=ALL", "Tmpfs=/tmp", "Restart=always", "HealthOnFailure=restart"):
            self.assertIn(line, self.unit)
        self.assertIn("--memory=", self.unit)
        self.assertIn("--pids-limit=", self.unit)

    def test_health_check_is_a_quote_free_script(self):
        cmds = re.findall(r"^HealthCmd=(.+)$", self.unit, re.M)
        self.assertEqual(cmds, ["python /app/healthcheck.py"])
        for ch in ('"', "'", ";", "`", "$"):
            self.assertNotIn(ch, cmds[0])

    def test_exactly_the_three_parts_db_folders_are_mounted(self):
        mounts = re.findall(r"^Volume=(.+)$", self.unit, re.M)
        self.assertEqual(mounts, [
            "/home/homeapps/parts-db/data:/data",
            "/home/homeapps/parts-db/media:/media",
            "/home/homeapps/parts-db/backups:/backups",
        ])
        env = "".join(re.findall(r"^Environment=(.+)$", self.unit, re.M))
        self.assertIn("DJANGO_DB_PATH=/data/db.sqlite3", env)
        self.assertIn("DJANGO_MEDIA_ROOT=/media", env)

    def test_prefix_https_and_hsts_settings_reach_the_app(self):
        env = re.findall(r"^Environment=(.+)$", self.unit, re.M)
        for expected in ("DJANGO_DEBUG_FALSE=1", "DJANGO_URL_PREFIX=/parts-db", "DJANGO_HTTPS=0", "DJANGO_HSTS_SECONDS=0", "DJANGO_BEHIND_TLS_PROXY=1"):
            self.assertIn(expected, env)
        flags = group_vars()["podman_run_flags"]
        for name in ("DJANGO_URL_PREFIX", "DJANGO_HTTPS", "DJANGO_HSTS_SECONDS", "DJANGO_DB_PATH", "DJANGO_MEDIA_ROOT", "DJANGO_BACKUP_DIR"):
            self.assertIn(f"-e {name}=", flags)        # one-off runs (migrate, backup, restore) see the same environment

    def test_deploy_checks_that_the_gateway_network_exists(self):
        text = read("infra/deploy.yaml")
        self.assertIn("{{ quadlet_dir }}/{{ gateway_network }}", text)
        self.assertIn("gateway_network_file.stat.exists", text)


class SudoFreeTests(SimpleTestCase):
    def rootless_files(self):
        return [INFRA / f"{p}.yaml" for p in ROOTLESS_PLAYBOOKS] + [INFRA / "tasks" / "service-session.yml"] + list((INFRA / "templates").glob("*.j2"))

    def test_rootless_playbooks_and_templates_never_escalate(self):
        for path in self.rootless_files():
            if path.name in {"parts-db.container.j2", "parts-db.env.j2"}:
                continue                                  # legacy rootful templates
            text = "\n".join(l for l in path.read_text().splitlines() if not l.strip().startswith("#"))
            self.assertNotRegex(text, r"\bbecome\b", path.name)
            self.assertNotRegex(text, r"\bsudo\b", path.name)
            self.assertNotRegex(text, r"(^|\s)-K(\s|$)", path.name)

    def test_makefile_rootless_targets_have_no_sudo_or_ask_become_pass(self):
        text = read("Makefile")
        rootless = code_only(text.split("# --- LEGACY")[0])
        self.assertNotRegex(rootless, r"\bsudo\b")
        self.assertNotRegex(rootless, r"(^|\s)-K(\s|$)")
        self.assertIn("-K", text.split("# --- LEGACY")[1])           # only the legacy rootful targets still ask for it

    def test_all_rootless_playbooks_run_as_the_service_group_and_check_the_session(self):
        for name in ROOTLESS_PLAYBOOKS:
            text = read(f"infra/{name}.yaml")
            self.assertIn("hosts: partsdb_rootless", text, name)
            self.assertIn("tasks/service-session.yml", text, name)

    def test_makefile_syntax_target_covers_every_rootless_playbook(self):
        line = re.search(r"for p in ([^;]+); do", read("Makefile")).group(1).split()
        self.assertEqual(sorted(line), sorted(f"{p}.yaml" for p in ROOTLESS_PLAYBOOKS))


class DataSafetyTests(SimpleTestCase):
    def texts(self):
        files = [INFRA / f"{p}.yaml" for p in ROOTLESS_PLAYBOOKS] + list((INFRA / "templates").glob("parts-db-*.j2"))
        return {f.name: f.read_text() for f in files}

    def test_nothing_deletes_or_prunes_data(self):
        for name, text in self.texts().items():
            code = code_only(text)
            self.assertNotIn("state: absent", code, name)
            self.assertNotRegex(code, r"--delete\b", name)
            self.assertNotRegex(code, r"(?<![-\w])rm\s+-", name)       # a shell rm, not podman's --rm
            self.assertNotRegex(code, r"(volume|system|container|image)\s+prune", name)

    def test_restore_needs_explicit_confirmation_and_takes_a_safety_backup_first(self):
        text = read("infra/restore.yaml")
        self.assertIn("confirm == 'RESTORE'", text)
        self.assertLess(text.index("--label pre-restore"), text.index("--target-db"))
        self.assertIn("--force", text)                         # moves existing data aside, never deletes it

    def test_deploy_backs_up_before_migrating_and_refuses_a_foreign_data_root(self):
        text = read("infra/deploy.yaml")
        self.assertLess(text.index("--label pre-deploy"), text.index("manage.py migrate"))
        self.assertIn("data_root == service_home ~ '/parts-db'", text)
        self.assertIn("allow_dirty | bool or dirty_tree | length == 0", text)

    def test_backup_timer_is_persistent_and_daily(self):
        timer = read("infra/templates/parts-db-backup.timer.j2")
        self.assertIn("Persistent=true", timer)
        self.assertIn("OnCalendar=*-*-*", timer)
        self.assertIn("backup_db --prune", read("infra/templates/parts-db-backup.service.j2"))

    def test_data_directories_are_private_and_the_secret_file_is_0600(self):
        text = read("infra/deploy.yaml")
        self.assertIn('mode: "0700"', text)
        self.assertIn("creates: \"{{ etc_dir }}/env\"", text)
        self.assertIn("umask 077", text)
        self.assertEqual(len(re.findall(r"no_log: true", text)), 1)


class IsolationFromTaxAppTests(SimpleTestCase):
    def test_no_tax_name_in_any_rendered_artifact_or_variable(self):
        blobs = {t: rendered(t) for t in ("parts-db-web.container.j2", "parts-db-backup.service.j2", "parts-db-backup.timer.j2", "manage.sh.j2")}
        blobs["group_vars"] = read("infra/group_vars/partsdb_rootless.yml")
        for name, text in blobs.items():
            code = code_only(text)
            for word in TAX_WORDS:
                self.assertNotIn(word, code.lower(), f"{name} mentions {word}")

    def test_every_mount_source_and_path_variable_is_under_the_parts_db_root(self):
        gv = group_vars()
        self.assertEqual(gv["app_name"], "parts-db")
        self.assertEqual(gv["container_name"], "parts-db-web")
        self.assertEqual(gv["image_repo"], "localhost/parts-db")
        self.assertEqual(gv["data_root"], "{{ service_home }}/{{ app_name }}")
        self.assertEqual(gv["etc_dir"], "{{ data_root }}/config")
        for line in re.findall(r"-v (\{\{ \w+ \}\}\S*|\S+)", gv["podman_run_flags"]):
            self.assertTrue(line.startswith("{{ data_root }}/"), line)
        for mount in re.findall(r"^Volume=(\S+?):", rendered("parts-db-web.container.j2"), re.M):
            self.assertTrue(mount.startswith("/home/homeapps/parts-db/"), mount)

    def test_playbooks_that_touch_images_only_name_the_parts_db_repository(self):
        for name in ROOTLESS_PLAYBOOKS:
            text = read(f"infra/{name}.yaml")
            for cmd in re.findall(r"podman (?:images|rmi|image \w+|tag)[^\n]*", text):
                self.assertTrue("image_repo" in cmd or "image_new" in cmd, f"{name}: {cmd}")
            self.assertNotRegex(text, r"systemctl --user (stop|restart|disable)", name)
        # units are addressed by the parts-db names only
        for name in ROOTLESS_PLAYBOOKS:
            for unit in re.findall(r'name: "([^"]*\.(?:service|timer))"', read(f"infra/{name}.yaml")):
                self.assertRegex(unit, r"container_name|app_name", f"{name}: {unit}")

    def test_deploy_refuses_when_data_root_is_not_the_parts_db_folder(self):
        text = read("infra/deploy.yaml")
        self.assertIn("data_root == service_home ~ '/parts-db'", text)
        self.assertIn("container_name == 'parts-db-web'", text)


class NoCaddyTests(SimpleTestCase):
    def test_no_caddy_configuration_anywhere_in_the_repo(self):
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        for rel in files:
            self.assertNotIn("caddy", rel.lower(), rel)
            path = ROOT / rel
            if path.suffix in {".py", ".yaml", ".yml", ".j2", ".ini", ".example"} and rel != "parts/tests_deploy_artifacts.py":
                self.assertNotRegex(path.read_text(errors="ignore"), r"(?i)reverse_proxy|Caddyfile|caddy\.container", rel)


class SecretsNotInGitTests(SimpleTestCase):
    def test_private_files_are_ignored(self):
        for path in ("infra/group_vars/all.yml", "infra/inventory.ini", "infra/inventory.local.ini", ".env", "local/backups/x.tar.gz", "media/x.pdf", "db.sqlite3"):
            result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
            self.assertEqual(result.returncode, 0, f"{path} is not git-ignored")

    def test_example_files_use_fake_values_only(self):
        for rel in ("infra/inventory.ini.example", "infra/group_vars/all.yml.example", "infra/group_vars/partsdb_rootless.yml"):
            text = read(rel)
            self.assertNotRegex(text, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b(?<!127\.0\.0\.1)", rel)
            self.assertNotIn("fritz.box", text, rel)
