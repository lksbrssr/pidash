# pidash

A single-file, self-contained HTML dashboard for your [pi](https://github.com/earendil-works/pi) agent sessions.

It scans `~/.pi/agent/sessions/` and `~/.pi/agent/skills/` and generates one
static `sessions-dashboard.html` you can open in any browser — no server, no
build step, no dependencies beyond Python 3.

## Features

- **Chats** — every session grouped into folders by the repo it worked on
  (`other` for sessions with no repo). Descriptive auto-titles, full transcript
  view (text, thinking, tool calls, bash output), and per-session search.
- **Skills** — a browsable folder of every skill under `~/.pi/agent/skills/`,
  showing name, description, and full `SKILL.md` body.
- **Insights** — spend over time (daily/monthly), cost by repo, cost by model,
  most expensive sessions, and pro-rated average monthly spend.
- **Copy-to-run commands** — each chat gives you a ready-to-paste
  `cd <cwd> && pi --session <id>` to resume it; each skill gives you a command
  to open it in your editor or hand it to pi for editing.

## Usage

```bash
python3 pi-sessions-dashboard.py
open ~/.pi/sessions-dashboard.html   # macOS; use xdg-open on Linux
```

### One-word shell function

Add to `~/.zshrc` (or `~/.bashrc`):

```bash
# pi sessions dashboard — regenerate from ~/.pi and open in browser
pidash() {
  python3 ~/dev/pidash/pi-sessions-dashboard.py && open ~/.pi/sessions-dashboard.html
}
```

Then just run `pidash` — it regenerates from your latest sessions and opens it.

## How it works

- Reads every `*.jsonl` under `~/.pi/agent/sessions/`.
- Detects the repo per session from GitHub references / working directory.
- Reads every `SKILL.md` under `~/.pi/agent/skills/` and parses its frontmatter.
- Embeds all data as **base64-encoded JSON** inside the HTML. Base64 can't
  contain any HTML/JS-breaking characters, so the file is bulletproof even when
  your sessions contain the dashboard's own source code (which they will).

## Privacy

The generated `sessions-dashboard.html` contains your **full session
transcripts** — it is deliberately `.gitignore`d. Never commit it. Only the
generator script lives in this repo.

## Paths

| What | Where |
| --- | --- |
| Generator | `pi-sessions-dashboard.py` |
| Sessions source | `~/.pi/agent/sessions/` |
| Skills source | `~/.pi/agent/skills/` |
| Generated output | `~/.pi/sessions-dashboard.html` (gitignored) |
