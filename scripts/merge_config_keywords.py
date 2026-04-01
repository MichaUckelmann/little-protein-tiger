#!/usr/bin/env python3
"""
Merge complex keywords into a copy of config.yaml for the overnight fetch run.

Usage:
    python scripts/merge_config_keywords.py
    python scripts/merge_config_keywords.py --keywords info/complex_keywords.yaml
    python scripts/merge_config_keywords.py --output config_with_complexes.yaml
"""
import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

parser = argparse.ArgumentParser()
parser.add_argument("--config", default="config.yaml")
parser.add_argument("--keywords", default="info/complex_keywords.yaml")
parser.add_argument("--output", default="config_with_complexes.yaml")
args = parser.parse_args()

with open(args.config) as f:
    cfg = yaml.safe_load(f)

with open(args.keywords) as f:
    extra = yaml.safe_load(f)["keywords"]

existing = set(cfg["keywords"])
new_kws = [k for k in extra if k not in existing]
cfg["keywords"] = cfg["keywords"] + new_kws

with open(args.output, "w") as f:
    yaml.dump(cfg, f, allow_unicode=True, width=200)

print(f"Base keywords    : {len(existing)}")
print(f"Complex keywords : {len(extra)} ({len(new_kws)} new after dedup)")
print(f"Total in output  : {len(cfg['keywords'])}")
print(f"Written to       : {args.output}")
