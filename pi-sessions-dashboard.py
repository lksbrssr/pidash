#!/usr/bin/env python3
"""Generate a visual HTML dashboard of all pi agent sessions.

Layout:
  - Left sidebar: searchable list of session names (click to open)
  - Center: full conversation transcript of the selected session
  - Right: slide-in metadata panel (cost, tokens, model...) you can show/hide

Scans ~/.pi/agent/sessions/ and writes a single self-contained HTML file.
"""
import base64
import json
import os
import re
import glob
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"
SKILLS_DIR = Path.home() / ".pi" / "agent" / "skills"
OUT = Path.home() / ".pi" / "sessions-dashboard.html"
HOME_BASE = os.path.basename(str(Path.home()))

_GH = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
_URL = re.compile(r"https?://\S+")
_PATH = re.compile(r"/(?:Users|tmp|var|opt|home|private)/[^\s'\"]+")
_TAG = re.compile(r"\[(?:telegram|reply|attachments|outputs|time|voice|guest)[^\]]*\]", re.I)
_NOISE_REPO = {"", "blob", "tree", "pull", "pulls", "issues", "commit",
               "commits", "compare", "releases", "actions", "wiki"}

# Merge repos that are the same project under different names (old -> canonical)
REPO_ALIASES = {
    "plrd-radar-curator": "pl-radar",
    "plresearch.org": "plrd.org",
}


def detect_repo(blob, cwd):
    """Pick the repo a session worked on: the most-mentioned github repo,
    else a non-home working directory, else 'other'."""
    counter = Counter()
    for m in _GH.finditer(blob or ""):
        repo = re.sub(r"\.git$", "", m.group(2)).strip(".-")
        if repo.lower() in _NOISE_REPO:
            continue
        counter[repo] += 1
    if counter:
        # most mentioned; tie-break on longer (more specific) name
        repo = max(counter, key=lambda k: (counter[k], len(k)))
        return REPO_ALIASES.get(repo, repo)
    base = os.path.basename((cwd or "").rstrip("/"))
    if base and base != HOME_BASE:
        return REPO_ALIASES.get(base, base)
    return "other"


def clean_title(name, first_user):
    """Descriptive title: session name if set, else the first user message with
    URLs, file paths, and bracket tags stripped so the actual ask shows."""
    if name:
        return name.strip()
    t = first_user or ""
    t = _TAG.sub("", t)
    t = _URL.sub("", t)
    t = _PATH.sub("", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([.,;:!?])", r"\1", t)   # "to ." -> "to."
    t = re.sub(r"([.,;:])\1+", r"\1", t)      # collapse repeats
    t = re.sub(r"\(\s*\)|\[\s*\]", "", t)     # empty brackets left over
    t = re.sub(r"\s+", " ", t).strip(" .,-—:;/()")
    if not t:
        return "(untitled)"
    t = t[0].upper() + t[1:]
    return t[:100]

# Truncation limits to keep the single file a sane size
MAX_TEXT = 6000        # per text/thinking block
MAX_TOOL = 1200        # per tool result / bash output
MAX_MSGS = 400         # per session


def first_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))
        return "\n".join(out)
    return ""


def trunc(s, n):
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n] + f"\n… [truncated {len(s)-n} chars]"


