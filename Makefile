# Development and deployment shortcuts. Routine deployment commands connect as the rootless service user (`homeapps`)
# and need NO sudo password. Only the legacy rootful targets (deploy-podman, deploy-docker) use sudo (-K); they are
# the rollback path and are removed in phase R3. Every target is explained in docs/deployment.md.
PYTHON := .venv/bin/python
PIP := $(PYTHON) -m pip
ANSIBLE_PLAYBOOK := ANSIBLE_CONFIG=infra/ansible.cfg .venv/bin/ansible-playbook
ANSIBLE_GALAXY := .venv/bin/ansible-galaxy
# EXTRA passes more options, e.g.  make status EXTRA="-e use_u01=false"  before the data has been relocated to /u01.
ANSIBLE := $(ANSIBLE_PLAYBOOK) -i infra/inventory.ini $(EXTRA)
HOST := $(shell awk '/^\[partsdb_rootless\]/{f=1;next} /^\[/{f=0} f && NF {h=$$1; for (i=2;i<=NF;i++) if ($$i ~ /^ansible_host=/) {h=$$i; sub("ansible_host=","",h)}; print h; exit}' infra/inventory.ini 2>/dev/null)
USER_AT := $(shell awk '/^\[partsdb_rootless\]/{f=1;next} /^\[/{f=0} f && NF {for (i=2;i<=NF;i++) if ($$i ~ /^ansible_user=/) {sub("ansible_user=","",$$i); print $$i "@"}; exit}' infra/inventory.ini 2>/dev/null)
BACKUP_ARG := $(if $(BACKUP),-e backup_file=$(BACKUP),)
CONFIRM_ARG := $(if $(CONFIRM),-e confirm=$(CONFIRM),)
MANAGE := bin/parts-db-manage

.PHONY: setup test check ansible-setup syntax deploy status backup-now fetch-backup restore-drill restore rollback relocate-storage rollback-storage \
        createsuperuser changepassword deploy-docker deploy-podman

setup:
	$(PIP) install -r requirements.txt

test:
	$(PYTHON) manage.py test

check:
	$(PYTHON) manage.py check

# --- Rootless deployment (no sudo) ------------------------------------------------------------------------
ansible-setup:
	$(PIP) install ansible
	$(ANSIBLE_GALAXY) collection install -r infra/requirements.yml

syntax:
	@for p in deploy.yaml status.yaml backup.yaml fetch-backup.yaml restore-drill.yaml restore.yaml rollback.yaml relocate-storage.yaml rollback-storage.yaml; do \
	  $(ANSIBLE) --syntax-check infra/$$p >/dev/null && echo "ok  $$p" || exit 1; done

deploy:                ## build and deploy the committed revision as the service user
	$(ANSIBLE) infra/deploy.yaml

status:                ## read-only overview
	$(ANSIBLE) infra/status.yaml

backup-now:            ## verified backup (database + PDFs) on the server right now
	$(ANSIBLE) infra/backup.yaml

fetch-backup:          ## copy the newest (or BACKUP=name) backup to local/backups and verify it
	$(ANSIBLE) infra/fetch-backup.yaml $(BACKUP_ARG)

restore-drill:         ## prove a backup restores, without touching live data (BACKUP=name optional)
	$(ANSIBLE) infra/restore-drill.yaml $(BACKUP_ARG)

restore:               ## restore live data from BACKUP=backup-....tar.gz (asks you to type RESTORE)
	@test -n "$(BACKUP)" || { echo "usage: make restore BACKUP=backup-YYYYMMDD-HHMMSS.tar.gz"; exit 2; }
	$(ANSIBLE) infra/restore.yaml $(BACKUP_ARG) $(CONFIRM_ARG)

rollback:              ## previous image (add BACKUP=pre-deploy backup to restore the data too)
	$(ANSIBLE) infra/rollback.yaml $(BACKUP_ARG)

relocate-storage:      ## copy data, PDFs and backups to /u01, compare every file, switch only after you type SWITCH (never deletes)
	$(ANSIBLE) infra/relocate-storage.yaml

rollback-storage:      ## point the app back at the old folders in the home folder (asks you to type ROLLBACK)
	$(ANSIBLE) infra/rollback-storage.yaml

createsuperuser:       ## create your login on the server (interactive)
	ssh -t $(USER_AT)$(HOST) $(MANAGE) createsuperuser

changepassword:        ## USERNAME=... set a new password
	@test -n "$(USERNAME)" || { echo "usage: make changepassword USERNAME=name"; exit 2; }
	ssh -t $(USER_AT)$(HOST) $(MANAGE) changepassword $(USERNAME)

# --- LEGACY, removed in phase R3 (rollback path while the old rootful instance still serves) ---------------
deploy-docker:
	$(ANSIBLE_PLAYBOOK) -i infra/inventory.ini infra/deploy-playbook.yaml -K -vv

deploy-podman:
	$(ANSIBLE_PLAYBOOK) -i infra/inventory.ini infra/deploy-playbook-podman-quadlet.yaml -K -vv
