#!/usr/bin/env bash

ANSIBLE_ENGINE="python3 $HOME/git_repos/hante-si-bulle-cloud/tools/hante-si-bulle-cloud.py"
#ANSIBLE_ENGINE="ansible-playbook -vvv"

session=$(curl -X PUT --json "{\"config\": {\"platform\": \"aws\", \"version\": \"sts\"}, \"auth\": {\"remote\": {\"key\": \"$(cat ~/.ansible-core-ci.key)\", \"nonce\": null}}, \"threshold\": 1}" https://ansible-core-ci.testing.ansible.com/dev/aws/$(uuidgen))
resource_prefix=$(uuidgen)

$ANSIBLE_ENGINE run.yaml \
	-e resource_prefix=${resource_prefix} \
	-e aws_access_key=$(echo $session|jq -r .aws.credentials.access_key) \
	-e aws_secret_key=$(echo $session|jq -r .aws.credentials.secret_key) \
	-e security_token=$(echo $session|jq -r .aws.credentials.session_token) \
	-e ansible_system=Linux \
	-e ansible_distribution=Fedora \
	-e aws_region=us-west-1