def parse_session(path):
    header = None
    name = None
    cwd = None
    first_user = None
    counts = {"user": 0, "assistant": 0, "toolResult": 0, "other": 0}
    total_cost = 0.0
    total_tokens = 0
    models = set()
    model_cost = {}   # model -> cost
    first_ts = None
    last_ts = None
    msgs = []

    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get("type")
                ts = e.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                if t == "session":
                    header = e
                    cwd = e.get("cwd")
                elif t == "session_info":
                    name = e.get("name") or name
                elif t == "message":
                    m = e.get("message", {})
                    role = m.get("role")
                    if role == "user":
                        counts["user"] += 1
                        txt = first_text(m.get("content"))
                        if first_user is None and txt:
                            first_user = txt
                        if len(msgs) < MAX_MSGS:
                            msgs.append({"r": "user", "t": trunc(txt, MAX_TEXT)})
                    elif role == "assistant":
                        counts["assistant"] += 1
                        if m.get("model"):
                            models.add(m["model"])
                        u = m.get("usage", {})
                        total_tokens += u.get("totalTokens", 0) or 0
                        c = u.get("cost", {})
                        if isinstance(c, dict):
                            ct = c.get("total", 0) or 0
                            total_cost += ct
                            mdl = m.get("model") or "unknown"
                            model_cost[mdl] = model_cost.get(mdl, 0) + ct
                        # build assistant content: text + thinking + tool calls
                        parts = []
                        content = m.get("content")
                        if isinstance(content, list):
                            for b in content:
                                if not isinstance(b, dict):
                                    continue
                                bt = b.get("type")
                                if bt == "text" and b.get("text", "").strip():
                                    parts.append({"k": "text", "v": trunc(b["text"], MAX_TEXT)})
                                elif bt == "thinking" and b.get("thinking", "").strip():
                                    parts.append({"k": "think", "v": trunc(b["thinking"], MAX_TEXT)})
                                elif bt == "toolCall":
                                    args = b.get("arguments", {})
                                    parts.append({"k": "tool", "v": b.get("name", "tool"),
                                                  "a": trunc(json.dumps(args)[:400], 400)})
                        elif isinstance(content, str) and content.strip():
                            parts.append({"k": "text", "v": trunc(content, MAX_TEXT)})
                        if len(msgs) < MAX_MSGS and parts:
                            msgs.append({"r": "assistant", "p": parts})
                    elif role == "toolResult":
                        counts["toolResult"] += 1
                        out = first_text(m.get("content"))
                        if len(msgs) < MAX_MSGS:
                            msgs.append({"r": "toolResult", "n": m.get("toolName", ""),
                                         "t": trunc(out, MAX_TOOL), "e": bool(m.get("isError"))})
                    elif role == "bashExecution":
                        counts["other"] += 1
                        if len(msgs) < MAX_MSGS:
                            msgs.append({"r": "bash", "cmd": trunc(m.get("command", ""), 500),
                                         "t": trunc(m.get("output", ""), MAX_TOOL)})
                    else:
                        counts["other"] += 1
    except OSError:
        return None

    if header is None and first_ts is None:
        return None

    project = "unknown"
    if cwd:
        project = os.path.basename(cwd.rstrip("/")) or cwd

    title = clean_title(name, first_user)

    # Build a bounded text blob to detect which repo the work was for.
    blob_parts = [first_user or ""]
    for mm in msgs:
        if mm["r"] == "user":
            blob_parts.append(mm.get("t", ""))
        elif mm["r"] == "assistant":
            for p in mm.get("p", []):
                blob_parts.append(p.get("v", ""))
                blob_parts.append(p.get("a", ""))
        else:
            blob_parts.append(mm.get("t", ""))
            blob_parts.append(mm.get("cmd", ""))
    blob = " ".join(blob_parts)[:500000]
    repo = detect_repo(blob, cwd)

    return {
        "id": (header or {}).get("id", ""),
        "name": name or "",
        "title": title,
        "repo": repo,
        "cwd": cwd or "",
        "project": project,
        "preview": (first_user or "").replace("\n", " ").strip()[:160],
        "counts": counts,
        "msgTotal": counts["user"] + counts["assistant"],
        "cost": round(total_cost, 4),
        "modelCost": {k: round(v, 4) for k, v in model_cost.items()},
        "tokens": total_tokens,
        "models": sorted(models),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "file": os.path.basename(path),
        "path": str(path),
        "msgs": msgs,
    }


def fmt_dt(ts):
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return ts


_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _fm_field(fm, key):
    """Pull a single frontmatter scalar (name:/description:), supporting a
    value that spills onto the next indented line."""
    m = re.search(rf"^{key}\s*:\s*(.*)$", fm, re.M)
    if not m:
        return ""
    val = m.group(1).strip().strip('"\'')
    return re.sub(r"\s+", " ", val).strip()


