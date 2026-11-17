#!/usr/bin/env python3
"""Hand-build the lgda-music OCI image and push it to Fly's registry.

Why this exists: Fly's remote builders (Depot and the classic builder) cannot
be reached from this VM — direct egress is blocked and the egress proxy breaks
their tunnels/gRPC. Docker Hub's registry also hangs here. But mirror.gcr.io
(Google's Docker Hub mirror), PyPI and registry.fly.io all answer fast, so
we assemble the image ourselves with plain HTTPS:

  1. Pull python:3.12-slim (linux/amd64) layers + config from mirror.gcr.io.
  2. Build the app layer: pip install -r requirements.txt into the image's
     site-packages path, plus app/, catalog.json, sessions.json, audio/.
  3. Write a new image config (Cmd: python -m uvicorn on port 8000, matching
     fly.toml's internal_port) and a schema-2 manifest.
  4. Push every blob + the manifest to registry.fly.io/lgda-music:latest
     (Basic auth: user x, password FLY_API_TOKEN from the environment).

Then deploy with: flyctl deploy --image registry.fly.io/lgda-music:latest

Usage: FLY_API_TOKEN=... python3 scripts/build_image.py [--pull-only|--push-only]
"""
import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OCI_DIR = ROOT / ".oci"
CACHE = OCI_DIR / "cache"
STAGING = OCI_DIR / "staging"
APP_LAYER = OCI_DIR / "app-layer.tar.gz"

MIRROR = "https://mirror.gcr.io"
BASE_REPO = "library/python"
BASE_TAG = "3.12-slim"
FLY_REGISTRY = "https://registry.fly.io"
FLY_REPO = "lgda-music"
FLY_TAG = "latest"

CHUNK = 8 * 1024 * 1024  # upload chunk size

MT_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MT_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MT_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
MT_CONFIG = "application/vnd.docker.container.image.v1+json"
MT_LAYER = "application/vnd.docker.image.rootfs.diff.tar.gzip"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


# ---------------------------------------------------------------- pull base
def pull_base():
    CACHE.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    # 1. manifest list -> amd64 manifest digest
    r = s.get(f"{MIRROR}/v2/{BASE_REPO}/manifests/{BASE_TAG}",
              headers={"Accept": f"{MT_LIST}, {MT_OCI_INDEX}"}, timeout=60)
    r.raise_for_status()
    idx = r.json()
    amd64 = [m for m in idx["manifests"]
             if m.get("platform", {}).get("architecture") == "amd64"
             and m.get("platform", {}).get("os") == "linux"]
    if not amd64:
        raise SystemExit("no linux/amd64 manifest in index")
    digest = amd64[0]["digest"]
    log(f"base amd64 manifest: {digest[:19]}")
    # 2. manifest -> config + layers (mirror serves OCI manifests; GCR 404s
    #    when the Accept header doesn't include the stored media type)
    r = s.get(f"{MIRROR}/v2/{BASE_REPO}/manifests/{digest}",
              headers={"Accept": f"{MT_MANIFEST}, "
                                 "application/vnd.oci.image.manifest.v1+json"},
              timeout=60)
    r.raise_for_status()
    manifest = r.json()
    blobs = [manifest["config"]] + manifest["layers"]
    total = sum(b["size"] for b in blobs)
    log(f"downloading {len(blobs)} blobs, {total/1e6:.1f} MB")
    paths = {}
    for b in blobs:
        d = b["digest"]
        p = CACHE / d.replace(":", "_")
        if p.exists() and p.stat().st_size == b["size"]:
            log(f"  cached {d[:19]}")
        else:
            with s.get(f"{MIRROR}/v2/{BASE_REPO}/blobs/{d}", stream=True,
                       timeout=300) as resp:
                resp.raise_for_status()
                with open(p, "wb") as f:
                    for chunk in resp.iter_content(1 << 20):
                        f.write(chunk)
            log(f"  got {d[:19]} {b['size']/1e6:.1f} MB")
        if sha256_file(p) != d:
            raise SystemExit(f"checksum mismatch on {d}")
        paths[d] = p
    (CACHE / "base-manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest, paths


# ---------------------------------------------------------------- app layer
def build_app_layer():
    if STAGING.exists():
        shutil.rmtree(STAGING)
    site_pkgs = STAGING / "usr/local/lib/python3.12/site-packages"
    site_pkgs.mkdir(parents=True)
    log("pip install --target (deps for the image)")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir",
         "--disable-pip-version-check", "--target", str(site_pkgs),
         "-r", str(ROOT / "requirements.txt")],
        capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        raise SystemExit("pip install failed")
    log("copying app + catalog + audio into the layer")
    app_dir = STAGING / "app"
    shutil.copytree(ROOT / "app", app_dir / "app")
    shutil.copy(ROOT / "catalog.json", app_dir / "catalog.json")
    shutil.copy(ROOT / "sessions.json", app_dir / "sessions.json")
    shutil.copytree(ROOT / "audio", app_dir / "audio")
    (app_dir / "data").mkdir(exist_ok=True)
    # tar it up, root-owned, gzip level 1 (audio is already compressed)
    log("tarring app layer")
    raw = OCI_DIR / "app-layer.tar"
    with tarfile.open(raw, "w") as tf:
        for path in sorted(STAGING.rglob("*")):
            arc = path.relative_to(STAGING)
            ti = tf.gettarinfo(str(path), arcname=str(arc))
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tf.addfile(ti, open(path, "rb") if path.is_file() else None)
    diff_id = sha256_file(raw)
    with open(raw, "rb") as fin, gzip.GzipFile(
            filename="", mode="wb", fileobj=open(APP_LAYER, "wb"),
            compresslevel=1, mtime=0) as gz:
        shutil.copyfileobj(fin, gz, 1 << 20)
    raw.unlink()
    blob_digest = sha256_file(APP_LAYER)
    log(f"app layer: {APP_LAYER.stat().st_size/1e6:.1f} MB, {blob_digest[:19]}")
    return blob_digest, diff_id, APP_LAYER.stat().st_size


