#!/usr/bin/env python3

import argparse
import asyncio
import json
import random
import re
from pathlib import Path

from ruamel.yaml import YAML

# LIMITATIONS
#   - import_role, include_role, import_playbok, import_tasks, include_tasks
#   - include_vars
#   - limited Jinja2 evaluation


async def evaluate_jinja(jinja, vars):
    if isinstance(jinja, list):
        return [await evaluate_jinja(i, vars) for i in jinja]
    if not isinstance(jinja, str):
        return jinja
    m = re.match(r"{{\s*([\w\d\.]+)\s*}}", jinja)
    if not m:
        return jinja
    var_name = m.group(1).split(".")[0]
    print(f"var_name: {var_name}")
    if isinstance(vars[var_name], asyncio.Task):
        if not vars[var_name].done():
            print(f"Waiting for {var_name}")
            await vars[var_name]
            print(f"{var_name} is ready")
            vars[var_name] = vars[var_name].result()
    return jinja


def task_module_name(task):
    return list(set(task.keys()) - set(["name", "register", "delegate_to"]))[0]


def expander(v):
    if isinstance(v, asyncio.Task):
        if v.done():
            return v.result()
    else:
        return v


async def run_task(task, module, vars):

    args = ""
    for k, v in dict(task[module]).items():
        await evaluate_jinja(v, vars)
        args += " " + f'{k}="{v}"'

    tmp_file = Path(f"/tmp/{random.random()}.json")
    tmp_file.open("w").write(
        json.dumps(
            {
                k: expander(v)
                for k, v in vars.items()
                if (isinstance(v, asyncio.Task) and v.done())
                or not isinstance(v, asyncio.Task)
            }
        )
    )
    cmd_args = [
        "ansible",
        "-m",
        module,
        "-a",
        args,
        "-e",
        f"@{tmp_file.resolve()}",
        "localhost",
    ]
    print(" ".join(cmd_args))
    proc = await asyncio.create_subprocess_exec(
        *cmd_args, stdout=asyncio.subprocess.PIPE
    )
    data = await proc.stdout.read()
    raw_output = data.decode().rstrip()
    await proc.wait()
    print(raw_output)
    # NOTE: output is a JSON structure but the first line starts with a status
    # prefix
    output = json.loads("\n".join(["{"] + raw_output.split("\n")[1:]))
    tmp_file.unlink()
    return output


async def run_playbook(playbook):
    loop = asyncio.get_running_loop()
    scoped_vars = dict(playbook.get("vars", []))
    tl = []
    tasks_stack = list(playbook["tasks"])
    while tasks_stack:
        task_vars = scoped_vars.copy()
        task, *tasks_stack = tasks_stack
        module = task_module_name(task)
        t = loop.create_task(run_task(task, module, vars=task_vars))
        tl.append(t)
        if "register" in task:
            scoped_vars[task["register"]] = t
    await asyncio.wait(tl)


async def main(playbook_path):
    playbooks = YAML().load(playbook_path.open().read())

    for playbook in playbooks:
        await run_playbook(playbook)


parser = argparse.ArgumentParser()
parser.add_argument("playbook_path", type=Path, help="Path of the playbook")
args = parser.parse_args()
asyncio.run(main(playbook_path=args.playbook_path))
