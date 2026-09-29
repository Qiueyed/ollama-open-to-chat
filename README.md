# ollama-open-to-chat

Make the Ollama macOS app open on **Chat** instead of the **Apps** page.

After a full quit (menu bar -> Quit Ollama) or a Mac restart, the Ollama app
always opens on the Apps page. Closing only the window (app stays alive in the
menu bar) reopens on Chat fine. There is no setting for this, and as of
v0.35.0 no upstream fix.

This tool patches the app locally so cold start opens on Chat. It changes
**8 bytes** in one internal navigation string, keeps a backup, and can revert
cleanly. No network access, no background processes, nothing else modified.

```
python3 open-to-chat.py            # patch (with Ollama fully quit)
python3 open-to-chat.py --check    # status probe
python3 open-to-chat.py --revert   # undo (with Ollama fully quit)
```

## Requirements

- macOS 14+ with the Ollama app in `/Applications` (tested on **0.34.2**,
  Apple Silicon; the tool is scan-based and refuses builds it does not
  recognize, so wrong versions fail safe)
- Python 3 (preinstalled on macOS via the command line tools)
- Quit Ollama fully before patching or reverting

## Get it

```sh
curl -fsSL -o open-to-chat.py https://raw.githubusercontent.com/Qiueyed/ollama-open-to-chat/main/open-to-chat.py
```

Read the script before running it (that is the point of downloading rather
than piping to a shell), then:

```sh
python3 open-to-chat.py
```

## What it actually does

Static analysis of the app (details in [HOW-IT-WORKS.md](HOW-IT-WORKS.md))
showed that the embedded UI would already open on Chat for every route except
one: the Go host navigates the fresh webview to `/connect` (the Apps page) on
cold start, ignoring its own saved `LastHomeView` setting. The patch rewrites
that one navigation string in place:

```
/connect  ->  /c/new#/      (8 bytes -> 8 bytes, trailing fragment ignored)
```

- The in-app sidebar "Apps" button keeps working (that lives in the JS bundle).
- A one-time backup of your original binary is kept at
  `~/.open-to-chat/Ollama-<version>.pristine.bin`.
- The app is ad-hoc re-signed after patching so macOS still launches it.

## Safety

- Pattern-matched: if your Ollama version's internals differ, the tool
  **refuses and changes nothing** (offsets are re-scanned every run, never
  hardcoded).
- Downgrade-proof revert: `--revert` restores the backup only if the
  installed binary is still the exact one the patch produced; if you updated
  Ollama since, it refuses rather than silently downgrade you.
- Read-only status probe (`--check`) never modifies anything.

## After patching

- Open Ollama: it should open on Chat. Fully quit and reopen once to be sure.
- If "launch at login" stops working, toggle it off/on once in Ollama Settings
  (re-signing resets that trust).
- If macOS ever re-blocks the app after an OS update:
  `xattr -dr com.apple.quarantine /Applications/Ollama.app`

## Updating Ollama

Any app update replaces the binary and removes the patch (in-app auto-update
is usually off by default on macOS; if you turned it on, it will). After an
update: run `python3 open-to-chat.py --check` first. Exit code 3 means the
update removed the patch; re-run the patch command (with Ollama quit). Also
check whether a newer Ollama fixed this natively before re-patching, and
please test/report new versions in an issue.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `EPERM` / "Operation not permitted" | macOS blocks writing app bundles. System Settings -> Privacy & Security -> App Management -> add your terminal app. Or run the tool with `sudo`. Quit Ollama first either way. |
| Refused: pattern not found | Your version changed internally. Nothing was modified. Open an issue with your `ollama --version` / app version. |
| macOS says the app is damaged / won't open | `xattr -dr com.apple.quarantine /Applications/Ollama.app`, then open again. |
| Want the old behavior back | `python3 open-to-chat.py --revert` (Ollama quit). |

## The proper fix is upstream

The saved `LastHomeView` setting existing but being ignored at boot looks
like an oversight; routing cold start to the last view (or to `/c/new`) would
be a small change in the app host. If that ships, this repo becomes obsolete
- which is the goal. See [ISSUE_DRAFT.md](ISSUE_DRAFT.md) for the upstream
report.

## License

MIT. This tool modifies your locally installed copy of the Ollama app; it
does not distribute any Ollama code. Not affiliated with Ollama.
