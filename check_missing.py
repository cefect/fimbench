 
import json, requests
import fimeval 

missing = []
q = fimeval.benchFIMquery(download=False)

for m in q.get("matches", []):
    r = m.get("record") or {}
    aid = str(r.get("id") or "").strip()
    fn = str(r.get("file_name") or "").strip()
    url = str(r.get("tif_url") or "").strip()
    if not (aid and fn and url):
        continue
    try:
        code = requests.head(url, allow_redirects=True, timeout=10).status_code
    except requests.RequestException:
        continue
    if code != 200:
        missing.append({"asset_id": aid, "file_name": fn, "head_status": code})

print(json.dumps(missing, indent=2))
 