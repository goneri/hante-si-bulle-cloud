#!/usr/bin/env bash

ANSIBLE_ENGINE="python3 $HOME/git_repos/hante-si-bulle-cloud/tools/hante-si-bulle-cloud.py"
#ANSIBLE_ENGINE="ansible-playbook -vvv"

$ANSIBLE_ENGINE run.yaml
