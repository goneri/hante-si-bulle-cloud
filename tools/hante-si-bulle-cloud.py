#!/usr/bin/env python3

import argparse
import asyncio
import importlib.util
import json
import logging
import random
import sys
from pathlib import Path

import ansible.plugins.loader
import ansible.utils.collection_loader
from ansible.errors import AnsibleUndefinedVariable
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ruamel.yaml import YAML
from ansible.module_utils.parsing.convert_bool import BOOLEANS_TRUE

# NOT IMPLEMENTED
#   - import_role, include_role and role in general
#   - import_playbok
#   - include_vars
#   - retries condition
#   - ignore_errors
#   - the use of the same register name several times.
#   - add_host and inventory


FORMAT = "%(asctime)s➤\n%(message)s"
import datetime as dt

logging.basicConfig(format=FORMAT, datefmt="%H:%M:%S")
http_logger = logging.getLogger("aiohttp.client")
http_logger.setLevel(logging.DEBUG)
http_logger.propagate = True


class HanteSiBulleCloudJinjaError(Exception):
    pass


class HanteSiBulleCloudAnsibleHasFailed(Exception):
    pass


def load_module(module_name):
    if not ansible.utils.collection_loader.AnsibleCollectionRef.is_valid_fqcr(
        module_name
    ):
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
    print(
        f'  - module_utils: {sys.modules["ansible_collections.vmware.vmware_rest.plugins.module_utils.vmware_rest"]}'
    )
    print(
        f'  - module_utils: {id(sys.modules["ansible_collections.vmware.vmware_rest.plugins.module_utils.vmware_rest"])}'
    )
    return pymod