def scan_skills():
    """Read every SKILL.md under ~/.pi/agent/skills and return metadata +
    rendered body so the dashboard can show a browsable folder of skills."""
    skills = []
    if not SKILLS_DIR.exists():
        return skills
    for d in sorted(SKILLS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir():
            continue
        md = d / "SKILL.md"
        if not md.exists():
            continue
        try:
            raw = md.read_text(encoding="utf-8")
        except OSError:
            continue
        name, desc, body = d.name, "", raw
        fm = _FM.match(raw)
        if fm:
            name = _fm_field(fm.group(1), "name") or d.name
            desc = _fm_field(fm.group(1), "description")
            body = raw[fm.end():]
        # list companion files (besides SKILL.md) so referenced assets show up
        files = []
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.name != "SKILL.md":
                files.append(str(f.relative_to(d)))
        skills.append({
            "name": name,
            "dir": d.name,
            "desc": desc,
            "body": trunc(body.strip(), 40000),
            "files": files[:100],
            "path": str(md),
            "words": len(body.split()),
        })
    return skills


def build():
    files = sorted(glob.glob(str(SESSIONS_DIR / "*" / "*.jsonl")))
    sessions = []
    for f in files:
        s = parse_session(f)
        if s:
            s["dateStr"] = fmt_dt(s["last_ts"])
            sessions.append(s)
    sessions.sort(key=lambda s: s["last_ts"] or "", reverse=True)

    total_cost = sum(s["cost"] for s in sessions)
    total_msgs = sum(s["msgTotal"] for s in sessions)
    total_tokens = sum(s["tokens"] for s in sessions)
    projects = sorted({s["project"] for s in sessions})

    # Average monthly spend, pro-rated: total cost / actual elapsed time.
    # Span = earliest session .. latest session, converted to fractional
    # months (30.44 days/mo). This correctly handles partial first/last
    # months (e.g. started mid-May, only 9 days into July).
    all_ts = []
    for s in sessions:
        for t in (s["first_ts"], s["last_ts"]):
            if t:
                try:
                    all_ts.append(datetime.fromisoformat(t.replace("Z", "+00:00")))
                except (ValueError, AttributeError):
                    pass
    if len(all_ts) >= 2:
        span_days = (max(all_ts) - min(all_ts)).total_seconds() / 86400
        n_months = max(span_days / 30.44, 1 / 30.44)  # >= 1 day
    else:
        n_months = 1
    avg_monthly = total_cost / n_months

    # ---- Insights aggregation ----
    daily = {}          # YYYY-MM-DD -> cost
    monthly = {}        # YYYY-MM -> {cost, tokens}
    by_project = {}     # project -> {cost, sessions}
    by_repo = {}        # repo group -> {cost, sessions}
    by_model = {}       # model -> cost
    for s in sessions:
        d = (s["last_ts"] or "")[:10]
        mth = (s["last_ts"] or "")[:7]
        if d:
            daily[d] = daily.get(d, 0) + s["cost"]
        if mth:
            mm = monthly.setdefault(mth, {"cost": 0, "tokens": 0})
            mm["cost"] += s["cost"]
            mm["tokens"] += s["tokens"]
        p = by_project.setdefault(s["project"], {"cost": 0, "sessions": 0})
        p["cost"] += s["cost"]
        p["sessions"] += 1
        r = by_repo.setdefault(s["repo"], {"cost": 0, "sessions": 0})
        r["cost"] += s["cost"]
        r["sessions"] += 1
        for mdl, c in s.get("modelCost", {}).items():
            by_model[mdl] = by_model.get(mdl, 0) + c

    # Fill daily gaps so the timeline chart has no missing days
    daily_series = []
    if daily:
        d0 = min(datetime.fromisoformat(x) for x in daily)
        d1 = max(datetime.fromisoformat(x) for x in daily)
        cur = d0
        while cur <= d1:
            key = cur.strftime("%Y-%m-%d")
            daily_series.append({"d": key, "cost": round(daily.get(key, 0), 2)})
            cur = cur + timedelta(days=1)

    insights = {
        "daily": daily_series,
        "monthly": [{"m": k, "cost": round(v["cost"], 2), "tokens": v["tokens"]}
                    for k, v in sorted(monthly.items())],
        "byProject": sorted(
            [{"name": k, "cost": round(v["cost"], 2), "sessions": v["sessions"]}
             for k, v in by_project.items()], key=lambda x: -x["cost"]),
        "byRepo": sorted(
            [{"name": k, "cost": round(v["cost"], 2), "sessions": v["sessions"]}
             for k, v in by_repo.items()], key=lambda x: -x["cost"]),
        "byModel": sorted(
            [{"name": k, "cost": round(v, 2)} for k, v in by_model.items()],
            key=lambda x: -x["cost"]),
        "topSessions": [
            {"title": s["title"], "cost": s["cost"], "project": s["project"],
             "date": s["dateStr"]}
            for s in sorted(sessions, key=lambda x: -x["cost"])[:10]],
        "avgMonthly": round(avg_monthly, 2),
        "totalCost": round(total_cost, 2),
    }
    # Embed both payloads as base64-encoded UTF-8 JSON. This is immune to every
    # HTML/JS escaping hazard ("</script>", quotes, <, >, &, U+2028) — critical
    # here because sessions can contain the dashboard's own source code.
    data_b64 = base64.b64encode(
        json.dumps(sessions, ensure_ascii=False).encode("utf-8")).decode("ascii")
    insights_b64 = base64.b64encode(
        json.dumps(insights, ensure_ascii=False).encode("utf-8")).decode("ascii")
    skills = scan_skills()
    skills_b64 = base64.b64encode(
        json.dumps(skills, ensure_ascii=False).encode("utf-8")).decode("ascii")
    gen = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")

    doc = HTML.replace("__DATA__", data_b64) \
              .replace("__INSIGHTS__", insights_b64) \
              .replace("__SKILLS__", skills_b64) \
              .replace("__GEN__", gen) \
              .replace("__DIR__", str(SESSIONS_DIR)) \
              .replace("__NSESS__", str(len(sessions))) \
              .replace("__NPROJ__", str(len(projects))) \
              .replace("__NMSGS__", f"{total_msgs:,}") \
              .replace("__NTOK__", f"{total_tokens/1_000_000:.1f}M") \
              .replace("__NCOST__", f"${total_cost:,.2f}") \
              .replace("__NAVG__", f"${avg_monthly:,.2f}")

    OUT.write_text(doc, encoding="utf-8")
    size = OUT.stat().st_size
    print(f"Wrote {OUT}  ({size/1_000_000:.1f} MB)")
    print(f"  {len(sessions)} sessions, {len(projects)} projects, "
          f"{len(skills)} skills, ${total_cost:.2f} est. cost")


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pi sessions</title>
<style>
  :root{
    --bg:#0a0b0d; --bg2:#0f1113; --panel:#111316; --panel2:#171a1e; --border:#22262c;
    --fg:#e9ebef; --muted:#818894; --accent:#7aa2f7; --accentbg:#7aa2f714;
    --accentln:#7aa2f733; --tagfg:#9db8fb; --green:#5fb98a; --think:#a58cf5;
  }
  *{box-sizing:border-box;}
  ::selection{background:var(--accentln);}
  .num{font-variant-numeric:tabular-nums;}
  html,body{height:100%;margin:0;}
  body{background:var(--bg);color:var(--fg);
    font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    display:flex;flex-direction:column;overflow:hidden;}
  header{padding:13px 20px;border-bottom:1px solid var(--border);display:flex;
    align-items:center;gap:18px;flex-wrap:wrap;}
  header h1{margin:0;font-size:14px;font-weight:600;white-space:nowrap;letter-spacing:.03em;
    color:var(--fg);}
  .hstats{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;
    display:flex;gap:14px;flex-wrap:wrap;}
  .hstats .st b{color:var(--fg);font-weight:600;}
  .hstats .st{white-space:nowrap;}
  .main{flex:1;display:flex;min-height:0;position:relative;}

  /* Far-left nav rail */
  .nav{width:70px;min-width:70px;background:var(--bg2);border-right:1px solid var(--border);
    display:flex;flex-direction:column;align-items:center;padding-top:12px;gap:6px;}
  .navbtn{width:56px;padding:10px 0;border-radius:10px;background:none;border:none;color:var(--muted);
    cursor:pointer;font-size:10.5px;letter-spacing:.02em;display:flex;flex-direction:column;
    align-items:center;gap:5px;transition:background .12s,color .12s;}
  .navbtn .ic{width:20px;height:20px;display:block;}
  .navbtn .ic svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:1.7;}
  .navbtn:hover{background:var(--panel2);color:var(--fg);}
  .navbtn.active{background:var(--accentbg);color:var(--accent);}

  /* Insights */
  .insights{flex:1;overflow-y:auto;padding:22px 30px;min-width:0;display:none;}
  .insights.show{display:block;}
  .chats-wrap{flex:1;display:flex;min-width:0;}
  .chats-wrap.hide{display:none;}
  .skills-wrap{flex:1;display:none;min-width:0;}
  .skills-wrap.show{display:flex;}
  .skillcol{width:300px;min-width:260px;}
  .skillitem{padding:11px 12px;border-bottom:1px solid var(--border);cursor:pointer;}
  .skillitem:hover{background:var(--panel2);}
  .skillitem.active{background:var(--accentbg);border-left:3px solid var(--accent);padding-left:9px;}
  .skillitem .t{font-weight:600;font-size:13px;}
  .skillitem .d{color:var(--muted);font-size:11px;margin-top:3px;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
  .skillbody{max-width:820px;white-space:pre-wrap;word-wrap:break-word;
    background:var(--panel);border:1px solid var(--border);border-radius:10px;
    padding:16px 18px;font-size:13px;line-height:1.6;}
  .skillfiles{margin-top:14px;font-size:12px;color:var(--muted);}
  .skillfiles code{color:var(--tagfg);}
  .skilldesc{color:var(--muted);font-size:13px;margin:6px 0 14px;font-style:italic;}
  .skillcmds{max-width:820px;margin:0 0 16px;}
  .skillcmds .ck{color:var(--muted);font-size:11px;text-transform:uppercase;
    letter-spacing:.04em;margin:10px 0 3px;}
  .insights h2{margin:0 0 4px;font-size:20px;}
  .insights .lead{color:var(--muted);font-size:13px;margin-bottom:20px;}
  .kpis{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:26px;}
  .kpi{background:var(--panel);border:1px solid var(--border);border-radius:12px;
    padding:14px 20px;min-width:130px;}
  .kpi .n{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.01em;}
  .kpi .n.cost{color:var(--green);}
  .kpi .l{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;margin-top:2px;}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;
    padding:16px 18px;margin-bottom:20px;}
  .card h3{margin:0 0 14px;font-size:14px;}
  /* bar chart */
  .bars{display:flex;align-items:flex-end;gap:3px;height:180px;}
  .bar{flex:1;min-width:3px;background:var(--accent);opacity:.85;
    border-radius:2px 2px 0 0;position:relative;transition:opacity .15s;}
  .bar:hover{opacity:1;}
  .bar .tip{display:none;position:absolute;bottom:100%;left:50%;transform:translateX(-50%);
    background:#000;border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:11px;
    white-space:nowrap;z-index:10;margin-bottom:4px;}
  .bar:hover .tip{display:block;}
  .axis{display:flex;gap:3px;margin-top:6px;}
  .axis span{flex:1;text-align:center;color:var(--muted);font-size:9px;overflow:hidden;
    white-space:nowrap;}
  /* horizontal ranked bars */
  .hbar{margin:8px 0;}
  .hbar .lbl{display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px;}
  .hbar .lbl .c{color:var(--green);font-variant-numeric:tabular-nums;}
  .hbar .track{background:var(--panel2);border-radius:4px;height:8px;overflow:hidden;}
  .hbar .fill{height:100%;background:var(--green);opacity:.9;border-radius:4px;}
  .toplist .row{display:flex;justify-content:space-between;gap:12px;padding:8px 0;
    border-bottom:1px solid var(--border);font-size:13px;}
  .toplist .row .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .toplist .row .c{color:var(--green);white-space:nowrap;}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
  @media(max-width:900px){.grid2{grid-template-columns:1fr;}}

  /* Column 1: folders (repo groups) */
  .folders{width:230px;min-width:200px;border-right:1px solid var(--border);
    overflow-y:auto;background:var(--panel);}
  .folders .colhead{padding:11px 12px;font-size:11px;text-transform:uppercase;
    letter-spacing:.05em;color:var(--muted);border-bottom:1px solid var(--border);
    position:sticky;top:0;background:var(--panel);}
  .folder{display:flex;align-items:center;gap:9px;padding:11px 12px;
    border-bottom:1px solid var(--border);cursor:pointer;}
  .folder:hover{background:var(--panel2);}
  .folder.active{background:var(--accentbg);border-left:3px solid var(--accent);padding-left:9px;}
  .folder .ficon{width:14px;height:14px;flex:none;color:var(--muted);}
  .folder.active .ficon{color:var(--accent);}
  .folder .ficon svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:1.7;}
  .folder .fname{flex:1;font-weight:500;font-size:13px;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap;}
  .folder .fmeta{color:var(--muted);font-size:11px;white-space:nowrap;
    font-variant-numeric:tabular-nums;}
  .folder .fmeta .fc{color:var(--green);}

  /* Column 2: chats in the selected folder */
  .chatcol{width:350px;min-width:300px;border-right:1px solid var(--border);
    display:flex;flex-direction:column;background:var(--panel);}
  .chatcol .search{padding:10px;border-bottom:1px solid var(--border);}
  .chatcol input{width:100%;background:var(--panel2);color:var(--fg);border:1px solid var(--border);
    border-radius:8px;padding:8px 11px;font-size:13px;outline:none;transition:border-color .12s;}
  .chatcol input:focus{border-color:var(--accent);}
  .chatcol input::placeholder{color:var(--muted);}
  .list{overflow-y:auto;flex:1;}
  .item{padding:10px 12px;border-bottom:1px solid var(--border);cursor:pointer;}
  .item:hover{background:var(--panel2);}
  .item.active{background:var(--accentbg);border-left:3px solid var(--accent);padding-left:9px;}
  .item .t{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;}
  .item .m{color:var(--muted);font-size:11px;margin-top:3px;display:flex;gap:8px;flex-wrap:wrap;
    font-variant-numeric:tabular-nums;}
  .item .m .cost{color:var(--green);}
  .tag{background:var(--accentbg);color:var(--tagfg);border-radius:20px;padding:1px 8px;font-size:11px;}

  /* Center transcript */
  .center{flex:1;overflow-y:auto;padding:20px 26px;min-width:0;}
  .placeholder{color:var(--muted);text-align:center;margin-top:12%;font-size:15px;}
  .convo-head{display:flex;align-items:flex-start;gap:12px;margin-bottom:16px;
    padding-bottom:14px;border-bottom:1px solid var(--border);}
  .convo-head h2{margin:0;font-size:18px;}
  .convo-head .sub{color:var(--muted);font-size:12px;margin-top:4px;}
  .infobtn{margin-left:auto;background:var(--panel2);color:var(--accent);border:1px solid var(--border);
    border-radius:8px;padding:7px 12px;cursor:pointer;font-size:13px;white-space:nowrap;}
  .infobtn:hover{background:var(--accentbg);}

  .msg{margin:12px 0;display:flex;flex-direction:column;}
  .msg .who{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
    margin-bottom:4px;}
  .bubble{padding:10px 14px;border-radius:10px;white-space:pre-wrap;word-wrap:break-word;
    border:1px solid var(--border);max-width:900px;}
  .user .bubble{background:var(--accentbg);border-color:var(--accentln);}
  .assistant .bubble{background:var(--panel);}
  .assistant{align-items:flex-start;}
  .user{align-items:flex-end;}
  .user .who{align-self:flex-end;}
  .think{color:var(--think);font-style:italic;border-left:2px solid var(--think);
    padding-left:10px;margin:6px 0;white-space:pre-wrap;font-size:13px;opacity:.85;}
  details.think-d summary{cursor:pointer;color:var(--think);font-size:12px;margin:6px 0;}
  .toolcall{color:var(--tagfg);font-size:12px;margin:4px 0;font-family:ui-monospace,Menlo,monospace;}
  .toolcall .args{color:var(--muted);}
  details.tool summary{cursor:pointer;color:var(--muted);font-size:12px;margin:6px 0;}
  details.tool pre,details.bash pre{background:var(--bg2);border:1px solid var(--border);border-radius:8px;
    padding:8px 10px;overflow-x:auto;font-size:12px;margin:4px 0;max-height:340px;white-space:pre-wrap;}
  details.tool.err summary{color:#ff7b72;}
  .bash .cmd{color:var(--green);font-family:ui-monospace,Menlo,monospace;font-size:12px;}

  /* Right slide-in meta */
  .meta{position:absolute;top:0;right:0;height:100%;width:320px;background:var(--panel);
    border-left:1px solid var(--border);transform:translateX(100%);transition:transform .22s ease;
    padding:18px;overflow-y:auto;z-index:5;}
  .meta.open{transform:translateX(0);}
  .meta h3{margin:0 0 12px;font-size:14px;display:flex;align-items:center;}
  .meta .close{margin-left:auto;cursor:pointer;color:var(--muted);background:none;border:none;font-size:18px;}
  .meta .row{padding:9px 0;border-bottom:1px solid var(--border);}
  .meta .row .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;}
  .meta .row .v{font-size:14px;margin-top:2px;word-break:break-all;font-variant-numeric:tabular-nums;}
  .meta .v.cost{color:var(--green);}
  .meta code{font-size:11px;color:var(--muted);}
  .cmdbox{margin-top:6px;background:var(--panel2);border:1px solid var(--border);border-radius:6px;
    padding:6px 8px;font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--accent);
    cursor:pointer;word-break:break-all;}
  .cmdbox:hover{background:var(--accentbg);}
