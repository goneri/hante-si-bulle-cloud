#!/usr/bin/env python3

import asyncio
import json
import re
import subprocess

from ruamel.yaml import YAML


async def evaluate_jinja(jinja, vars):
    if isinstance(jinja, list):
        return [evaluate_jinja(i, vars) for i in jinja]
    if not isinstance(jinja, str):
        return jinja
    m = re.match(r"{{\s*([\w\d\.]+)\s*}}", jinja)
    if not m:
        return jinja
    var = vars
    for i in m.group(1).split("."):
        var = var[i]
        if isinstance(var, asyncio.Task):
            if not var.done():
                print(f"Waiting for {i}")
                await var
            print(f"{i} is ready")
            var = var.result()
    return var


async def run_task(task, vars={}):
    module = list(set(task.keys()) - set(["name", "register", "delegate_to"]))[0]
    args = ""
    for k, raw_v in dict(task[module]).items():
        v = await evaluate_jinja(raw_v, vars)
        args += " " + f'{k}="{v}"'
    cmd_args = [
        "ansible",
        "-m",
        module,
        "-a",
        args,
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
    output = json.loads("\n".join(["{"] + raw_output.split("\n")[1:]))
    return output


async def run_playbook(playbook):
    loop = asyncio.get_running_loop()
    scoped_vars = dict(playbook.get("vars", []))
    tl = []
    for task in playbook["tasks"]:
        print("-------------------------------------")
        t = loop.create_task(run_task(task, vars=scoped_vars))
        tl.append(t)
        if "register" in task:
            scoped_vars[task["register"]] = t
    await asyncio.wait(tl)


async def main():
    with open("playbook.yaml", "r") as fd:
        playbooks = YAML().load(fd.read())

    for playbook in playbooks:
        await run_playbook(playbook)


asyncio.run(main())
