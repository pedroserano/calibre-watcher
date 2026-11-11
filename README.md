# calibre-watcher

A small background service that notices when you finish a book in
[Calibre-Web](https://github.com/janeczku/calibre-web) and keeps a queue of those
books, so another device can pick them up later and log them. I use it to make
[Day One](https://dayoneapp.com) journal entries.

It is a single Python file with no dependencies beyond the standard library. It is
meant to run in a Docker container on a home server (TrueNAS in my case).

## How it works

1. Every few minutes (5 by default) the watcher reads Calibre-Web's **Read Books**
   OPDS feed.
2. When a book appears there that it hasn't seen before, it records the **exact time
   it noticed**, downloads the cover, and adds the book to a queue on disk.
3. A client on the same network (an iPhone Shortcut, a Mac script, anything that can
   make HTTP requests) asks for the queue whenever it's running, creates its journal
   entry using the recorded time, and then **acknowledges** the item so the watcher
   removes it.

Because the queue is kept on the server, nothing is lost if the client is off for
days, and entries still get the right date.

**First run:** when there is no saved state yet, the watcher only records the books
already marked as read and doesn't queue them. That way you don't get a flood of
entries for books you read years ago. Only books marked read *after* that point
are queued.

```
Calibre-Web ──(OPDS, every POLL_SECONDS)──▶ calibre-watcher ──(HTTP queue API)──▶ Mac / iPhone
                                            /data/state/queue            GET /queue, GET /cover/<id>, POST /ack/<id>
```

## Requirements

- Calibre-Web with OPDS available (it is by default at `/opds`), and a Calibre-Web
  user whose books you mark as read.
- Somewhere to run a container that can reach Calibre-Web, e.g. TrueNAS SCALE, any
  Docker host, or just `python3` on any machine.
- A persistent folder for state (seen books + queue).

## Configuration

Everything is set with environment variables:

| Variable            | Required | Default            | Meaning |
|---------------------|----------|--------------------|---------|
| `CALIBRE_URL`       | yes      |                    | Calibre-Web address as seen from the container, e.g. `http://192.168.1.50:8083` |
| `CALIBRE_USER`      | usually  |                    | Calibre-Web username (Basic/Digest auth) |
| `CALIBRE_PASSWORD`  | usually  |                    | Calibre-Web password |
| `CALIBRE_READ_PATH` | no       | `/opds/readbooks`  | Path of the Read Books feed. If it 404s, the watcher looks for it in the `/opds` catalog |
| `QUEUE_TOKEN`       | yes      |                    | Shared secret. Clients must send `Authorization: Bearer <QUEUE_TOKEN>`. Use a long random string |
| `POLL_SECONDS`      | no       | `300`              | How often to check Calibre-Web |
| `PORT`              | no       | `8099`             | Port for the queue API |
| `DATA_DIR`          | no       | `/data`            | Where `seen.json` and `queue/` are stored. Mount a persistent volume here |
| `TZ`                | no       |                    | Container time zone, only affects log timestamps. Queue times are always UTC |

Generate a token with, for example:

```bash
openssl rand -hex 32
```

## Install on TrueNAS SCALE (or any Docker Compose host)

The container uses the stock `python:3.12-alpine` image and clones this repo when it
starts, so there is nothing to build.

1. In TrueNAS, create a dataset for it (I use `thor/calibre-watcher`), then in
   **System → Shell** run:

   ```bash
   mkdir /mnt/thor/calibre-watcher/data
   ```

2. Go to **Apps → Discover Apps → ⋮ (next to Custom App) → Install via YAML** and
   paste the following. Fill in your Calibre-Web login and `QUEUE_TOKEN`, and change
   the paths if your pool isn't `thor`:

   ```yaml
   services:
     calibre-watcher:
       image: python:3.12-alpine
       container_name: calibre-watcher
       restart: unless-stopped
       command:
         - sh
         - -c
         - |
           apk add --no-cache git >/dev/null
           if [ -d /data/repo/.git ]; then
             git -C /data/repo pull --ff-only || echo "git pull failed, using the copy already downloaded"
           else
             git clone --depth 1 https://github.com/pedroserano/calibre-watcher.git /data/repo
           fi
           exec python -u /data/repo/calibre_watcher.py
       ports:
         - "8099:8099"          # keep this LAN-only (plain HTTP + token)
       environment:
         CALIBRE_URL: "http://192.168.1.50:8083"
         CALIBRE_USER: "your-calibre-web-user"
         CALIBRE_PASSWORD: "your-calibre-web-password"
         QUEUE_TOKEN: "change-me-to-a-long-random-string"
         POLL_SECONDS: "300"
         TZ: "America/New_York"
         DATA_DIR: "/data/state"   # queue + state; the repo is cloned to /data/repo
       volumes:
         - /mnt/thor/calibre-watcher/data:/data
   ```

3. Start the app and check its logs. You should see something like:

   ```
   watching http://192.168.1.50:8083/opds/readbooks every 300s; queue API on port 8099
   first run: recorded 42 already-read book(s), nothing queued
   ```

**Keep your real passwords and token only in the YAML on the server.** This repo is
public and the script itself contains no secrets.

### Updating

Every time the container starts it runs `git pull`, so to update the watcher just
push to this repo and restart the app in TrueNAS.

### Running without Docker

```bash
CALIBRE_URL=http://192.168.1.50:8083 CALIBRE_USER=me CALIBRE_PASSWORD=secret \
QUEUE_TOKEN=$(openssl rand -hex 32) DATA_DIR=./data \
python3 calibre_watcher.py
```

## Queue API

Every endpoint except `/health` needs the header
`Authorization: Bearer <QUEUE_TOKEN>`.

| Method | Path          | Returns |
|--------|---------------|---------|
| GET    | `/health`     | `{"ok": true}`, no auth needed. Good for checking that it's up |
| GET    | `/queue`      | JSON list of pending books, oldest first |
| GET    | `/cover/<id>` | Cover image bytes for a pending item (404 if it had no cover) |
| POST   | `/ack/<id>`   | Removes the item (and its cover) from the queue. Call this after you've logged it |
| GET    | `/sample`     | One already-read book with its cover as base64, **not** queued. Useful for testing a client |

A queue item looks like this:

```json
{
  "id": "3f2a9c1d0b7e4a55",
  "key": "urn:uuid:…",
  "title": "The Left Hand of Darkness",
  "authors": ["Ursula K. Le Guin"],
  "detected_at": "2026-11-10T21:04:17+00:00",
  "cover_ext": "jpg"
}
```

`detected_at` is when the watcher noticed the book was read (UTC), so the real
finish time is at most `POLL_SECONDS` earlier. `cover_ext` is `jpg`, `png`, `gif`,
`webp`, `heic`, or `null` if there's no cover.

Try it from a computer on your network:

```bash
curl http://SERVER:8099/health
```

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" http://SERVER:8099/queue
```

```bash
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" http://SERVER:8099/ack/3f2a9c1d0b7e4a55
```

### Writing a client

A client only needs to do this whenever it runs:

1. `GET /queue`.
2. For each item: if `cover_ext` isn't null, `GET /cover/<id>`. Create your entry
   (title, authors, cover) dated `detected_at`.
3. Once the entry has been saved, `POST /ack/<id>`. Don't acknowledge before saving,
   or a failure will lose the book. If the client crashes, the item just stays in
   the queue for next time.

## iPhone setup: log books to Day One with Shortcuts

This is the client I use on iOS. It reads the queue, creates a Day One entry for
each book, and acknowledges it. It needs the Day One app installed, and the phone
has to be on your home network to reach the watcher.

### The "Log Books" shortcut

Create a new shortcut in the Shortcuts app named **Log Books**. Replace
`<truenas-ip>` with your server's address and `<QUEUE_TOKEN>` with your token:

1. **Get Contents of URL**: `http://<truenas-ip>:8099/queue`, method **GET**,
   header `Authorization` = `Bearer <QUEUE_TOKEN>`.
2. **Get Dictionary from Input**.
3. **Repeat with Each** on that result. Inside the loop:
   1. **Get Dictionary Value** from *Repeat Item*, once each for `title`,
      `authors`, `id` and `detected_at`.
   2. **Combine Text** on `authors`, with the custom separator ` and ` (with a
      space on each side).
   3. **Get Dates from Input** on `detected_at`.
   4. **Create Entry** (Day One): body
      `Just finished reading [title] by [combined authors]`, tag **Book**, date from
      the previous step, and your journal.
   5. **Get Contents of URL**: `http://<truenas-ip>:8099/ack/[id]`, method
      **POST**, with the same `Authorization` header. This removes the book from
      the queue so it's never logged twice.
4. **End Repeat**.

### Run it automatically

1. In Shortcuts, open the **Automation** tab and tap **+**.
2. Pick a trigger:
   - **Wi-Fi** (when you join your home network). This is the better choice, since
     the phone needs your home network to reach the watcher anyway.
   - **Time of Day**. This only works if you're home at that time.
3. Choose **Run Immediately** if it's offered, turn off **Notify When Run** if you
   want it to be quiet, and select **Log Books**.

### Test it before relying on it

1. Mark a book as read in Calibre-Web.
2. Wait about `POLL_SECONDS` (5 minutes by default) for the watcher to queue it.
   You can check with `curl` against `/queue` (see above).
3. Run **Log Books** by hand and open the new entry in Day One.

Also check:

- **The entry's date.** *Get Dates from Input* has to understand the watcher's
  timestamp format (`2026-11-10T22:30:20+00:00`). If it doesn't, entries get the
  current time instead of when you finished the book.
- **Whether the automation really runs without asking.** Some iOS versions require
  confirmation for certain triggers.

### Covers (not used yet)

Entries are created without the cover for now. Photos attached to Day One entries
created from Shortcuts sometimes don't show up. Some users report a blue square in
the list preview while the full entry has the image, and others report images
missing after syncing to the Mac. Before deciding it doesn't work, open the entry
fully on the iPhone and check again after it syncs.

To add covers once that works, put two steps before **Create Entry**:

1. **Get Contents of URL** on `http://<truenas-ip>:8099/cover/[id]` with the same
   `Authorization` header.
2. Pass the result into Create Entry's **photo** field.

Some items have no cover (`cover_ext` is `null`), and `/cover/[id]` returns 404 for
them. An **If** on `cover_ext` avoids that.

Day One's URL scheme can paste an image from the clipboard, but reports say the
photo often still doesn't appear. It also opens the Day One app every time, so it
isn't suitable for an unattended automation.

## Files on disk

Inside `DATA_DIR`:

- `seen.json`: every book the watcher has seen as read, so it never queues the same
  book twice.
- `queue/<id>.json` and `queue/<id>.img`: pending items and their covers.

**Troubleshooting:**

- To have a book queued again, remove its entry from `seen.json` (and make sure
  it's still marked read in Calibre-Web).
- To start from scratch, stop the app and delete `seen.json`. The next run will be
  a "first run" again, so it records what's already read without queueing anything.

## Security notes

- The API is plain HTTP protected only by the token. **Keep port 8099 on your LAN**
  and don't forward it on your router. Use a VPN (WireGuard, Tailscale) if you need
  to reach it from outside.
- The Calibre-Web password and the token live only in the container's environment,
  never in this repo.