</style>
</head>
<body>
<header>
  <h1>pi&nbsp;·&nbsp;sessions</h1>
  <div class="hstats">
    <span class="st"><b>__NSESS__</b> sessions</span>
    <span class="st"><b>__NPROJ__</b> projects</span>
    <span class="st"><b>__NMSGS__</b> msgs</span>
    <span class="st"><b>__NTOK__</b> tokens</span>
    <span class="st"><b>__NCOST__</b> est. cost</span>
    <span class="st"><b>__NAVG__</b>/mo avg</span>
  </div>
</header>

<div class="main">
  <nav class="nav">
    <button class="navbtn active" id="navChats" onclick="showView('chats')">
      <span class="ic"><svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></span>Chats</button>
    <button class="navbtn" id="navSkills" onclick="showView('skills')">
      <span class="ic"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/></svg></span>Skills</button>
    <button class="navbtn" id="navInsights" onclick="showView('insights')">
      <span class="ic"><svg viewBox="0 0 24 24"><line x1="5" y1="21" x2="5" y2="11"/><line x1="12" y1="21" x2="12" y2="4"/><line x1="19" y1="21" x2="19" y2="14"/></svg></span>Insights</button>
  </nav>

  <div class="chats-wrap" id="chatsWrap">
    <aside class="folders" id="folders"></aside>

    <aside class="chatcol">
      <div class="search"><input id="q" type="search" placeholder="Search in folder…"></div>
      <div class="list" id="list"></div>
    </aside>

    <section class="center" id="center">
      <div class="placeholder">Pick a folder, then a chat to read the conversation.</div>
    </section>

    <aside class="meta" id="meta">
      <h3>Details<button class="close" id="metaClose">×</button></h3>
      <div id="metaBody"></div>
    </aside>
  </div>

  <section class="insights" id="insights"></section>

  <div class="skills-wrap" id="skillsWrap">
    <aside class="chatcol skillcol">
      <div class="search"><input id="sq" type="search" placeholder="Search skills…"></div>
      <div class="list" id="skillList"></div>
    </aside>
    <section class="center" id="skillCenter">
      <div class="placeholder">Pick a skill to read it.</div>
    </section>
  </div>
