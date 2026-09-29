# How it works (root cause analysis)

Static analysis of Ollama.app 0.34.2, done entirely without screenshots:
strings, embedded-bundle carving, and Mach-O structure parsing.

## The app's shape

Despite the `com.electron.ollama` bundle id, the app is NOT Electron. It is a
single Go binary (`Contents/MacOS/Ollama`, a FAT/universal Mach-O with arm64
and x86_64 slices) that embeds the whole web UI (`app/dist/assets/index-*.js`,
React + TanStack Router) and renders it in a WKWebView (`webview/webview_go`,
`cocoa_wkwebview_engine` symbols). The Chromium-style folders in
`~/Library/Application Support/Ollama/` are leftovers from an older
Electron-based build.

## Route table (carved from the embedded JS bundle)

| Route         | Behavior on load                                                          |
|---------------|---------------------------------------------------------------------------|
| `/`           | redirect to `/c/new` (Chat) if `OnboardingVersion >= 1`, else `/onboarding` |
| `/onboarding` | redirect to `/c/new` if `OnboardingVersion >= 1`                           |
| `/connect`    | **renders the Apps page** (title "Apps", sidebar current "apps"), no gate  |
| `/c/$chatId`  | renders Chat; on every visit writes `LastHomeView="chat"` into settings    |
| `/settings`   | Settings                                                                   |

Minified snippets, verbatim from the binary:

```js
// "/" route: chat is the intended destination, gated only by onboarding
const QG = vf("/")({ beforeLoad: async ({ context: e }) => {
  if ((await e.queryClient.fetchQuery({ queryKey: ["settings"], ... }))
        .settings.OnboardingVersion < Nm) throw om({ to: "/onboarding" });
  const n = SA();  // returns "new"
  throw om({ to: "/c/$chatId", params: { chatId: n }, mask: { to: "/" } });
}});

// the chat route persists the last view on every visit
E.useEffect(() => { t && t.LastHomeView !== "chat" &&
  n({ LastHomeView: "chat" }).catch(() => {}) }, [e, t, n])

// the Apps page lives at /connect and has no gate
function YG() { return x.jsx(vl, { title: "Apps",
  sidebar: x.jsx(XC, { current: "apps" }), children: x.jsx(PG, {}) }) }
```

(`Nm = 1`, and the shipped settings row has `OnboardingVersion = 1`, so the
gates pass and every JS-side entry point lands on Chat.)

## The actual bug

The Go host navigates the fresh webview straight to `/connect` on cold start.
Evidence:

1. The Go string tables hold `/connect` and `/c/new` as window navigation
   targets, right next to the app menu strings ("Open Ollama", "File >
   New Chat", and SF Symbol icon names).
2. Injected Go-side JS in the binary checks
   `window.location.pathname === '/connect'`.
3. `/connect` is the only route that renders Apps without a gate. If the boot
   URL were anything else (`/`, `/onboarding`), the router itself would land
   on Chat.

The schema even defines the setting the boot code ignores:

```sql
last_home_view TEXT NOT NULL DEFAULT 'chat'
```

The UI dutifully saves `LastHomeView="chat"` on every chat visit - which is
why closing only the window (warm reopen, no navigation) reopens on Chat -
but cold start hardcodes `/connect` and never reads it. The 8-byte patch
changes that one navigation string:

```
/connect  ->  /c/new#/
```

The trailing `#/` keeps the length identical (Go strings are pointer+length);
the router ignores the fragment, so the webview loads `/c/new` = Chat. The
string appears once per CPU slice in the universal binary; both are patched.

## What was ruled out

Earlier community attempts (and why they could not work): `onboarding_version`
values (the router uses it only as an onboarding gate; both 0 and 99 still
leave the Go-side boot URL at `/connect`), `last_home_view` (written by the
UI, never read at boot), `config.json` keys, hosts blocks and proxy tricks
(the boot route is local logic, not network-driven).

## Verification method

- Byte-level: patch offsets re-scanned from NUL-separated nav-table anchors;
  pristine backup kept; revert guarded by per-slice segment comparison
  (immune to re-signing, sensitive to any rebuild).
- Behavioral, no screenshots: after patching, a fresh cold start fires the
  Chat page's API signature (`/api/v1/chats` + `/api/v1/settings`) and none
  of the `/api/v1/integrations` calls the Apps/connect page makes.
- Final visual confirmation is left to the user, on purpose.
