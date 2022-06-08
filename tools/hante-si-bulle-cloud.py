#!/usr/bin/env python3

import importlib.util
import sys
import ansible.utils.collection_loader
import ansible.plugins.loader
import argparse
import asyncio
import json
import random
from pathlib import Path
from ansible.errors import AnsibleUndefinedVariable
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

from ruamel.yaml import YAML

# NOT IMPLEMENTED
#   - import_role, include_role, import_playbok, import_tasks
#   - include_vars
#   - limited Jinja2 evaluation
#   - retries condition
#   - ignore_errors
#   - the use of the same register name several times.
#   - add_host

import logging

logging.basicConfig()
logging.getLogger().setLevel(logging.DEBUG)
http_logger = logging.getLogger("aiohttp.client")
http_logger.setLevel(logging.DEBUG)
http_logger.propagate = True


def load_module(module_name):
    if not ansible.utils.collection_loader.AnsibleCollectionRef.is_valid_fqcr(module_name):
        raise Exception("module must be a FQCN")
    
    match module_name.split("."):
        case [s0, s1, s2]:
            internal_module_name = f"ansible.collections.ansible_collections.{s0}.{s1}.plugins.modules.{s2}"
        case _:
            raise Exception()

    file_path = ansible.plugins.loader.module_loader.find_plugin(module_name)
    if not file_path:
        raise Exception(f"Cannot find {module_name}")
    
    spec = importlib.util.spec_from_file_location(internal_module_name, file_path)
    pymod = importlib.util.module_from_spec(spec)
    sys.modules[internal_module_name] = pymod
    spec.loader.exec_module(pymod)
    print(f'  - module_utils: {sys.modules["ansible_collections.vmware.vmware_rest.plugins.module_utils.vmware_rest"]}')
    print(f'  - module_utils: {id(sys.modules["ansible_collections.vmware.vmware_rest.plugins.module_utils.vmware_rest"])}')
    return pymod


def find_undef_variable_in_jinja(jinja, vars):

    loader = DataLoader()

    vars_no_pending_task = {
        k: expander(v)
        for k, v in vars.items()
        if (
            (isinstance(v, asyncio.Task) and v.done())
            or not isinstance(v, asyncio.Task)
        )
    }
    
    templar = Templar(loader=loader, variables=vars_no_pending_task)
    try:
        templar.template(jinja)
    except AnsibleUndefinedVariable as e:
        if str(e).startswith("'"):
            found = str(e).split(" ")[0].rstrip("'").lstrip("'")
            print(f"  -> undef var found in {jinja}: {found} -- {e}")
            return found


def expand_jinja2_from_args(args, vars):
    loader = DataLoader()

    vars_no_pending_task = {
        k: expander(v)
        for k, v in vars.items()
        if (
            (isinstance(v, asyncio.Task) and v.done())
            or not isinstance(v, asyncio.Task)
        )
    }

    templar = Templar(loader=loader, variables=vars_no_pending_task)

    for k, v in args.items():
        if isinstance(v, dict):
            args[k] = expand_jinja2_from_args(v, vars)
        elif isinstance(v, str):
            args[k] = templar.template(v)
        else:
            pass
    return args 
        

async def wait_for_requirements(jinja, vars):
    assert isinstance(vars, dict)
    while True:
        var_name = find_undef_variable_in_jinja(jinja, vars)
        if not var_name:
            break
        print(f"Waiting for {var_name}")
        await vars[var_name]
        print(f"{var_name} is ready: {vars[var_name].result()}")
        await asyncio.sleep(0.01)


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


def get_args(task, module):
    if isinstance(task[module], str):
        task_args = {}
        if "=" in task[module]:
            for i in task[module].split(" "):
                try:
                    k, v = i.split("=")
                    task_args[k] = v
                except ValueError as e:
                    raise Exception(f"{e} -- {i}")
        return task_args
    elif task[module]:
        return json.loads(json.dumps(task[module]))
    else:
        return {}
    
async def prep_args(task, module, vars):
    args = ""
    try:
        for k, v in get_args(task, module).items():
            assert isinstance(vars, dict)
            await wait_for_requirements(v, vars)
            args += " " + f'{k}="{v}"'
    except ValueError:
        print(f"prep_args: invalid input: {args}")
    return args


async def run_ansible(cmd_args):
    proc = await asyncio.create_subprocess_exec(
        *cmd_args, stdout=asyncio.subprocess.PIPE,
        
        stderr= asyncio.subprocess.DEVNULL  # NOTE: To avoid ansible's devel branch warnings
    )

    data = await proc.stdout.read()
    raw_output = data.decode().rstrip()
    await proc.wait()
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
    tasks = list(YAML().load(task_file.open().read()))
    return tasks


async def evaluate_jinja2(jinja2, vars):
    assert isinstance(vars, dict)
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
        args = get_args(task, module)

        if "with_items" in task:
            assert isinstance(task_vars, dict)
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
            for k, v in args.items():
                scoped_vars[k] = loop.create_task(evaluate_jinja2(v, task_vars))
            continue
#        if module in ("assert", "ansible.builtin.assert"):
#             that = args.get("that", [])
#             if isinstance(that, list):
#                 for t in that:
#                     await wait_for_requirements(t, task_vars)
#        if module in ("debug", "ansible.builtin.debug"):
#             await wait_for_requirements(args.get("var"), task_vars)
#             await wait_for_requirements(args.get("msg"), task_vars)
                   
        if module.startswith("vmware.vmware_rest"):
            pymod = load_module(module)
            params = task[module] or {}

            for v in args.values():
                await wait_for_requirements(v, task_vars)
            args = expand_jinja2_from_args(args, task_vars)
            from pprint import pprint
            pprint("00000>")
            pprint(args)
            pprint("<00000")
            
            t = loop.create_task(pymod.main(params=args))
        else:
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
