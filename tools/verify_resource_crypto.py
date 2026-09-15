"""Offline real-resource verification; writes only to a NEW output directory."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.resource_crypto import load_candidates, decrypt_staged, xor_repeat, validate_bundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    candidates = load_candidates(args.game, args.output)
    rows = []
    for source in sorted((args.game / "assets/aa/Android").glob("*.bundle")):
        target = args.output / "decrypted" / source.name
        target.parent.mkdir(exist_ok=True)
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        shutil.copy2(source, target)
        profile = decrypt_staged(target, candidates)
        if profile is None:
            raise RuntimeError(f"Not recognized: {source}")
        assert profile["encrypted_sha256"] == original_hash
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
        rows.append({"file": source.name, "objects": profile["object_count"], "roundtrip": True,
                     "source_unchanged": True})
        print(f"[{len(rows)}] {source.name}: {profile['object_count']} objects", flush=True)
    (args.output / "verification.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Validated {len(rows)} bundles, {sum(row['objects'] for row in rows)} objects")


if __name__ == "__main__":
    main()
