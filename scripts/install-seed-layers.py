#!/usr/bin/env python3
"""Install the sanitized seed pack into an AetherMind store.

Reads seeds/tessera-seed-layers.json and writes the
generic bootstrap layers plus textures through the engine CLI on the host that
owns the store, exactly like the merge runner: hash-guarded so a rerun skips
layers whose bodies already exist (idempotent install).

Usage:
    scripts/install-seed-layers.py --out-dir OUT
    scripts/install-seed-layers.py --host HOST --root REMOTE_ROOT --out-dir OUT --run

--run scps payloads to the host and executes the generated runner against the
store root. Without --run it only builds the payload files for inspection.

The pack is a package default, written once into a store that has no history.
It never rewrites a layer the operator or the running companion already holds:
the hash guard skips any body that is already present, so editing a seed and
reinstalling cannot overwrite a live companion's own version of that rule.
"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

SEED_AUTHOR = "seed"
SEED_MARKERS = ["seed"]
TEXTURE_MARKERS = ["seed"]
SKILL_DIR = "skills"


def seed_ctx(value):
    """Seed contexts live under one `seed/` namespace, named once.

    The pack already writes its contexts as `seed/<area>/<rule>`; prefixing
    again produced `seed/seed/<area>/<rule>` and split the namespace in two.
    """
    text = str(value or "generic").strip().strip("/")
    if text.startswith("seed/"):
        return text
    return "seed/" + text


def normalize(pack_path, out_dir):
    pack = json.load(open(pack_path))
    layers = []
    for layer in pack.get("layers") or []:
        body = str(layer.get("body") or "").strip()
        if not body:
            continue
        layers.append({
            "type": str(layer.get("type") or "load-bearing"),
            "body": body,
            "ctx": seed_ctx(layer.get("ctx")),
            "conf": layer.get("conf") if isinstance(layer.get("conf"), (int, float)) else 1.0,
            "author": SEED_AUTHOR,
            "markers": list(SEED_MARKERS),
            "primitive": "layer",
            "kind": "seed-layer",
        })
    textures = []
    for texture in pack.get("textures") or []:
        body = str(texture.get("body") or "").strip()
        if not body:
            continue
        textures.append({
            "ctx": seed_ctx(texture.get("ctx")),
            "body": body,
            "author": SEED_AUTHOR,
            "markers": list(TEXTURE_MARKERS),
        })
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payloads = {"layers": layers, "textures": textures}
    (out / "seed-payloads.json").write_text(json.dumps(payloads, ensure_ascii=False, indent=2))
    runner = RUNNER_TEMPLATE
    (out / "seed-runner.py").write_text(runner)
    skills = install_skills(pack_path, out)
    return len(layers), len(textures), skills


def install_skills(pack_path, out_dir):
    """Stage the pack's skills beside the payloads for the caller to copy.

    The pack names its skills; the shelf itself is the destination. This
    function copies `seeds/skills/<name>/` into the staging directory and
    returns the names it staged, so the runner can report what a fresh install
    would gain. It writes nothing into a live shelf: the operator's own skills
    win, and a skill of the same name is never overwritten here.
    """
    source = Path(pack_path).resolve().parent / SKILL_DIR
    if not source.is_dir():
        return []
    target = Path(out_dir) / SKILL_DIR
    target.mkdir(parents=True, exist_ok=True)
    staged = []
    for child in sorted(source.iterdir()):
        if not child.is_dir() or not (child / "SKILL.md").is_file():
            continue
        destination = target / child.name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(child, destination)
        staged.append(child.name)
    return staged


RUNNER_TEMPLATE = r'''#!/usr/bin/env python3
"""Write seed payloads into a host-local store; skips bodies already present."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def present_hashes(project_root):
    import tomllib
    store_file = Path(project_root).expanduser() / ".aethermind" / "layers.aem"
    if not store_file.is_file():
        return set()
    doc = tomllib.load(store_file.open("rb"))
    return set(hashlib.sha256(str(row.get("body") or "").encode()).hexdigest()
               for row in doc.get("layer") or [])


def present_texture_bodies(project_root):
    """Texture entries are stored as plain text blocks, not TOML rows.

    A rerun must not append every seed texture again. The layer hash guard does
    not look at this file. Read the bodies that are already there so
    a second install skips them.
    """
    store_file = Path(project_root).expanduser() / ".aethermind" / "texture.aem"
    if not store_file.is_file():
        return set()
    bodies = set()
    lines = store_file.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        header = lines[index]
        if header.startswith("20") and "[" in header and header.rstrip().endswith("]"):
            index += 1
            block = []
            while index < len(lines) and not (lines[index].startswith("20") and
                                              "[" in lines[index] and lines[index].rstrip().endswith("]")):
                block.append(lines[index])
                index += 1
            # Entries are separated by a bare '---' line; it is punctuation,
            # not part of the body, so it must not enter the hash.
            while block and block[-1].strip() == "---":
                block.pop()
            body = "\n".join(block).strip()
            if body:
                bodies.add(hashlib.sha256(body.encode()).hexdigest())
            continue
        index += 1
    return bodies


