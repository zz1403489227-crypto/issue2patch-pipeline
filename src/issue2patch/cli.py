"""Command line: i2p <command>."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import time
from pathlib import Path

from .config import Settings


def _settings(args) -> Settings:
    s = Settings()
    if getattr(args, "keep", False):
        s.keep_containers = True
    return s


def cmd_export(args):
    from .tasks import export_verified

    s = Settings()
    print(export_verified(Path(s.data_file)), "instances ->", s.data_file)


def cmd_pick(args):
    """Deterministic small sample from instances with a local image, spread over repos."""
    from .tasks import image_for, load_instances, local_images

    s = Settings()
    inst = load_instances(s.data_file)
    imgs = local_images()
    pool = [i for i in inst.values() if image_for(i["instance_id"]) in imgs
            and (not args.difficulty or i.get("difficulty") == args.difficulty)]
    rng = random.Random(args.seed)
    rng.shuffle(pool)
    picked, repos = [], set()
    for i in pool:
        if i["repo"] in repos and len(repos) < len({p["repo"] for p in pool}):
            continue
        picked.append(i["instance_id"])
        repos.add(i["repo"])
        if len(picked) == args.n:
            break
    print(f"{len(pool)} candidates with local images; picked:")
    print(",".join(picked))


def cmd_smoke(args):
    """Start one sandbox and check that Pi reaches the model, runs tools and submits."""
    from .gateway import ModelGateway
    from .harness import AgentRequest, make_harness
    from .nodes.prepare import container_name
    from .sandbox import GIT_PREPARE, DockerSandbox
    from .tasks import image_for

    s = _settings(args)
    run_dir = Path(s.runs_dir) / "smoke" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    sock = Path(s.sock_root) / hashlib.sha1(str(run_dir).encode()).hexdigest()[:10]
    gw = ModelGateway(sock / "gw.sock", s.vllm_url, run_dir / "model_calls.jsonl", args.task)
    gw.start()
    gw.current_node = "smoke"
    sb = DockerSandbox.create(image_for(args.task), container_name("smoke", args.task),
                              [(s.node_home, "/opt/node", "ro"), (s.pi_dir, "/opt/pi", "ro"),
                               (s.tools_dir, "/opt/i2p-tools", "ro"), (str(sock), "/i2p", "rw")],
                              s.container_cpus, s.container_memory)
    try:
        print("git prepare:", sb.exec(GIT_PREPARE, conda=False, timeout=900).output.strip().splitlines()[-4:])
        net = sb.exec("python -c \"import urllib.request;urllib.request.urlopen('http://pypi.org',timeout=5)\"",
                      timeout=30)
        print("network blocked:", net.code != 0)
        res = make_harness(s).run(AgentRequest(
            node="smoke", system_prompt="You are a careful assistant working in /testbed. Use the tools.",
            user_prompt=(("With bash run: printf 'a = 1\\nb = 2\\nc = 3\\n' > /tmp/i2p_smoke.py . Then use the edit "
                          "tool to change `b = 2` into `b = 20` and, in a second edit, `c = 3` into `c = 30`. "
                          "Then cat the file and call submit with its content.") if args.edit else
                         ("Run `python --version` and `git log --oneline -1` with bash, read the first 5 lines of "
                          "the repository README (any README* file) with the read tool, then call submit with the "
                          "python version and the commit subject.")),
            container=sb.container, workdir="/testbed", trace_dir=run_dir / "agent",
            tools=["read", "bash", "edit"] if args.edit else ["read", "bash"],
            submit={"description": "Submit findings.", "fields": (
                {"content": {"type": "string", "description": "Final content of the file"}} if args.edit else {
                "python_version": {"type": "string", "description": "Output of python --version"},
                "commit_subject": {"type": "string", "description": "Subject of HEAD commit"}})},
            max_requests=s.agent_limits["smoke"]["max_requests"], timeout_s=s.agent_limits["smoke"]["timeout_s"]))
        print(json.dumps(res.to_dict(), indent=2)[:3000])
        print("gateway usage:", gw.node_usage("smoke"))
    finally:
        gw.stop()
        if not s.keep_containers:
            sb.destroy()


def cmd_run(args):
    from .pipeline import run_many
    from .tasks import load_instances

    s = _settings(args)
    inst = load_instances(s.data_file)
    ids = [t for t in args.tasks.split(",") if t]
    run_id = args.run_id or time.strftime("r%Y%m%d-%H%M%S")
    print("run", run_id, "->", Path(s.runs_dir) / run_id, flush=True)
    run_many([inst[i] for i in ids], run_id, s, parallel=args.parallel)


def cmd_resume(args):
    """Continue every unfinished task of a run (finished tasks are skipped by run_task)."""
    from .pipeline import run_many
    from .tasks import load_instances

    s = _settings(args)
    inst = load_instances(s.data_file)
    meta = json.loads((Path(s.runs_dir) / args.run_id / "run.json").read_text())
    ids = list(dict.fromkeys(t for inv in meta.get("invocations", []) for t in inv["tasks"]))
    print("resume", args.run_id, len(ids), "tasks", flush=True)
    run_many([inst[i] for i in ids], args.run_id, s, parallel=args.parallel)


def cmd_rerun_node(args):
    """Single-node re-test: re-run one node of every task of a run from its stored inputs."""
    from .pipeline import run_many, seed_from
    from .tasks import load_instances

    s = _settings(args)
    inst = load_instances(s.data_file)
    src = Path(s.runs_dir) / args.from_run
    ids = sorted(d.name for d in (src / "tasks").iterdir() if d.is_dir())
    if args.tasks:
        ids = [i for i in ids if i in set(args.tasks.split(","))]
    dst = Path(s.runs_dir) / args.run_id
    for i in ids:
        seed_from(src, dst, i, args.node)
    run_many([inst[i] for i in ids], args.run_id, s, parallel=args.parallel, stop_after=args.until or args.node)


def cmd_grade(args):
    from .grade import grade

    s = Settings()
    print(json.dumps(grade(Path(s.runs_dir) / args.run_id, s.data_file, args.workers), indent=2))


def cmd_accept(args):
    from .grade import accept
    from .tasks import image_for, load_instances, local_images

    s = Settings()
    inst = load_instances(s.data_file)
    imgs = local_images()
    ids = sorted(i for i in inst if image_for(i) in imgs)
    if args.ids_file:
        wanted = set(json.loads(Path(args.ids_file).read_text())["candidates"])
        ids = [i for i in ids if i in wanted]
    if args.limit:
        ids = ids[: args.limit]
    out = Path(s.runs_dir).parent / "var" / "acceptance"
    v = accept(inst, ids, out, s.data_file, args.workers, args.part)
    if v:
        print(f"{sum(x['accepted'] for x in v.values())}/{len(v)} accepted -> {out / 'acceptance.json'}")


def cmd_dev_plan(args):
    from .tasks import dev_plan, image_for, load_instances, local_images

    s = Settings()
    inst = load_instances(s.data_file)
    imgs = local_images()
    plan = dev_plan(inst, sorted(i for i in inst if image_for(i) in imgs), args.n_dev, args.seed)
    out = Path(s.runs_dir).parent / "data" / "dev-plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2))
    print(len(plan["candidates"]), "candidates ->", out)


def cmd_pick_dev(args):
    from .tasks import load_instances, pick_dev

    s = Settings()
    root = Path(s.runs_dir).parent
    plan = json.loads((root / "data" / "dev-plan.json").read_text())
    acc = json.loads((root / "var" / "acceptance" / "acceptance.json").read_text())
    sp = pick_dev(plan, acc, load_instances(s.data_file), args.n_canary, args.seed)
    (root / "data" / "splits.json").write_text(json.dumps(sp, indent=2))
    print(len(sp["dev"]), "dev,", len(sp["canary"]), "canary, shortfall:", sp["shortfall"])


def cmd_split(args):
    from .tasks import load_instances, make_splits

    s = Settings()
    inst = load_instances(s.data_file)
    acc = json.loads((Path(s.runs_dir).parent / "var" / "acceptance" / "acceptance.json").read_text())
    sp = make_splits(inst, acc, args.n_dev, args.n_canary, args.seed)
    out = Path(s.runs_dir).parent / "data" / "splits.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sp, indent=2))
    print({k: len(v) for k, v in sp.items() if isinstance(v, list)}, "->", out)


def cmd_evaluate(args):
    """Hidden-test grading (if missing) + node-level truth metrics + failure attribution."""
    from .evaluate import evaluate
    from .grade import grade
    from .tasks import load_instances

    s = Settings()
    run_dir = Path(s.runs_dir) / args.run_id
    seeded = {json.loads((d / "state.json").read_text()).get("meta", {}).get("seeded_from")
              for d in (run_dir / "tasks").iterdir() if (d / "state.json").exists()} - {None}
    if not (run_dir / "grading" / "resolved.json").exists():
        if seeded:  # single-node re-test: the patch is the source run's, so is its hidden-test result
            (run_dir / "grading").mkdir(exist_ok=True)
            src = Path(s.runs_dir) / seeded.pop() / "grading" / "resolved.json"
            (run_dir / "grading" / "resolved.json").write_text(src.read_text())
        else:
            grade(run_dir, s.data_file, args.workers)
    res = evaluate(run_dir, load_instances(s.data_file), s, args.workers)
    print(json.dumps(res["summary"], indent=2))


def cmd_export(args):
    """Send a run to the self-hosted Langfuse (keys from var/langfuse.env)."""
    from .observe import export_run

    s = Settings()
    env = dict(line.split("=", 1) for line in (Path(s.runs_dir).parent / "var" / "langfuse.env").read_text()
               .splitlines() if "=" in line and not line.startswith("#"))
    n = export_run(Path(s.runs_dir) / args.run_id, args.host, env["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"],
                   env["LANGFUSE_INIT_PROJECT_SECRET_KEY"])
    print(f"exported {n} events")


def cmd_funnel(args):
    from .funnel import summarize, to_markdown

    s = Settings()
    ids = args.run_ids.split(",")
    summ = summarize(Path(s.runs_dir), ids)
    md = to_markdown(summ)
    out = Path(s.runs_dir) / f"funnel-{'+'.join(ids)}"
    out.with_suffix(".json").write_text(json.dumps(summ, indent=2))
    out.with_suffix(".md").write_text(md)
    print(md)


def cmd_service_setup(args):
    from . import service

    service.setup()
    print("schema and queues ready")


def cmd_worker(args):
    import logging

    from . import service

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    service.Worker(args.name).serve()


def cmd_api(args):
    import uvicorn

    uvicorn.run("issue2patch.api:app", host="127.0.0.1", port=args.port, log_level="info")


def fake_forge_repo(instance_id: str) -> dict:
    """A bare git repo holding the SWE-bench image's checkout, served by the local fake GitHub."""
    import shutil
    import subprocess

    from .config import ROOT
    from .sandbox import DockerSandbox
    from .tasks import image_for

    name = instance_id.split("__")[-1]
    bare = ROOT / "var" / "fake-github" / f"{name}.git"
    if not bare.exists():
        s = Settings()
        sb = DockerSandbox.create(image_for(instance_id), f"i2p-forge-{name}", [], s.container_cpus, s.container_memory)
        try:
            sb.exec("git branch -f i2p-base HEAD && git bundle create /tmp/repo.bundle i2p-base", conda=False, timeout=600)
            tmp = ROOT / "var" / "fake-github" / f"{name}.bundle"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["docker", "cp", f"{sb.container}:/tmp/repo.bundle", str(tmp)], check=True)
            subprocess.run(["git", "clone", "-q", "--bare", str(tmp), str(bare)], check=True)
            tmp.unlink()
        finally:
            sb.destroy()
    return {"api": "http://127.0.0.1:13850", "repo": f"local/{name}", "git_url": str(bare), "base": "i2p-base"}