</div>

<script>
function b64json(s){
  const bin = atob(s);
  const bytes = Uint8Array.from(bin, c => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}
const SESSIONS = b64json("__DATA__");
const INSIGHTS = b64json("__INSIGHTS__");
const SKILLS = b64json("__SKILLS__");
const GEN = "__GEN__", DIR = "__DIR__";
const listEl = document.getElementById('list');
const centerEl = document.getElementById('center');
const metaEl = document.getElementById('meta');
const metaBody = document.getElementById('metaBody');
const q = document.getElementById('q');
let activeIdx = -1;

function esc(s){return (s||"").replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
const FOLDER_SVG='<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

const foldersEl = document.getElementById('folders');
let activeRepo = null;

// Build folders (repo groups) once: sessions per repo, ordered by most-recent
// activity, 'other' always last. Sessions within a repo stay newest-first
// (SESSIONS is globally sorted by last_ts desc).
const FOLDERS = (function(){
  const map = {};
  SESSIONS.forEach((s, i) => { (map[s.repo] = map[s.repo] || []).push(i); });
  const keys = Object.keys(map).sort((a,b)=>{
    if(a==='other') return 1;
    if(b==='other') return -1;
    const ma = map[a].reduce((x,i)=> SESSIONS[i].last_ts>x?SESSIONS[i].last_ts:x, '');
    const mb = map[b].reduce((x,i)=> SESSIONS[i].last_ts>x?SESSIONS[i].last_ts:x, '');
    return mb<ma?-1:(mb>ma?1:0);
  });
  return { map, keys };
})();

function renderFolders(){
  let html = '<div class="colhead">Folders</div>';
  FOLDERS.keys.forEach((k, ki) => {
    const idxs = FOLDERS.map[k];
    const cost = idxs.reduce((a,i)=>a+SESSIONS[i].cost,0);
    html += '<div class="folder'+(k===activeRepo?' active':'')+'" onclick="selectFolder('+ki+')">'+
      '<span class="ficon">'+FOLDER_SVG+'</span>'+
      '<span class="fname">'+esc(k)+'</span>'+
      '<span class="fmeta">'+idxs.length+' · <span class="fc">$'+cost.toFixed(0)+'</span></span></div>';
  });
  foldersEl.innerHTML = html;
}

function selectFolder(ki){
  activeRepo = FOLDERS.keys[ki];
  q.value = '';
  renderFolders();
  renderChats('');
}

function renderChats(term){
  term = (term||'').toLowerCase();
  const idxs = activeRepo!=null ? FOLDERS.map[activeRepo] : [];
  let html = '';
  for(const i of idxs){
    const s = SESSIONS[i];
    const hay = (s.title+' '+s.preview+' '+s.models.join(' ')).toLowerCase();
    if(term && !hay.includes(term)) continue;
    const cost = s.cost ? '$'+s.cost.toFixed(2) : '';
    html += '<div class="item'+(i===activeIdx?' active':'')+'" onclick="openSession('+i+')">'+
      '<div class="t">'+esc(s.title)+'</div>'+
      '<div class="m"><span>'+esc(s.dateStr)+'</span>'+
      '<span>'+s.msgTotal+' msgs</span>'+
      (cost?'<span class="cost">'+cost+'</span>':'')+'</div></div>';
  }
  listEl.innerHTML = html || '<div style="padding:16px;color:#8b949e">No chats here.</div>';
}

function openSession(i){
  activeIdx = i;
  renderChats(q.value);
  const s = SESSIONS[i];
  let h = '<div class="convo-head"><div><h2>'+esc(s.title)+'</h2>'+
    '<div class="sub">'+esc(s.dateStr)+' · '+esc(s.project)+' · '+s.msgTotal+' msgs'+
    (s.cost?' · $'+s.cost.toFixed(2):'')+'</div></div>'+
    '<button class="infobtn" onclick="toggleMeta()">Details</button></div>';

  for(const m of s.msgs){
    if(m.r==='user'){
      h += '<div class="msg user"><div class="who">You</div><div class="bubble">'+esc(m.t)+'</div></div>';
    } else if(m.r==='assistant'){
      let inner='';
      for(const p of (m.p||[])){
        if(p.k==='text') inner += '<div class="bubble">'+esc(p.v)+'</div>';
        else if(p.k==='think') inner += '<details class="think-d"><summary>thinking</summary><div class="think">'+esc(p.v)+'</div></details>';
        else if(p.k==='tool') inner += '<div class="toolcall">'+esc(p.v)+' <span class="args">'+esc(p.a||'')+'</span></div>';
      }
      h += '<div class="msg assistant"><div class="who">pi</div>'+inner+'</div>';
    } else if(m.r==='toolResult'){
      h += '<details class="tool'+(m.e?' err':'')+'"><summary>'+esc(m.n||'tool')+(m.e?' (error)':'')+'</summary><pre>'+esc(m.t)+'</pre></details>';
    } else if(m.r==='bash'){
      h += '<details class="bash"><summary class="cmd">$ '+esc(m.cmd)+'</summary><pre>'+esc(m.t)+'</pre></details>';
    }
  }
  centerEl.innerHTML = h;
  centerEl.scrollTop = 0;
  fillMeta(s);
}

function fillMeta(s){
  const row=(k,v,cls)=>'<div class="row"><div class="k">'+k+'</div><div class="v '+(cls||'')+'">'+v+'</div></div>';
  let h='';
  h += row('Title', esc(s.title));
  if(s.name) h += row('Name', esc(s.name));
  h += row('Date', esc(s.dateStr));
  h += row('Repo', esc(s.repo));
  h += row('Project', esc(s.project));
  h += row('Working dir', '<code>'+esc(s.cwd)+'</code>');
  h += row('Messages', s.msgTotal+' ('+s.counts.user+' you / '+s.counts.assistant+' pi)');
  h += row('Tool results', s.counts.toolResult);
  h += row('Tokens', s.tokens.toLocaleString());
  h += row('Est. cost', s.cost?'$'+s.cost.toFixed(4):'—', 'cost');
  h += row('Model(s)', esc(s.models.join(', ')||'—'));
  h += row('Session ID', '<code>'+esc(s.id)+'</code>');
  h += row('File', '<code>'+esc(s.file)+'</code>');
  const cmd=(s.cwd?'cd '+shq(s.cwd)+' && ':'')+'pi --session '+s.id;
  h += '<div class="row"><div class="k">Resume in pi — click to copy</div>'+
       '<div class="cmdbox" onclick="copyCmd(this)">'+esc(cmd)+'</div></div>';
  metaBody.innerHTML = h;
}

// POSIX single-quote a path so it survives spaces/specials in the shell.
function shq(p){ return "'"+String(p||'').replace(/'/g,"'\\''")+"'"; }

function toggleMeta(){ metaEl.classList.toggle('open'); }
document.getElementById('metaClose').addEventListener('click', ()=>metaEl.classList.remove('open'));
function copyCmd(el){ navigator.clipboard.writeText(el.textContent).then(()=>{
  const o=el.textContent; el.textContent='Copied'; setTimeout(()=>el.textContent=o,1000);}); }

q.addEventListener('input', ()=>renderChats(q.value));
activeRepo = FOLDERS.keys[0] || null;   // open the most-recent folder by default
renderFolders();
renderChats('');

/* ---------- View switching ---------- */
function showView(v){
  document.getElementById('chatsWrap').classList.toggle('hide', v!=='chats');
  document.getElementById('insights').classList.toggle('show', v==='insights');
  document.getElementById('skillsWrap').classList.toggle('show', v==='skills');
  document.getElementById('navChats').classList.toggle('active', v==='chats');
  document.getElementById('navSkills').classList.toggle('active', v==='skills');
  document.getElementById('navInsights').classList.toggle('active', v==='insights');
  if(v==='insights') renderInsights();
  if(v==='skills') renderSkills('');
}

/* ---------- Skills ---------- */
const skillListEl = document.getElementById('skillList');
const skillCenterEl = document.getElementById('skillCenter');
const sq = document.getElementById('sq');
let activeSkill = -1;

function renderSkills(term){
  term = (term||'').toLowerCase();
  let html = '';
  SKILLS.forEach((s,i)=>{
    const hay = (s.name+' '+s.desc+' '+s.dir).toLowerCase();
    if(term && !hay.includes(term)) return;
    html += '<div class="skillitem'+(i===activeSkill?' active':'')+'" onclick="openSkill('+i+')">'+
      '<div class="t">'+esc(s.name)+'</div>'+
      '<div class="d">'+esc(s.desc)+'</div></div>';
  });
  skillListEl.innerHTML = html || '<div style="padding:16px;color:#8b949e">No skills found.</div>';
}

function openSkill(i){
  activeSkill = i;
  renderSkills(sq.value);
  const s = SKILLS[i];
  let h = '<div class="convo-head"><div><h2>'+esc(s.name)+'</h2>'+
    '<div class="sub">'+esc(s.dir)+' · '+s.words+' words · '+s.files.length+' file(s)</div></div></div>';
  h += '<div class="skilldesc">'+esc(s.desc)+'</div>';
  const dir = s.path.replace(/\/SKILL\.md$/,'');
  h += '<div class="skillcmds">'+
    '<div class="ck">Edit in your editor — click to copy</div>'+
    '<div class="cmdbox" onclick="copyCmd(this)">${EDITOR:-vi} '+esc(shq(s.path))+'</div>'+
    '<div class="ck">Edit with pi (agent) — click to copy</div>'+
    '<div class="cmdbox" onclick="copyCmd(this)">cd '+esc(shq(dir))+' && pi @SKILL.md</div>'+
    '</div>';
  h += '<div class="skillbody">'+esc(s.body)+'</div>';
  if(s.files.length){
    h += '<div class="skillfiles"><b>Files:</b> '+
      s.files.map(f=>'<code>'+esc(f)+'</code>').join(', ')+'</div>';
  }
  h += '<div class="skillfiles">Path: <code>'+esc(s.path)+'</code></div>';
  skillCenterEl.innerHTML = h;
  skillCenterEl.scrollTop = 0;
}

sq.addEventListener('input', ()=>renderSkills(sq.value));

/* ---------- Insights ---------- */
function money(n){return '$'+(+n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});}

function barChart(series, keyLabel, keyVal, fmt){
  const max = Math.max(...series.map(d=>d[keyVal]), 0.0001);
  let bars='', axis='';
  const step = Math.ceil(series.length/12) || 1;
  series.forEach((d,i)=>{
    const h = Math.max((d[keyVal]/max)*100, d[keyVal]>0?2:0);
    bars += '<div class="bar" style="height:'+h+'%">'+
      '<span class="tip">'+esc(d[keyLabel])+': '+fmt(d[keyVal])+'</span></div>';
    axis += '<span>'+(i%step===0?esc(d[keyLabel].slice(5)):'')+'</span>';
  });
  return '<div class="bars">'+bars+'</div><div class="axis">'+axis+'</div>';
}

function hbars(items, keyName, keyVal){
  const max = Math.max(...items.map(d=>d[keyVal]), 0.0001);
  return items.map(d=>{
    const pct = (d[keyVal]/max)*100;
    return '<div class="hbar"><div class="lbl"><span>'+esc(d[keyName])+'</span>'+
      '<span class="c">'+money(d[keyVal])+'</span></div>'+
      '<div class="track"><div class="fill" style="width:'+pct+'%"></div></div></div>';
  }).join('');
}

let insightsDrawn=false;
function renderInsights(){
  if(insightsDrawn) return; insightsDrawn=true;
  const I = INSIGHTS;
  const nDays = I.daily.length;
  let h = '<h2>Insights</h2>'+
    '<div class="lead">Spend and usage across '+SESSIONS.length+' sessions · generated '+GEN+'</div>';

  h += '<div class="kpis">'+
    '<div class="kpi"><div class="n cost">'+money(I.totalCost)+'</div><div class="l">Total spend</div></div>'+
    '<div class="kpi"><div class="n cost">'+money(I.avgMonthly)+'</div><div class="l">Avg / month</div></div>'+
    '<div class="kpi"><div class="n">'+nDays+'</div><div class="l">Active days</div></div>'+
    '<div class="kpi"><div class="n cost">'+money(I.daily.reduce((a,d)=>a+d.cost,0)/Math.max(nDays,1))+'</div><div class="l">Avg / active day</div></div>'+
    '</div>';

  h += '<div class="card"><h3>Spend over time (daily)</h3>'+
    barChart(I.daily,'d','cost',money)+'</div>';

  h += '<div class="card"><h3>Spend by month</h3>'+
    barChart(I.monthly,'m','cost',money)+'</div>';

  h += '<div class="grid2">'+
    '<div class="card"><h3>Cost by repo group</h3>'+hbars(I.byRepo,'name','cost')+'</div>'+
    '<div class="card"><h3>Cost by model</h3>'+hbars(I.byModel,'name','cost')+'</div>'+
    '</div>';

  h += '<div class="card toplist"><h3>Most expensive sessions</h3>'+
    I.topSessions.map(s=>'<div class="row"><span class="t">'+esc(s.title)+
      ' <span style="color:#8b949e">· '+esc(s.date)+'</span></span>'+
      '<span class="c">'+money(s.cost)+'</span></div>').join('')+'</div>';

  document.getElementById('insights').innerHTML = h;
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    build()
