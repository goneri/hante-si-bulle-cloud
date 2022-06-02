#!/usr/bin/env python3

import argparse
import asyncio
import json
import random
from pathlib import Path

import jinja2
from ruamel.yaml import YAML

# NOT IMPLEMENTED
#   - import_role, include_role, import_playbok, import_tasks
#   - include_vars
#   - limited Jinja2 evaluation
#   - retries condition
#   - ignore_errors
#   - the use of the same register name several times.
#   - add_host


async def wait_for_task(var_name, vars):
    var_value = vars.get(var_name)
    if isinstance(var_value, asyncio.Task):
        if not var_value.done():
            print(f"Waiting for {var_name}")
            await var_value
            print(f"{var_name} is ready: {var_value.result()}")


def find_undef_variable_in_jinja(jinja, vars):
    missing_vars = []

    class CollectingUndefined(jinja2.Undefined):
        def __init__(self, name):
            if name not in ["env"]:
                missing_vars.append(name)
            super().__init__(name)

    env = jinja2.Environment(undefined=CollectingUndefined)
    template = env.from_string(jinja)

    def dummy_func(*args):
        return

    vars_no_pending_task = {
        k: expander(v)
        for k, v in vars.items()
        if (
            (isinstance(v, asyncio.Task) and v.done())
            or not isinstance(v, asyncio.Task)
        )
    }
    vars_no_pending_task["lookup"] = dummy_func
    try:
        template.render(**vars_no_pending_task)
    except jinja2.exceptions.UndefinedError:
        # NOTE: happens with dict variable like var1.subkey1
        pass
    return missing_vars


async def wait_for_requirements(jinja, vars):
    if isinstance(jinja, list):
        return [await wait_for_requirements(i, vars) for i in jinja]
    if not isinstance(jinja, str):
        return jinja

    while True:
        undef_vars = find_undef_variable_in_jinja(jinja, vars)
        if not undef_vars:
            break
        for var_name in undef_vars:
            await vars[var_name]
        await asyncio.sleep(50)


def task_module_name(task):
    return list(
        set(task.keys())
        - set(
            [
                "name",
                "register",
                "delegate_to",
                "retries",
                "delay",
                "until",
                "with_items",
                "ignore_errors",
            ]
        )
    )[0]


def expander(v):
    if isinstance(v, asyncio.Task):
        if v.done():
            return v.result()
    else:
        return v


async def prep_args(task, module, vars):
    args = ""
    if isinstance(task[module], str):
        if "=" in task[module]:
            for i in task[module].split(" "):
                task_args = {k: v for [k, v] in i.split("=")}
        return task[module]
    else:
        task_args = task[module] or {}
    try:
        for k, v in dict(task_args).items():
            await wait_for_requirements(v, vars)
            args += " " + f'{k}="{v}"'
    except ValueError:
        print(f"prep_args: invalid input: {task_args}")
    return args


async def run_ansible(cmd_args):
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
    return output


def serialize_vars_on_disk(vars):
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
    return tmp_file


async def run_task(task, module, vars):

    args = await prep_args(task, module, vars)

    task_vars = {**task.get("vars", {}), **vars}
    tmp_file = serialize_vars_on_disk(task_vars)
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
    output = await run_ansible(cmd_args)
    # tmp_file.unlink()
    return output


def import_tasks_file(task_file):
    print(f"Loading tasks from {task_file}")
    tasks = list(YAML().load(task_file.open().read()))
    return tasks


async def evaluate_jinja2(jinja2, vars):
    await wait_for_requirements(jinja2, vars)
    tmp_file = serialize_vars_on_disk(vars)
    cmd_args = [
        "ansible",
        "-m",
        "debug",
        "-a",
        f'msg="{jinja2}"',
        "-e",
        f"@{tmp_file.resolve()}",
        "localhost",
    ]
    output = await run_ansible(cmd_args)
    # tmp_file.unlink()
    return output["msg"]


async def run_playbook(playbook):
    loop = asyncio.get_running_loop()
    scoped_vars = dict(playbook.get("vars", []))
    tl = []
    tasks_stack = list(playbook["tasks"])
    while tasks_stack:
        task_vars = scoped_vars.copy()
        task, *tasks_stack = tasks_stack
        module = task_module_name(task)

        if "with_items" in task:
            items = await evaluate_jinja2(task["with_items"], task_vars)
            if isinstance(items, list):
                for item in items:
                    if "vars" not in task:
                        task["vars"] = {}
                    task["vars"]["item"] = item
                    tasks_stack.append(task)
            else:
                print(f"WARNING: items not a list: {items}")
            continue

        if module == "include_tasks":
            tasks_stack = import_tasks_file(Path(task["include_tasks"])) + tasks_stack
            continue
        if module in ("set_fact", "ansible.builtin.set_fact"):
            for k, v in task[module].items():
                scoped_vars[k] = loop.create_task(evaluate_jinja2(v, task_vars))
            continue

        t = loop.create_task(run_task(task, module, vars=task_vars))
        tl.append(t)
        if "register" in task:
            scoped_vars[task["register"]] = t

    if tl:
        await asyncio.wait(tl)


async def main(playbook_path):
    playbooks = YAML().load(playbook_path.open().read())

    for playbook in playbooks:
        await run_playbook(playbook)


parser = argparse.ArgumentParser()
parser.add_argument("playbook_path", type=Path, help="Path of the playbook")
args = parser.parse_args()
asyncio.run(main(playbook_path=args.playbook_path))