def cmd_submit(args):
    from . import service

    for iid in args.instances.split(","):
        spec = {"kind": "swebench", "instance_id": iid, "name": iid.split("__")[-1], "title": iid}
        if args.fake_forge:
            spec["exit"] = fake_forge_repo(iid)
        print(json.dumps(service.submit(spec, args.user, args.priority)))


def cmd_report(args):
    from .report import build
    from .tasks import load_instances

    s = Settings()
    run_dir = Path(s.runs_dir) / args.run_id
    md = build(run_dir, load_instances(s.data_file))
    (run_dir / "report.md").write_text(md)
    print(md)


def main():
    p = argparse.ArgumentParser(prog="i2p")
    sub = p.add_subparsers(required=True)
    sub.add_parser("export-data").set_defaults(fn=cmd_export)
    a = sub.add_parser("pick")
    a.add_argument("--n", type=int, default=5)
    a.add_argument("--seed", type=int, default=0)
    a.add_argument("--difficulty", default="<15 min fix")
    a.set_defaults(fn=cmd_pick)
    a = sub.add_parser("smoke")
    a.add_argument("--task", required=True)
    a.add_argument("--edit", action="store_true", help="exercise the edit tool")
    a.add_argument("--keep", action="store_true")
    a.set_defaults(fn=cmd_smoke)
    a = sub.add_parser("run")
    a.add_argument("--tasks", required=True, help="comma-separated instance ids")
    a.add_argument("--run-id")
    a.add_argument("--parallel", type=int, default=1)
    a.add_argument("--keep", action="store_true")
    a.set_defaults(fn=cmd_run)
    a = sub.add_parser("resume")
    a.add_argument("--run-id", required=True)
    a.add_argument("--parallel", type=int, default=1)
    a.add_argument("--keep", action="store_true")
    a.set_defaults(fn=cmd_resume)
    a = sub.add_parser("rerun-node", help="re-run one node from a previous run's stored inputs")
    a.add_argument("--from-run", required=True)
    a.add_argument("--node", required=True)
    a.add_argument("--until", default=None, help="keep going up to this node (e.g. validate) to measure it")
    a.add_argument("--run-id", required=True)
    a.add_argument("--tasks", default="")
    a.add_argument("--parallel", type=int, default=4)
    a.add_argument("--keep", action="store_true")
    a.set_defaults(fn=cmd_rerun_node)
    a = sub.add_parser("grade")
    a.add_argument("--run-id", required=True)
    a.add_argument("--workers", type=int, default=4)
    a.set_defaults(fn=cmd_grade)
    a = sub.add_parser("accept", help="reference patch must pass, no-op must fail (all local images)")
    a.add_argument("--workers", type=int, default=8)
    a.add_argument("--limit", type=int, default=0)
    a.add_argument("--part", choices=["all", "gold", "noop", "collect"], default="all")
    a.add_argument("--ids-file", default="", help="JSON with a 'candidates' list (e.g. data/dev-plan.json)")
    a.set_defaults(fn=cmd_accept)
    a = sub.add_parser("dev-plan", help="stratified sampling plan + acceptance candidates for the dev set")
    a.add_argument("--n-dev", type=int, default=100)
    a.add_argument("--seed", type=int, default=0)
    a.set_defaults(fn=cmd_dev_plan)
    a = sub.add_parser("pick-dev", help="dev and canary from the plan and acceptance results")
    a.add_argument("--n-canary", type=int, default=20)
    a.add_argument("--seed", type=int, default=0)
    a.set_defaults(fn=cmd_pick_dev)
    a = sub.add_parser("split", help="dev / canary / held-out from accepted tasks")
    a.add_argument("--n-dev", type=int, default=100)
    a.add_argument("--n-canary", type=int, default=20)
    a.add_argument("--seed", type=int, default=0)
    a.set_defaults(fn=cmd_split)
    a = sub.add_parser("evaluate", help="grade + node metrics + failure attribution")
    a.add_argument("--run-id", required=True)
    a.add_argument("--workers", type=int, default=6)
    a.set_defaults(fn=cmd_evaluate)
    a = sub.add_parser("export", help="send a run to Langfuse")
    a.add_argument("--run-id", required=True)
    a.add_argument("--host", default="http://127.0.0.1:13300")
    a.set_defaults(fn=cmd_export)
    a = sub.add_parser("funnel", help="node metrics with intervals across runs")
    a.add_argument("--run-ids", required=True, help="comma-separated, e.g. base-r1,base-r2,base-r3")
    a.set_defaults(fn=cmd_funnel)
    a = sub.add_parser("service-setup", help="create the Postgres schema and the RabbitMQ queues")
    a.set_defaults(fn=cmd_service_setup)
    a = sub.add_parser("worker", help="consume tasks from RabbitMQ (one task at a time)")
    a.add_argument("--name", default=None)
    a.set_defaults(fn=cmd_worker)
    a = sub.add_parser("api", help="HTTP entry point on 127.0.0.1")
    a.add_argument("--port", type=int, default=13800)
    a.set_defaults(fn=cmd_api)
    a = sub.add_parser("submit", help="queue SWE-bench tasks through the service (testing)")
    a.add_argument("--instances", required=True)
    a.add_argument("--user", default="default")
    a.add_argument("--priority", type=int, default=0)
    a.add_argument("--fake-forge", action="store_true", help="open the PR on the local fake GitHub")
    a.set_defaults(fn=cmd_submit)
    a = sub.add_parser("fake-github", help="local stand-in for GitHub's PR endpoints (tests)")
    a.add_argument("--port", type=int, default=13850)
    a.set_defaults(fn=lambda a: __import__("uvicorn").run("issue2patch.fake_github:app", host="127.0.0.1",
                                                          port=a.port, log_level="warning"))
    a = sub.add_parser("report")
    a.add_argument("--run-id", required=True)
    a.set_defaults(fn=cmd_report)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