def run_cli(engine_python, engine_dir, project_root, subcommand, payload):
    return subprocess.run(
        [engine_python, "-m", "aethermind.cli", subcommand,
         "--payload", "-", "--project-root", project_root, "--create"],
        input=json.dumps(payload), capture_output=True, text=True,
        cwd=engine_dir, env={"PYTHONPATH": "lib", **os.environ})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payloads", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--skills-dir", default="")
    parser.add_argument("--engine-python", required=True)
    parser.add_argument("--engine-dir", required=True)
    args = parser.parse_args()
    payloads = json.load(open(args.payloads))
    known = present_hashes(args.project_root)
    known_textures = present_texture_bodies(args.project_root)
    written = skipped = failed = 0
    for payload in payloads.get("layers") or []:
        if hashlib.sha256(payload["body"].encode()).hexdigest() in known:
            skipped += 1
            continue
        proc = run_cli(args.engine_python, args.engine_dir, args.project_root, "write-layer", payload)
        if proc.returncode == 0:
            written += 1
        else:
            failed += 1
            print("seed layer failed: " + (proc.stdout or proc.stderr).strip()[:200], file=sys.stderr)
    for payload in payloads.get("textures") or []:
        if hashlib.sha256(payload["body"].encode()).hexdigest() in known_textures:
            skipped += 1
            continue
        proc = run_cli(args.engine_python, args.engine_dir, args.project_root, "write-texture", payload)
        if proc.returncode == 0:
            written += 1
        else:
            failed += 1
            print("seed texture failed: " + (proc.stdout or proc.stderr).strip()[:200], file=sys.stderr)
    # Seed skills are the floor, not the ceiling: an operator skill of the same
    # name wins and is never replaced, so a customized install keeps its version.
    skills_staged = skills_kept = 0
    source = Path(args.payloads).resolve().parent / "skills"
    if args.skills_dir and source.is_dir():
        shelf = Path(args.skills_dir).expanduser()
        shelf.mkdir(parents=True, exist_ok=True)
        for child in sorted(source.iterdir()):
            if not child.is_dir() or not (child / "SKILL.md").is_file():
                continue
            destination = shelf / child.name
            if destination.exists():
                skills_kept += 1
                continue
            shutil.copytree(child, destination)
            skills_staged += 1
    print("seed-install: %d written, %d skipped, %d failed" % (written, skipped, failed))
    print("seed-install: %d skills staged, %d kept from the shelf" % (skills_staged, skills_kept))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
'''


def main():
    parser = argparse.ArgumentParser(description="install the seed pack")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--host", default="",
                        help="store host for --run; required when --run is set")
    parser.add_argument("--root", default="")
    parser.add_argument("--skills-dir", default="",
                        help="shelf directory for seed skills; empty means <root>/skills")
    parser.add_argument("--run", action="store_true", help="scp and run on the store host")
    parser.add_argument("--engine-python", default="")
    parser.add_argument("--engine-dir", default="")
    args = parser.parse_args()

    pack = Path(__file__).resolve().parent.parent / "seeds" / "tessera-seed-layers.json"
    if not pack.is_file():
        print("install-seed-layers: seed pack missing at " + str(pack), file=sys.stderr)
        return 2
    layers, textures, skills = normalize(pack, args.out_dir)
    print(f"install-seed-layers: {layers} layers, {textures} textures and {len(skills)} skills "
          f"normalized in {args.out_dir}")
    if skills:
        print("install-seed-layers: skills staged: " + ", ".join(skills))

    if args.run:
        if not args.host:
            print("install-seed-layers: --run needs --host (no default host is set)", file=sys.stderr)
            return 2
        if not args.root:
            print("install-seed-layers: --run needs --root (the store root on --host)", file=sys.stderr)
            return 2
        if not args.engine_python or not args.engine_dir:
            print("install-seed-layers: --run needs --engine-python and --engine-dir (no host paths are hardcoded)", file=sys.stderr)
            return 2
        import subprocess
        subprocess.run(["ssh", args.host, "mkdir -p /tmp/seed-install"], capture_output=True, text=True)
        transfer = [f"{args.out_dir}/seed-payloads.json", f"{args.out_dir}/seed-runner.py"]
        if skills:
            transfer.append(f"{args.out_dir}/{SKILL_DIR}")
        res = subprocess.run(
            ["scp", "-r", *transfer, f"{args.host}:/tmp/seed-install/"],
            capture_output=True, text=True)
        if res.returncode != 0:
            print("install-seed-layers: scp failed: " + res.stderr.strip()[:300], file=sys.stderr)
            return 2
        skills_dir = args.skills_dir or (args.root.rstrip("/") + "/" + SKILL_DIR)
        subprocess.run(
            ["ssh", args.host,
             "mkdir -p /tmp/seed-install && cd /tmp/seed-install && "
             "nohup python3 seed-runner.py --payloads /tmp/seed-install/seed-payloads.json "
             f"--project-root {args.root} "
             f"--skills-dir {skills_dir} "
             f"--engine-python {args.engine_python} "
             f"--engine-dir {args.engine_dir} "
             "> /tmp/seed-install/seed-install.log 2>&1 < /dev/null & "])
        print("install-seed-layers: launched the runner on the store host; log at /tmp/seed-install/seed-install.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())