def find_undef_variable_in_jinja(jinja, task_vars, task_run_id):
    assert isinstance(task_vars, dict)
    loader = DataLoader()

    vars_no_pending_task = {
        k: expander(v)
        for k, v in task_vars.items()
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
            if "has no attribute" in str(e):
                from pprint import pprint

                print(f"{task_run_id} ⚠️jinja: {jinja}: {e}")
                return
            found = str(e).split(" ")[0].rstrip("'").lstrip("'")
            print(f"{task_run_id} -> undef var found in {jinja}: {found} -- {e}")
            return found


async def evaluate_jinja2(jinja2_str, task_vars, task_run_id):
    assert isinstance(task_vars, dict)
    await wait_for_requirements(jinja2_str, task_vars, task_run_id)

    loader = DataLoader()

    vars_no_pending_task = {
        k: expander(v)
        for k, v in task_vars.items()
        if (
            (isinstance(v, asyncio.Task) and v.done())
            or not isinstance(v, asyncio.Task)
        )
    }

    templar = Templar(loader=loader, variables=vars_no_pending_task)

    return templar.template(jinja2_str)


def expand_jinja2_from_args(args, task_vars):
    loader = DataLoader()

    vars_no_pending_task = {
        k: expander(v)
        for k, v in task_vars.items()
        if (
            (isinstance(v, asyncio.Task) and v.done())
            or not isinstance(v, asyncio.Task)
        )
    }

    templar = Templar(loader=loader, variables=vars_no_pending_task)

    for k, v in args.items():
        if isinstance(v, dict):
            args[k] = expand_jinja2_from_args(v, task_vars)
        elif isinstance(v, str):
            args[k] = templar.template(v)
        else:
            pass
    return args


async def wait_for_requirements(jinja, task_vars, task_run_id):
    assert isinstance(task_vars, dict)
    while True:
        var_name = find_undef_variable_in_jinja(jinja, task_vars, task_run_id)
        if not var_name:
            logging.info(f"{task_run_id} 🔘 requirements fulfill for: {jinja}")
            return
        logging.debug(f"{task_run_id} Waiting for {var_name} because of {jinja}")
        try:
            await task_vars[var_name]
        except KeyError:
            logging.debug(
                f"{task_run_id} var {var_name} not found in {task_vars.keys()}"
            )
            raise
        print(f"{task_run_id} ✅ {var_name} var is ready")
        await asyncio.sleep(0.01)


def task_module_name(task):
    return list(
        set(task.keys())
        - set(
            [
                "delay",
                "delegate_to",
                "ignore_errors",
                "loop_control",
                "name",
                "no_log",
                "register",
                "retries",
                "until",
                "vars",
                "when",
                "with_items",
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
        as_dict = json.loads(json.dumps(task[module]))
        if not isinstance(as_dict, dict):
            raise ValueError(f">>>{as_dict}<<<")
        return as_dict
    else:
        return {}


async def prep_args(task, module, task_vars, task_run_id):
    args = ""
    if module in ["command", "shell"]:
        await wait_for_requirements(task[module], task_vars, task_run_id)
        return task[module]

    try:
        for k, v in get_args(task, module).items():
            assert isinstance(task_vars, dict)
            await wait_for_requirements(v, task_vars, task_run_id)
            args += " " + f'{k}="{v}"'
    except ValueError:
        logging.error(f"{task_run_id} prep_args: invalid input: {args}")
    return args


async def run_ansible(cmd_args, task_run_id):
    logging.debug(f"{task_run_id}✨ {cmd_args}")
    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,  # NOTE: To avoid ansible's devel branch warnings
    )

    data = await proc.stdout.read()
    raw_output = data.decode().rstrip()
    await proc.wait()
    # NOTE: output is a JSON structure but the first line starts with a status
    # prefix
    logging.debug(f"{task_run_id}✨ {cmd_args}")
    logging.debug("\n".join([f"{task_run_id} 📌 {l}" for l in raw_output.split("\n")]))
    likely_json = raw_output.split(" => ", maxsplit=1)[1]
    try:
        output = json.loads(likely_json)
        assert isinstance(output, dict)
    except json.decoder.JSONDecodeError:
        logging.warning(
            f"{task_run_id}⚠️ Cannot decode this JSON structure: {likely_json}"
        )
        raise
    return output


def serialize_vars_on_disk(task_vars):
    tmp_file = Path(f"/tmp/{random.random()}.json")
    tmp_file.open("w").write(
        json.dumps(
            {
                k: expander(v)
                for k, v in task_vars.items()
                if (isinstance(v, asyncio.Task) and v.done())
                or not isinstance(v, asyncio.Task)
            }
        )
    )
    return tmp_file


async def run_task(task, module=None, task_vars=None, task_run_id=None):
    assert task_run_id
    if not module:
        raise ValueError
    assert isinstance(task_vars, dict)

    args = await prep_args(task, module, task_vars, task_run_id)

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
    output = await run_ansible(cmd_args, task_run_id)
    # tmp_file.unlink()
    return None, output


def import_tasks_file(task_file, task_vars):
    assert isinstance(task_vars, dict)
    tasks = []
    for t in list(YAML().load(task_file.open().read())):
        if "vars" not in t:
            t["vars"] = {}
        for k, v in task_vars.items():
            t["vars"][k] = v
        tasks.append(t)
    return tasks


def add_jinja_brackets(input):
    if isinstance(input, list):
        return [add_jinja_brackets(i) for i in input]
    elif not isinstance(input, str):
        return

    if "{{" in input:
        return input
    else:
        return "{{" + input + "}}"


async def create_task(name, func, kwargs, task_vars=None, when=None, task_run_id=None):
    assert task_run_id
    if not isinstance(kwargs, dict):
        raise ValueError
    if not isinstance(task_vars, dict):
        raise ValueError
    loop = asyncio.get_running_loop()

    async def coro():
        if when:
            logging.debug(
                f"{task_run_id} ⏸️ Waiting for when condition: {when} [{name}]"
            )
            await wait_for_requirements(
                add_jinja_brackets(when), task_vars, task_run_id
            )
            logging.debug(f"{task_run_id} ▶️ Done waiting for: {when} [{name}]")
        # Special case with assert, each line is actually a template
        if kwargs["module"] in ("assert", "ansible.builtin.assert"):
            for l in kwargs["task"][kwargs["module"]]["that"]:
                await wait_for_requirements(
                    add_jinja_brackets(l), task_vars, task_run_id
                )
        if kwargs["module"] in ("debug", "ansible.builtin.debug"):
            var = kwargs["task"][kwargs["module"]].get("var", "")
            await wait_for_requirements(
                add_jinja_brackets(var), task_vars, task_run_id)
        _, ret = await func(**kwargs, task_run_id=task_run_id)
        return ret

    return loop.create_task(coro())


async def expand_loops(task, task_vars, task_run_id):
    match task:
        case {"with_items": _}:
            loop_keyword = "with_items"
        case {"loop": _}:
            loop_keyword = "loop"
        case _:
            return

    assert isinstance(task_vars, dict)
    items = await evaluate_jinja2(task[loop_keyword], task_vars, task_run_id)
    try:
        loop_var = task["loop_control"]["loop_var"]
    except KeyError:
        loop_var = "item"
    logging.info(f"{task_run_id} with_items/items: {items}, loop_var={loop_var}")
    new_tasks = []
    if isinstance(items, list):
        for item in items:
            if "vars" not in task:
                task["vars"] = {}
            cloned_task = task.copy()
            cloned_task["vars"] = task["vars"].copy()
            del cloned_task[loop_keyword]
            logging.info(f"{task_run_id} item: {item}")
            cloned_task["vars"][loop_var] = item
            new_tasks.append(cloned_task)
    else:
        logging.error(f"items not a list: ¨{items}¨")
        raise ValueError(f"items not a list: {items}")
    return new_tasks


async def run_playbook(playbook, extra_vars=None):
    if not extra_vars:
        extra_vars = {}
    loop = asyncio.get_running_loop()
    scoped_vars = dict(playbook.get("vars", {}))
    tl = []
    tasks_stack = list(playbook["tasks"])
    while tasks_stack:
        task_run_id = random_emojis()
        task, *tasks_stack = tasks_stack
        task_vars = extra_vars | scoped_vars.copy() | task.get("vars", {})
        task_name = task.get("name", "noname")
        module = task_module_name(task)

        try:
            new_tasks = await expand_loops(task, task_vars, task_run_id)
            if new_tasks:
                tasks_stack = new_tasks + tasks_stack
                continue

            if "block" in task:
                assert isinstance(task["block"], list)
                block_tasks = []
                for t in task["block"]:
                    if "vars" not in t:
                        t["vars"] = {}
                    for k, v in task_vars.items():
                        t["vars"][k] = v
                    block_tasks.append(t)
                tasks_stack = block_tasks + tasks_stack
                # TODO handle the always block
                continue

            if module == "include_tasks":
                tasks_stack = (
                    import_tasks_file(Path(task["include_tasks"]), task.get("vars", {}))
                    + tasks_stack
                )
                continue
            args = get_args(task, module)
            if module in ("set_fact", "ansible.builtin.set_fact"):
                for k, v in args.items():
                    scoped_vars[k] = loop.create_task(
                        evaluate_jinja2(v, task_vars, task_run_id)
                    )
                continue
            if module in ("pause", "ansible.builtin.pause"):
                seconds = task.get("seconds", 0)
                minutes = task.get("minutes", 0)
                t = loop.create_task(asyncio.sleep(seconds + minutes * 60))
                tl.append(t)
                if "register" in task:
                    scoped_vars[task["register"]] = t
                continue

            when = task.get("when")
            logging.info(f"{task_run_id}🎬 starting task: {task_name}")
            if module.startswith("vmware.vmware_rest"):
                pymod = load_module(module)
                params = task[module] or {}

                for v in args.values():
                    await wait_for_requirements(v, task_vars, task_run_id)
                args = expand_jinja2_from_args(args, task_vars)

                t = await create_task(
                    task_name,
                    pymod.amain,
                    kwargs={"params": args},
                    task_vars=task_vars,
                    when=when,
                    task_run_id=task_run_id,
                )
            else:
                t = await create_task(
                    task_name,
                    run_task,
                    kwargs={"task": task, "module": module, "task_vars": task_vars},
                    task_vars=task_vars,
                    when=when,
                    task_run_id=task_run_id,
                )
            tl.append(t)
            if "register" in task:
                print(f"{task_run_id} register: {task['register']} for {task_name}")
                scoped_vars[task["register"]] = t
        except AnsibleUndefinedVariable:
            if task.get("ignore_errors") not in BOOLEANS_TRUE:
                logging.info(f"Task failure: {task_name}")
                raise

    if tl:
        await asyncio.wait(tl)


async def main(playbook_path, extra_vars):
    playbooks = YAML().load(playbook_path.open().read())

    for playbook in playbooks:
        await run_playbook(playbook, extra_vars=extra_vars)


class AddToDict(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is None:
            setattr(namespace, self.dest, values)
        for k, v in values.items():
            getattr(namespace, self.dest)[k] = v


def kv_type(string):
    k, v = string.split("=", maxsplit=1)
    return {k: v}


def random_emojis():
    pool = list("🏉🥁🪗🎸🎭🧩🎱🥊🍋🥝🫒🍓" "🍍🥐🥖🧀🥞🍄🍆🍚🍯🍺🧊🍾" "🍷🍬🍫🥡🧭🍝🎧🧨🔪🍉⚽")
    random.shuffle(pool)
    return "".join(pool[:4])


parser = argparse.ArgumentParser()
parser.add_argument("playbook_path", type=Path, help="Path of the playbook")
parser.add_argument(
    "-e", help="Extra variable", dest="extra_vars", action=AddToDict, type=kv_type
)
args = parser.parse_args()
logging.getLogger().setLevel(logging.DEBUG)
asyncio.run(main(playbook_path=args.playbook_path, extra_vars=args.extra_vars))
