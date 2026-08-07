#!/usr/bin/env python3
import argparse, json, os, sys, time, urllib.parse, urllib.request
from pathlib import Path

import captain_telemetry
from clickup_credentials import MissingClickUpCredentials, load_clickup_credentials

API="https://api.clickup.com/api/v2"

def req(url, token):
    r=urllib.request.Request(url, headers={"Authorization": token})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())

def main():
    ap=argparse.ArgumentParser(description="Fetch ClickUp tasks read-only, always including subtasks and pagination")
    ap.add_argument("--out", required=True)
    ap.add_argument("--include-closed", action="store_true")
    args=ap.parse_args()
    try:
        credentials=load_clickup_credentials(("CLICKUP_API_KEY", "CLICKUP_TEAM_ID"))
    except MissingClickUpCredentials as error:
        raise SystemExit(str(error))
    token=credentials["CLICKUP_API_KEY"]
    team=credentials["CLICKUP_TEAM_ID"]
    list_ids=[x.strip() for x in os.environ.get("CAPTAIN_CLICKUP_LIST_IDS","").split(",") if x.strip()]
    all_tasks=[]
    if list_ids:
        bases=[f"{API}/list/{lid}/task" for lid in list_ids]
    else:
        bases=[f"{API}/team/{team}/task"]
    for base in bases:
        page=0
        while True:
            qs={"subtasks":"true","include_closed":str(args.include_closed).lower(),"page":str(page)}
            data=req(base+"?"+urllib.parse.urlencode(qs), token)
            all_tasks.extend(data.get("tasks",[]))
            if data.get("last_page", True): break
            page+=1
            time.sleep(0.7)
    out=Path(args.out).expanduser().resolve(); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tasks":all_tasks}, indent=2), encoding="utf-8")
    print(json.dumps({"out":str(out),"tasks":len(all_tasks)}, indent=2))
if __name__ == "__main__":
    with captain_telemetry.guard("fetch_clickup_tasks"):
        main()