# ---------------------------------------------------------------- config
def build_config(manifest, paths, layer_digest, diff_id, layer_size):
    base_config = json.loads(paths[manifest["config"]["digest"]].read_text())
    cfg = json.loads(json.dumps(base_config))  # deep copy
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cfg["created"] = now
    cfg["architecture"] = "amd64"
    cfg["os"] = "linux"
    c = cfg.setdefault("config", {})
    c["WorkingDir"] = "/app"
    c["Cmd"] = ["sh", "-c",
                "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
    c.setdefault("ExposedPorts", {})["8000/tcp"] = {}
    c.pop("Entrypoint", None)
    cfg.setdefault("rootfs", {}).setdefault("diff_ids", []).append(diff_id)
    cfg.setdefault("history", []).append({
        "created": now, "created_by": "lgda hand-built app layer",
        "empty_layer": False})
    blob = json.dumps(cfg, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    p = CACHE / digest.replace(":", "_")
    p.write_bytes(blob)
    log(f"image config: {digest[:19]}")
    # manifest (normalize base layer media types to Docker types; the blob
    # bytes are identical gzip tars either way)
    layers = [{"mediaType": MT_LAYER, "size": b["size"], "digest": b["digest"]}
              for b in manifest["layers"]]
    layers.append({
        "mediaType": MT_LAYER, "size": layer_size, "digest": layer_digest})
    new_manifest = {
        "schemaVersion": 2,
        "mediaType": MT_MANIFEST,
        "config": {"mediaType": MT_CONFIG, "size": len(blob), "digest": digest},
        "layers": layers,
    }
    (OCI_DIR / "manifest.json").write_text(json.dumps(new_manifest, indent=1))
    blobs = [(b["digest"], paths[b["digest"]]) for b in manifest["layers"]]
    blobs.append((layer_digest, APP_LAYER))
    blobs.append((digest, p))
    return new_manifest, blobs


# ---------------------------------------------------------------- push
def push_blobs(blobs):
    token = os.environ.get("FLY_API_TOKEN")
    if not token:
        raise SystemExit("FLY_API_TOKEN is not set")
    s = requests.Session()
    s.auth = ("x", token)
    base = f"{FLY_REGISTRY}/v2/{FLY_REPO}"
    for digest, path in blobs:
        r = s.head(f"{base}/blobs/{digest}", timeout=60)
        if r.status_code == 200:
            log(f"  already in registry {digest[:19]}")
            continue
        log(f"  uploading {digest[:19]} ({path.stat().st_size/1e6:.1f} MB)")
        r = s.post(f"{base}/blobs/uploads/", timeout=60)
        r.raise_for_status()
        loc = r.headers["Location"]
        if loc.startswith("/"):
            loc = FLY_REGISTRY + loc
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                r = s.patch(loc, data=chunk,
                            headers={"Content-Type": "application/octet-stream"},
                            timeout=300)
                r.raise_for_status()
                loc = r.headers["Location"]
                if loc.startswith("/"):
                    loc = FLY_REGISTRY + loc
        sep = "&" if "?" in loc else "?"
        r = s.put(f"{loc}{sep}digest={digest}", timeout=120)
        r.raise_for_status()
        log(f"  pushed {digest[:19]}")


def push_manifest(manifest):
    token = os.environ.get("FLY_API_TOKEN")
    s = requests.Session()
    s.auth = ("x", token)
    r = s.put(f"{FLY_REGISTRY}/v2/{FLY_REPO}/manifests/{FLY_TAG}",
              data=json.dumps(manifest),
              headers={"Content-Type": MT_MANIFEST}, timeout=120)
    r.raise_for_status()
    log(f"manifest pushed: {FLY_REGISTRY}/{FLY_REPO}:{FLY_TAG}")
    log(f"digest: {r.headers.get('Docker-Content-Digest')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull-only", action="store_true")
    ap.add_argument("--push-only", action="store_true")
    args = ap.parse_args()

    manifest, paths = pull_base()
    layer_digest, diff_id, layer_size = build_app_layer()
    new_manifest, blobs = build_config(manifest, paths, layer_digest,
                                       diff_id, layer_size)
    if args.pull_only:
        log("pull-only: image assembled locally, not pushed")
        return
    push_blobs(blobs)
    push_manifest(new_manifest)
    log("DONE — deploy with: "
        "flyctl deploy --image registry.fly.io/lgda-music:latest")


if __name__ == "__main__":
    main()
