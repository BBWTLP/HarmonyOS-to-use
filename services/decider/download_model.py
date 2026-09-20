"""Download and verify one immutable HF snapshot; never uses runtime credentials."""
import concurrent.futures, hashlib, json, os, urllib.request, time, subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parent
REV = "7789eb65d5cf519737608e218fa88819bddea0af"
REPO = "Mapika/decider-2b"
DEST = ROOT / "models" / REV

def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as r: return json.load(r)

def download(entry):
    name = entry["path"]
    if entry["type"] != "file" or name == ".gitattributes": return None
    target = DEST / name
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = entry.get("lfs", {}).get("oid")
    def digest(p):
        h = hashlib.sha256()
        with p.open("rb") as f:
            for block in iter(lambda: f.read(8*1024*1024), b""): h.update(block)
        return h.hexdigest()
    if target.exists() and target.stat().st_size == entry["size"] and (not expected or digest(target) == expected):
        return {"path":name,"size":target.stat().st_size,"sha256":digest(target)}
    tmp = target.with_name(target.name + ".part")
    url = f"https://huggingface.co/{REPO}/resolve/{REV}/{name}?download=true"
    print("Downloading", name, entry["size"], flush=True)
    if entry["size"] > 100*1024*1024:
        chunks = ROOT / ".tmp" / (name.replace("/", "_") + ".chunks")
        chunks.mkdir(parents=True, exist_ok=True)
        step = 32*1024*1024
        spans = [(i, start, min(start+step, entry["size"])-1) for i,start in enumerate(range(0,entry["size"],step))]
        def part(span):
            i,start,end = span
            p = chunks / str(i)
            if p.exists() and p.stat().st_size == end-start+1: return p
            for attempt in range(4):
                proc = subprocess.run(["curl.exe","-sS","--fail","-L","--max-time","180","--connect-timeout","30","--range",f"{start}-{end}","-o",str(p),url],capture_output=True)
                if proc.returncode == 0 and p.stat().st_size == end-start+1:
                    print(f"Verified size: chunk {i+1}/{len(spans)}",flush=True)
                    return p
                time.sleep(1+attempt)
            raise RuntimeError(f"Chunk failed after retry: {i}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            parts = list(pool.map(part,spans))
        with tmp.open("wb") as f:
            for p in parts:
                with p.open("rb") as src:
                    while block := src.read(1024*1024): f.write(block)
    else:
        with urllib.request.urlopen(url, timeout=60) as r, tmp.open("wb") as f:
            while block := r.read(65536): f.write(block)
    actual = digest(tmp)
    if tmp.stat().st_size != entry["size"] or (expected and actual != expected):
        raise RuntimeError(f"Snapshot integrity failed: {name}")
    os.replace(tmp,target)
    print("Verified",name,flush=True)
    return {"path":name,"size":target.stat().st_size,"sha256":actual}

if __name__ == "__main__":
    entries = get_json(f"https://huggingface.co/api/models/{REPO}/tree/{REV}?recursive=true")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        files = [x for x in pool.map(download, entries) if x]
    (ROOT/"snapshot-manifest.json").write_text(json.dumps({"repo":REPO,"revision":REV,"files":files},indent=2)+"\n",encoding="utf-8")
    print("SNAPSHOT_READY",DEST,flush=True)
