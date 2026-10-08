PYTHON := .venv/bin/python
PIP := $(PYTHON) -m pip
ANSIBLE_PLAYBOOK := ANSIBLE_CONFIG=infra/ansible.cfg .venv/bin/ansible-playbook
ANSIBLE_GALAXY := .venv/bin/ansible-galaxy

.PHONY: setup test check ansible-setup deploy-docker deploy-podman caddy-proxy caddy-disable

setup:
	$(PIP) install -r requirements.txt

test:
	$(PYTHON) manage.py test

check:
	$(PYTHON) manage.py check

ansible-setup:
	$(PIP) install ansible
	$(ANSIBLE_GALAXY) collection install -r infra/requirements.yml

deploy-docker:
	$(ANSIBLE_PLAYBOOK) -i infra/inventory.ini infra/deploy-playbook.yaml -K -vv

deploy-podman:
	$(ANSIBLE_PLAYBOOK) -i infra/inventory.ini infra/deploy-playbook-podman-quadlet.yaml -K -vv

caddy-proxy:
	$(ANSIBLE_PLAYBOOK) -i infra/inventory.ini infra/configure-caddy-playbook.yaml -e caddy_mode=proxy -K -vv

caddy-disable:
	$(ANSIBLE_PLAYBOOK) -i infra/inventory.ini infra/configure-caddy-playbook.yaml -e caddy_mode=disable -K -vv
