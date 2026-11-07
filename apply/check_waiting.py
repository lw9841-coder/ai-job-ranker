"""Re-fill every application waiting for a Send it tap, without submitting, and make sure it's still ready."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apply import apply as a  # noqa: E402
from apply import form  # noqa: E402

state = a.load_state()
for s, rec in state["jobs"].items():
    if rec.get("status") != "waiting":
        continue
    try:
        form.run(rec["form_url"], plan={"answers": rec["answers"], "files": rec["files"], "labels": rec["labels"]})
        print("ready:", rec["company"])
    except Exception as e:
        print("NOT READY:", rec["company"], "|", str(e)[:200])
