#!/usr/bin/env python3
"""Check an independent review receipt against a trusted checkout."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


SHA = re.compile(r"[a-f0-9]{40}")
REQUIRED = {"head", "base", "tested_commit", "tested_tree", "policy_sha256",
            "decision", "evidence"}


def git(*args):
    return subprocess.run(["git", "--no-replace-objects", *args], check=True, capture_output=True, timeout=10).stdout


def duplicate_free_pairs(items):
    result = dict(items)
    if len(items) != len(result):
        raise ValueError("duplicate receipt key")
    return result


def read_receipt(path):
    with Path(path).open("rb") as handle:
        data = handle.read(65537)
    if len(data) > 65536:
        raise ValueError("receipt exceeds 64 KiB")
    return json.loads(data, object_pairs_hook=duplicate_free_pairs)


def validate(receipt, *, head, base, tested):
    for value in (head, base, tested):
        if not SHA.fullmatch(value):
            raise ValueError("expected full lower-case Git commit IDs")
    if type(receipt) is not dict or set(receipt) != REQUIRED:
        raise ValueError("invalid receipt fields")
    if [receipt[k] for k in ("head", "base", "tested_commit")] != [head, base, tested]:
        raise ValueError("receipt is not for the current head/base/combined commit")
    parents = git("show", "-s", "--format=%P", tested).decode("ascii").split()
    if parents != [base, head]:
        raise ValueError("tested commit is not the requested merge of base and head")
    tree = git("rev-parse", tested + "^{tree}").decode("ascii").strip()
    if tree != receipt["tested_tree"]:
        raise ValueError("tested tree differs")
    policy = hashlib.sha256(git("show", base + ":REVIEW.md")).hexdigest()
    if receipt["policy_sha256"] != policy:
        raise ValueError("review rules differ from target base")
    if receipt["decision"] not in ("pass", "block"):
        raise ValueError("invalid review decision")
    proof = receipt["evidence"]
    if (type(proof) is not list or not 1 <= len(proof) <= 20
            or any(type(x) is not str or not x.strip() or len(x) > 512
                   or any(ord(char) < 32 or ord(char) == 127 for char in x) for x in proof)):
        raise ValueError("missing or invalid evidence references")
    return receipt["decision"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("off", "observe", "enforce"), default="off")
    parser.add_argument("--receipt")
    parser.add_argument("--head")
    parser.add_argument("--base")
    parser.add_argument("--tested")
    args = parser.parse_args(argv)
    state = "off"
    if args.mode != "off":
        try:
            if not all((args.receipt, args.head, args.base, args.tested)):
                raise ValueError("receipt and expected revisions are required")
            state = validate(read_receipt(args.receipt), head=args.head, base=args.base,
                             tested=args.tested)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            state = "invalid"
            print(json.dumps({"mode": args.mode, "result": state, "reason": str(error)}))
            return 1 if args.mode == "enforce" else 0
    print(json.dumps({"mode": args.mode, "result": state}))
    return 1 if args.mode == "enforce" and state != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
