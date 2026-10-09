# clean

Safe cleanup for big drives:

- **`clean empty`**: finds folders that are truly empty (no files at all,
  counting folders that only hold other empty folders) and lists or deletes
  them.
- **`clean dupes`**: finds duplicates in one folder or across several drives:
  folders that are copies of each other (whole, renamed, partial or diverged),
  folders whose files all exist elsewhere, and byte-identical files. With
  `--media`, also look-alikes: the same photo or song in files whose bytes
  differ. It writes a report and never deletes anything.
- **`clean remove`**: acts on a `dupes` run. The extra copies go to the Trash,
  or are merged or replaced with hard links. Every file is checked again first,
  and the last copy of anything is never removed.
- **`clean undo`** and **`clean trash`**: put back what a removal moved, or see
  and empty the Trash of each drive.
- **`clean space`**: where the space goes. It shows the drives, the biggest
  folders and files, the caches programs keep (with each program's own
  command to clear them), the Trash, clutter and clean's own data. It only
  looks.
- **`clean ui`**: the same in a page in your browser. It runs scans, lets you
  tick what goes, removes it, and shows the space, the history, the Trash and
  the settings. The Files app can open it on a folder from its right-click
  menu.

Every run is saved with its summary, a report page and the full lists, so
`clean history` and `clean show` can bring any of them back later.

Plain Python 3.10+ with no dependencies, for Linux. Made on Ubuntu with
large exFAT USB drives in mind; see [Requirements](#requirements).

## Requirements

| | |
|---|---|
| System | **Linux.** Used on Ubuntu 22.04 (GNOME); the test suite also passes in a fresh Ubuntu 24.04 container. Other current distributions should work: clean needs only the kernel's `/proc` and the freedesktop Trash layout that Linux desktops share. |
| Python | **3.10 or newer** (tested on 3.10, 3.12 and 3.13), standard library only: nothing to `pip install`. Ubuntu 22.04 comes with 3.10 and 24.04 with 3.12. |
| Optional | `xdg-open` (package `xdg-utils`) to open report pages and folders. A web browser for `clean ui`. GNOME Files (Nautilus) for the right-click entries. |

**macOS and Windows are not supported.** clean reads Linux's own mount table
(`/proc/self/mountinfo`), finds a drive again after it is re-plugged by its
filesystem UUID (`/dev/disk/by-uuid`), and moves files into the freedesktop
Trash, not the macOS Trash or the Windows Recycle Bin. Started anywhere else,
it says so and stops. WSL2 is a real Linux, so clean should run there, but
that is untested, and what it removes from a Windows drive (`/mnt/c`, ...)
would go to a `.Trash-1000` folder on that drive, not to the Recycle Bin.

## Install

Get the code (clone this repository, or download it as a ZIP and unpack it),
then in its folder:

```bash
./install.sh                        # links ~/.local/bin/clean -> this folder (no sudo)
./install.sh --nautilus             # ...and adds it to the Files right-click menu
./install.sh uninstall              # removes both; your settings and run history stay
./install.sh uninstall --nautilus   # removes only the Files menu entries
./install.sh test                   # runs the test suite (a few minutes)
./install.sh test --slow            # also the slow tests on big trees
```

The installer won't overwrite a different program called `clean`. It warns if
`~/.local/bin` isn't on your `PATH`. To install somewhere else, use
`BIN_DIR=~/bin ./install.sh`. The link points into this folder, so keep the
folder where it is, or run the installer again after moving it.

**The Files menu** (`--nautilus`). Right-click a folder in Files, then
**Scripts > clean**:

| Entry | Opens the dashboard on |
|---|---|
| Find duplicates here | a new `dupes` scan of the selected folders |
| Find empty folders here | a new `empty` scan |
| Where the space goes | a new `space` look |

Several selected folders all go into the scan. A selected file counts as its
folder, and with nothing selected the open folder is used. Nothing starts
until you press **Start** on the page. If a dashboard is running, that one
opens. Otherwise one starts in a terminal window (closing the window stops
it); if it can't start, the window stays open with the reason. Folders on
network places are refused with a message. The entries are three links in
`~/.local/share/nautilus/scripts/clean/` (it follows `$XDG_DATA_HOME`) to
`nautilus/clean-nautilus`. The installer leaves anything else there alone,
and `uninstall` removes only its own links.

## Empty folders

```bash
clean empty "/media/$USER/Backup Drive/Photos"          # list only (the default)
clean empty "/media/$USER/Backup Drive/Photos" --delete # then delete
clean empty /media/$USER/USB/{old-phone,downloads} --delete   # several at once
clean empty ~/Projects -e node_modules -e 'build*'      # skip some folders
```

Run it without `--delete` first and read the list. The `--delete` run
re-checks every folder, so anything that gained a file in between is left
alone.

**Why no file can be lost:** the only delete call it uses is `rmdir`, and the
operating system refuses `rmdir` on any folder that contains something. A
folder that holds a single file of any kind (a `.DS_Store` or `Thumbs.db`
too), or a symlink, is kept, and so is every folder above it.

**Never deleted:**

| What | Why |
|---|---|
| The folders you name | Only what's *inside* them gets cleaned |
| `.git` `.hg` `.svn` `.bzr` and everything inside | git stops recognising a repo whose `.git/refs` is gone, and after `git gc` that folder holds only empty folders |
| `lost+found`, `.stfolder` | Needed by fsck and Syncthing |
| Folders matching `-e/--exclude` | You asked |
| Mount points (other drives mounted inside) | Not yours to remove. With `-x` it doesn't even go inside them |

It refuses `--delete` on your home folder itself (`~`) or a folder holding it
(`/home`), and on system folders (`/usr`, `/etc`, `/var`, …), because programs
need their empty folders there. `--force` overrides that. It also refuses
drives mounted read-only, and never scans `/`, `/proc`, `/sys` or `/dev`.

If a folder can't be removed (no permission, in use), it is reported once and
its parent folders are left alone without extra errors. If the drive is
unplugged, starts failing, or turns read-only partway through, the run stops at
once with one message. Ctrl-C (or `kill`, or closing the terminal) is safe at
any point: while scanning, nothing has been deleted yet; while deleting, the
run's `folders.tsv` lists exactly what was removed, and `clean show` says so.

## Duplicates

```bash
clean dupes "/media/$USER/Backup Drive"                # within one drive
clean dupes ~/Pictures "/media/$USER/Backup Drive"     # across two places; ~/Pictures is preferred for keeping
clean dupes ~/Pictures --type images --min-size 100K   # photos only, no thumbnails
clean dupes ~/Music /media/$USER/USB/Music --media     # also songs in other formats or bitrates
clean show --open                                      # the last run's report page
```

Two files are duplicates only if their bytes are identical. Names, dates and
locations don't matter (unless `--same-name` / `--same-date` ask for them too).
Files are compared by size, then the first 4 KB, then the full content
(BLAKE2b), so only files that are probably identical get read completely.

Results come in these kinds, largest reclaimable space first:

| Kind | Meaning | Example |
|---|---|---|
| **Folder family** (F1, F2, ...) | Folders that are copies of each other, around one folder to keep (the most complete) | |
| &nbsp;&nbsp;identical | the same names and the same contents | `Backup1/Pictures` and `Backup2/Pictures` |
| &nbsp;&nbsp;renamed | the same files, renamed or in other subfolders | `Phone dump/` and `Photos 2019/` sorted by month |
| &nbsp;&nbsp;partial | every file in it is also in the kept folder | an old backup holding 900 of the 1 000 photos |
| &nbsp;&nbsp;diverged | shares at least `--similar` % (80) of its files, but both have files of their own: keep both. Copies between the two (`B/sub` the same as `A/sub`) are part of the pair, not results of their own | two copies edited separately |
| **Covered folder** (C1, ...) | every file in it exists elsewhere, spread over several folders, mostly in places that are not more of a copy than it is | `Downloads/old/`, whose files are in 3 other folders |
| **File group** (G1, ...) | files with the same bytes | `IMG_0001.jpg` and `20190612_101530.jpg` |
| **Look-alike** (L1, ...) | with `--media`: the same photo or song in files whose bytes differ (see below) | `song.flac` and `song 128.mp3` |

A copied tree is reported once, at the top: `Backup2` identical to `Backup1`,
not again for each subfolder. File groups that a folder result already
accounts for are folded away under it, so only the files the folders don't
explain are listed one by one. For each family member the report shows what
differs from the kept folder: files only here, only there, changed (same path,
other content), and moved or renamed.

**What is never reported as redundant.** A folder is only called a copy if
every file in it would still exist somewhere after *all* the redundant folders
reported were removed, so folders that are copies of each other in a circle
are never all reported. A folder holding something the scan can't vouch for
(a symlink, an unreadable or excluded entry, another drive, a file left out by
a filter) is never a copy as a whole, though its duplicate files still are.

**Which copy is kept.** The most complete folder is kept. Among equal ones,
the first rule that tells them apart decides:

1. The one in the first PATH given.
2. The one inside a folder a bigger family already keeps. When `Pictures` and
   `Backup/Pictures` are copies, and `Phone dump` holds the same photos as
   `Pictures/2019`, then `Pictures/2019` is kept, so the kept `Pictures` isn't
   hollowed out.
3. The one in the more complete surroundings. `Main/a` wins over `Old/a` when
   everything in `Old` is also in `Main`, so `Old` is reported as one partial
   copy rather than piece by piece.
4. The one whose files are least deep. `Photos/` wins over `Backup/Photos/`
   when `Backup` holds nothing else.

**Diverged** is measured on the two folders' files together: the files they
share, out of all the different files in either. Two folders of 10 files that
share 9 have 11 different files between them, so they are 82% alike.

A folder of originals is not called covered just because its files were also
copied into folders that are mostly copies themselves: `Projects/p/a` stays,
even though its two files are also in `Downloads/x` and `Inbox/y`.

**Space** counts each file once: hard-linked names are one file, and a folder
only frees a file's space if all of that file's names are inside it.

Not counted: empty files, symlinks, extra names of hard-linked files, system
junk (`Thumbs.db`, `desktop.ini`, `.DS_Store`, ...: setting `junk.names`; it
doesn't stop two folders from being copies), Trash folders, and anything in
`.git`, `.hg`, `.svn`, `.bzr`, `lost+found`, `.stfolder`. Hidden files are
compared as part of their folder but listed only with `--hidden`.

A file that changes while it is being compared is skipped and reported, and so
is an unreadable file. If the drive is unplugged or starts failing (100 reads
in a row fail), the run stops and is saved as `drive-gone` with no results, so
a list of duplicates never has silent gaps.

**Which files.** All of these narrow what is compared:

| Option | |
|---|---|
| `--type KINDS` | `images`, `audio`, `video`, `documents`, `archives`, `applications`, comma-separated |
| `--ext jpg,png` / `--not-ext EXTS` | only / not these extensions |
| `--min-size SIZE` / `--max-size SIZE` | e.g. `4096`, `500K`, `10M`, `2G` |
| `--newer-than WHEN` / `--older-than WHEN` | a date (`2024-03-01`) or a time ago (`30d`, `6w`, `2y`) |
| `--name GLOB` | only names matching, ignoring case (`'IMG_*'`); repeatable |
| `--max-depth N` | 0 = only the files in PATH, 1 = and its subfolders, ... |
| `--hidden` | also list hidden files and folders |
| `--same-name` / `--same-date` | duplicates must also have the same name / modification time |
| `--media` | also find look-alike photos and songs (below) |
| `--similar PCT` | how alike two folders must be to count as diverged copies: the files they share, out of all the different files in either (default 80) |
| `--no-cache` | read every file, ignoring and not saving cached hashes |

**Look-alikes** (`--media`, setting `dupes.media`). These are the same photo
or song in files that are not byte-identical: an edited or re-saved photo, a
HEIC and its JPEG export, one song ripped twice at different bitrates.

| | The same when |
|---|---|
| Photos: JPEG, PNG, WebP, HEIC/HEIF/AVIF, TIFF and the raw formats (DNG, CR2, NEF, ARW, ...) | the capture time (EXIF), the camera (make and model) and the dimensions, either way round, all match |
| Songs: MP3, FLAC, M4A, Ogg Vorbis, Opus | the artist and title match (ignoring case, spaces and punctuation), and the lengths are within 2 seconds |

Only each file's headers are read (a few KB to a few hundred KB), never the
picture or the sound itself, though on a spinning drive that is still a seek
or two per photo and song. The tags are kept in the hash cache, so a second
scan doesn't read them again. A file whose tags are missing, truncated or
corrupt is simply not matched. Each
set names its versions A, B, C, ..., and marks with `*` the one that looks
best: lossless, then the higher bitrate, for songs; raw, then more pixels,
for photos.

Look-alikes are only reported. **Nothing picks them for removal**: not the
default rules, not `--rank`, and not **Auto-select** in the dashboard. A
version goes only if you tick it in the dashboard, and the last version of a
photo or song never goes: if every version is ticked, the best one stays.
Not matched: a resized or cropped copy (other dimensions), a photo without a
capture time, or a song without artist and title.

**The hash cache.** Each file's hash is saved in `~/.cache/clean/hashes.sqlite`
(it follows `$XDG_CACHE_HOME`), keyed by the drive's UUID and the path on it,
with the file's size and modification time. A second scan of the same files
reads only what changed, even if the drive is now mounted somewhere else.
Entries for files that are gone are dropped after a complete scan, and when
the file grows past `cache.max_mb` (500) the entries used longest ago go too.
The cache is safe to delete at any time; setting `cache.enabled false` turns
it off.

```bash
clean cache               # how big it is, how many files it remembers, on which drives
clean cache prune         # drop entries of files that are gone or changed, then compact it
clean cache clear         # delete it (asks first; -y doesn't)
```

`prune` keeps the entries of drives that aren't connected: their files can't
be checked, and they are still right when the drive comes back. A stale entry
can never cause a wrong result, because an entry is only used while the
file's size and modification time still match, and a removal reads every
file again anyway.

## Removing duplicates

```bash
clean remove -n                        # what removing the newest scan's copies would do
clean remove                           # do it: to the Trash (asks first)
clean remove --keep-in ~/Pictures      # never touch anything in ~/Pictures
clean remove --remove-from /media/$USER/USB/backup  # only remove copies in there
clean remove 3 --rank newest           # act on run 3 of `clean history`; keep the newest copy
clean remove --merge                   # also merge diverged folders into the one that stays
clean dupes ~/Pictures /media/$USER/USB --remove    # scan, then remove straight away
```

`clean remove` acts on a finished `clean dupes` run; by default that is the
newest one. It doesn't scan again. The run can be an hour old or a week old,
because every file is checked again just before it is touched.

**What goes, by default:**

- **Folder families:** every copy except #1 of the report (`identical`,
  `renamed` and `partial` copies).
- **Covered folders:** every file in them exists elsewhere.
- **File groups left over:** every copy but one. `--rank` picks the copy that
  stays: by default it is in the first PATH the scan was given, then the
  oldest, then the one with the shortest path.
- **Diverged folders stay**, because each has files of its own. `--merge`
  changes that (see below).
- **Look-alikes stay**, unless you ticked a version in the dashboard. Even
  then, the last version of a photo or song is never removed.

A dry run (`-n`) lists all of this and saves it as a run. It ends with the
exact command to go ahead:

```
$ clean remove -n
Would move 2 folders and 1 file (13 files in all, 31.8 MB) to the Trash:
  folder  ~/Backup
          7 files, 17.4 MB · same files as ~/Pictures
  folder  ~/Phone dump
          5 files, 11.5 MB · same files as ~/Pictures/2019
  file    ~/Downloads/IMG_100 (1).jpg
          3.0 MB · copy of ~/Pictures/2020/IMG_100.jpg
...
To go ahead: clean remove 2026-10-08_012004-dupes
```

**Choosing what goes:**

| Option | |
|---|---|
| `--keep-in DIR` | A reference folder. Nothing inside it is removed, and its copies are kept first. Repeatable |
| `--remove-from DIR` | Only copies inside DIR are removed. Repeatable |
| `--rank RULES` | Which copy of a file stays. The first rule that decides wins: `first-root`, `oldest`, `newest`, `shortest`, `largest-folder`, `path-contains=TEXT` (default `first-root,oldest,shortest`) |
| `--rank-folders` | Let `--rank` also pick which copy of a folder stays (by default, the most complete) |
| `--only files` / `--only folders` | Act only on single files, or only on folder results |

**Copies inside copies.** A family's folder that would go anyway, because it
sits inside a folder another family removes, is not the one it keeps. Say
`Pictures` and `Backup/Pictures` are copies, and `Phone dump` holds the same
photos as `Pictures/2019`. With `--keep-in Backup`, `Pictures` goes, so the
2019 family keeps `Backup/Pictures/2019` instead, and `Phone dump` goes too.
That folder is found by its files, so it may have another name. If no folder
in the copy that stays has the same files, another copy of the family is kept.
If there is none, the deepest folder there that holds all of them is kept.

**The last copy is never removed.** Before anything is touched, the whole plan
is checked: every content must keep at least one file on disk that nothing
removes. Two names of one hard-linked file count as one file. A pick that
would take the last copy is blocked and listed with the reason, whatever the
rules say. A folder that holds a kept file is never removed whole, and the
removal never removes a folder that is a mount point, or one that holds one
(another drive mounted inside it).

**Checked again, just before each item:**

- **The copy that goes** must still be the same kind of thing, with the same
  size and modification time. It is then read again in full and must hash the
  same as at the scan.
- **A copy that stays** gets the same check, once per content. If it fails,
  the next copy that stays is tried. If none passes, the item is skipped:
  "no verified copy remains".
- **A folder** is listed again. If it holds anything the scan didn't see, it
  is skipped whole (system junk such as `Thumbs.db` doesn't count).
- **Anything gone already** (deleted by hand, or by an earlier run) is
  reported as `gone`. A folder the scan was given that isn't there any more is
  left alone, with a note.

**How things go:**

| | |
|---|---|
| Trash (the default) | Each item goes to the Trash of its own drive, the one the Files app shows: `~/.local/share/Trash` on the drive of your home folder, `.Trash-1000` at the top of other drives. A whole folder takes one rename. Nothing is ever copied to another drive, and the Trash never silently turns into a delete for good. Setting `remove.method` |
| `--permanent` | Delete for good: there is no undo. The checked files are deleted, then the folders, deepest first. A folder in which something new appears stays |
| `--link` | Each copy is replaced with a hard link to the copy that stays. The space is freed and every path still works, but editing one then edits all. Not on FAT or exFAT drives, which can't hold hard links (refused, with the reason) |

**Merging diverged folders** (`--merge`). Each diverged folder is merged into
the copy that stays. Its files that are found nowhere in that folder move in,
at the same place inside it, and then the diverged folder goes to the Trash.
A move never overwrites anything. Across drives, a file is copied to a
temporary name, synced, and checked by hash before it gets its name. When a
name is taken by a different file, `--conflicts` decides:

| `--conflicts` | |
|---|---|
| `both` (default) | Keep both: the one moving in is named `name (from B).ext` |
| `skip` | Leave that file, and so the diverged folder, where it is |
| `newer` | Keep the newer one. The older one goes to the Trash, but only when a checked copy of it stays somewhere else; otherwise both are kept, as with `both`. Refused with `--permanent` |

```
$ clean remove --merge -n
...
  merge   ~/Projects/thesis-draft
          into ~/Projects/thesis  (1 file only there moves in)
```

**Stopping.** Ctrl-C, `kill` or closing the terminal stops the removal between
items. A pulled drive stops it at once. In both cases it is saved as
`interrupted` or `drive-gone`. Every step is written to the run's
`actions.tsv`, and synced to disk, *before* it happens, so even after
`kill -9` or a power cut the log names everything that may have moved.
`clean undo` works from that log, and running `clean remove` again on the same
scan finishes the rest (what went already is `gone`).

**Asking first.** `clean remove` lists what it will do and asks before going
ahead. `-y` skips the question; without a terminal to ask on, `-y` is needed.
It refuses when a folder the scan was given is your whole home folder
(`clean dupes ~`), a folder holding it, or a system folder such as `/usr`.
Scan subfolders such as `~/Pictures` instead, or add `--force`. It also
refuses on drives mounted read-only.

At the end it says how much space is freed once the Trash is emptied, and the
commands to undo the removal or empty it for good:

```
Mode:            TRASH: `clean undo` puts everything back
Items:           3: 3 removed
Files:           13 files (31.8 MB)
Space freed:     31.8 MB once the Trash is emptied
                 (in ~/.local/share/Trash)
Undo:            clean undo 2026-10-08_012004-remove-2
Free the space:  clean trash empty --run 2026-10-08_012004-remove-2
```

An item ends as `removed`, `linked`, `merged`, `gone` (not there any more),
`skipped` (changed since the scan, or no checked copy stays: the reason is
given), `failed`, `partial` (part of a folder or merge was done), or `not-done`
(the run stopped first).

## Undo and the Trash

```bash
clean undo -n                 # what undoing the newest removal would put back
clean undo                    # put it back (asks first)
clean undo 2                  # undo run 2 of `clean history`
clean trash                   # what is in the Trash of each drive
clean trash list -v           # ...item by item, with the removal that put it there
clean trash restore ~/Pictures/x.jpg            # put that back where it was
clean trash empty --run 2     # delete for good what removal run 2 moved there
clean trash empty --older-than 30               # what has been there for 30 days or more
clean trash empty --drive /media/$USER/USB      # only that drive's Trash
```

`clean undo` reads the removal's `actions.tsv` backwards and puts each thing
back exactly where it was:

- **From the Trash**, an item comes back if its `.trashinfo` still says it came
  from that path during that run.
- **A merge is undone file by file.** The files moved in move back out, folders
  the merge made are removed if they are empty, and the diverged folder comes
  back from the Trash. Copies made on another drive are deleted only once the
  original is back and both still hash the same.
- **A hard link** makes way for the file it replaced, which comes back from
  the Trash. If that fails, the link is put back.

**Nothing is ever overwritten.** If something new is at a path, that item
stays in the Trash and is listed as a `conflict`. Undo can run again: what is
back already is `already`. An item no longer in the Trash is `missing`, and
one on a drive that isn't connected is `offline`. A removal made with
`--permanent` can't be undone. Undo also stops on Ctrl-C or a pulled drive,
and running it again finishes the rest.

`clean trash` covers the Trash of every drive that is connected, including
items other programs put there. `list` tags the items a `clean remove` run
moved with that run. `restore` never overwrites. `empty` deletes for good, so
it asks first. `--run`, `--older-than` and `--drive` limit it to part of the
Trash. After `clean trash empty --run N`, that removal can no longer be
undone.

## Space

```bash
clean space                       # your home folder
clean space ~ /media/$USER/USB    # your home folder and that drive
clean space ~/Projects --top 40   # the 40 biggest folders and files there
clean show --open                 # the report page: every big folder, to open one by one
```

One read-only walk of each PATH shows:

- **Drives**: size, used and free for each one connected.
- **The biggest folders and files.** A size is what the files take on the
  disk, and a file with several names (hard links) counts once. The terminal
  shows the top 15 (`--top`). The report keeps every folder of 1 MB or more
  (up to 50 000), as a tree you can open folder by folder.
- **Caches** that programs keep: conda, pip, uv, npm, Yarn, pnpm, Go, Hugging
  Face, the browsers, thumbnails and the editors, plus the rest of
  `~/.cache`. Each comes with the program's own command to clear it (`conda
  clean --all`, `pip cache purge`, ...), or what to do where there is none.
  For conda, files that an environment also uses are shown apart, because
  clearing them frees nothing. When a PATH is on the system disk, the
  system's own caches are measured too: the journal, apt's downloads and old
  snap versions. Their commands need sudo, so they are only printed
  (`space.system_caches false` turns this off).
- **The Trash** of every drive, with a warning past `trash.warn_gb`.
- **Clutter**: zero-byte files, broken symlinks, temp leftovers (`*.tmp`,
  `~$*`, `.~lock.*`) and empty folders. Clutter inside program and package
  folders (`.git`, `node_modules`, environments, dot folders) is counted but
  not listed, because those programs need their files as they are. A temp
  file changed in the last day is marked as possibly in use.
- **Duplicates**: the newest `clean dupes` run of these folders, or the
  command for one.
- **clean's own data**: the runs and what retention will prune, and the hash
  cache with `clean cache prune` / `clear`.

**It deletes nothing.** The only things clean ever clears are the Trash
(`clean trash empty`) and its own data (`clean history --prune`, `clean
cache prune` / `clear`), each as its own command. clean never clears a
program's cache: only that program knows what in it is still in use.

Each PATH is walked without leaving its drive. A drive mounted inside it is
listed but not entered; name it as a PATH of its own to include it (`clean
space ~ /media/$USER/USB`). The run is saved like any other, with
`summary.txt`, `report.html` and the whole tree in `data.json.gz`.

## The dashboard (`clean ui`)

```bash
clean ui                  # start it and open it in the browser
clean ui --no-browser     # only print the link
clean ui --port 8765      # on a fixed port (default: a free one; setting ui.port)
clean ui ~/Pictures       # open on a new duplicates scan of ~/Pictures
clean ui --scan space /media/$USER/USB   # ...or a space look at that drive
```

With folders (or `--scan dupes|empty|space`), it opens on the **New scan**
page with those filled in; the scan waits for you to press **Start**. If a
dashboard is already running, `clean ui` opens that one rather than starting
another. `--if-running` only does that: with no dashboard running it says so
and exits with 1 (the Files menu uses this).

The dashboard is a page served from your own computer. Every scan, removal
or undo it starts is a `clean` command run for you, with the same checks, and
every page shows that command under **Same in a terminal**. It has:

- **Home**: each disk with its free space, the last scan's results waiting to
  be reviewed, the Trash, and the recent runs.
- **New scan**: pick folders with the folder browser (or type a path) and
  set the same options as on the command line. The page then follows the
  scan as it runs, with a Cancel button that works like Ctrl-C.
- **Results** of a `dupes` run: folder families, covered folders, file
  groups and (with `--media`) look-alikes, with search, filters and sorting.
  Each copy has a box to tick, and each folder shows what differs from the
  kept one. You can also preview a file, open a folder, or ignore a folder in
  every later scan. **Auto-select** applies one rule to all the results: keep
  the newest, keep the oldest, one copy per group, only some extensions,
  merge diverged folders, invert, or select nothing. It never ticks a
  look-alike: those go only one tick at a time. Your ticks are saved as you
  go (`selection.json` in the run's folder), and nothing happens until you
  press **Remove**.
- **Remove**: first shows what goes, from which drive, and what can't go and
  why. Then **Move to the Trash** does it. **Delete for good** asks you to type
  `delete` first.
- **Cleanup completed**: what was moved and what was skipped. It has
  **Select more**, **Scan again**, **View report**, **Undo**, and **Empty
  these from the Trash** buttons.
- **Space**: the drives, and the newest `clean space` look: the biggest
  folders as a tree to open, the caches, clutter and the newest duplicates
  scan of those folders. Below that is what clean can clear itself: the
  Trash of each drive, and its own runs and hash cache (prune or delete them
  from here). A space run's own page opens any folder of the tree, with a
  path back up.
- **History**: every run, with search and filters. You can open, delete or
  prune runs.
- **Trash**: what is in the Trash on each drive. Restore items, or empty
  those older than N days.
- **Settings**: every setting, with its default and where its value comes
  from. You can edit or reset each one. It also shows where the data is kept
  and how big it is.

`clean remove` uses the ticks made in the dashboard. Pass `--no-choices` to go
by its own rules instead.

**Safety.** The dashboard only listens on `127.0.0.1`, so it can't be reached
from other machines. It only opens through the link `clean ui` prints. That
link works once, within 10 minutes, and signs that browser in. Run `clean ui`
again for a new link to the same dashboard. The dashboard also turns away
requests from other web pages: it checks each request's `Host` and `Origin`
headers.

Any step that deletes or moves something needs a confirmation from its own
dialog, and that confirmation works only once. Previews are fetched by a
result's number, never by a path. A file or folder name is always shown as
text, so a name like `<script>` can't run anything. The page loads nothing
from the internet.

Ctrl-C stops the dashboard. If a job is running, the dashboard stops once
that job ends. A second Ctrl-C cancels the job, and what was done so far is
saved.

## Scan options (`empty` and `dupes`)

| Option | Meaning |
|---|---|
| `-e, --exclude PATTERN` | Skip matching files/folders. `name`, glob (`'*.tmp'`), or a full path (`'/media/x/Keep*'`). Repeatable. |
| `--no-default-excludes` | Also go into `.git`, `.hg`, `.svn`, `.bzr`, `lost+found`, `.stfolder` |
| `-x, --one-file-system` | Don't enter other drives mounted inside PATH |
| `-j, --jobs N` | Parallel workers. Keep 1 on USB hard drives (see below) |
| `-q, --quiet` | Summary only: no progress line, no per-item list |
| `empty --force` | Allow `--delete` in `~` itself or in system folders |
| `dupes --type`, `--min-size`, ... | which files `dupes` compares (see Duplicates) |
| `dupes --remove` | After the scan, remove the extra copies as `clean remove` would. It takes the same options (`-n`, `--keep-in`, `--merge`, `--permanent`, `-y`, ...) and saves a `dupes` run and a `remove` run |

`clean --help` and `clean COMMAND --help` show all of this in the terminal,
with the defaults your settings give. `clean --version` prints the version.
`python3 -B -m cleanup` (from this folder) works too.

## Settings

Defaults you'd otherwise type every time go into settings:

```bash
clean config                                   # every setting: value, where it comes from, meaning
clean config set dupes.min_size 1M             # from now on, ignore files under 1 MB
clean config add scan.exclude node_modules '*.tmp'
clean config remove scan.exclude '*.tmp'
clean config get scan.exclude                  # the value alone, one item per line
clean config reset dupes.min_size              # back to the built-in default (or: reset --all)
clean config edit                              # open the file in $VISUAL / $EDITOR
clean config path                              # where the settings file and the data folder are
```

| Setting | Default | Meaning |
|---|---|---|
| `empty.jobs` | 1 | `-j` for `clean empty` |
| `dupes.jobs` | 1 | `-j` for `clean dupes` |
| `dupes.min_size` | 0 | `--min-size` for `clean dupes` |
| `dupes.max_size` | 0 | `--max-size` for `clean dupes` (0: no limit) |
| `dupes.types` | (none) | `--type` for `clean dupes` (none: every kind) |
| `dupes.hidden` | false | `--hidden` for `clean dupes` |
| `dupes.same_name` | false | `--same-name` for `clean dupes` |
| `dupes.same_date` | false | `--same-date` for `clean dupes` |
| `dupes.media` | false | `--media` for `clean dupes`: also look-alike photos and songs |
| `dupes.similar_pct` | 80 | `--similar` for `clean dupes` |
| `remove.method` | trash | `trash`: removed copies go to the Trash (`clean undo` brings them back). `permanent`: deleted outright (`--permanent` for one run) |
| `remove.rank` | `first-root`, `oldest`, `shortest` | `--rank` for `clean remove`: which copy of a file stays |
| `junk.names` | `Thumbs.db`, ... | Files ignored everywhere: system clutter |
| `cache.enabled` | true | Keep the hash cache (`--no-cache` for one run) |
| `cache.max_mb` | 500 | Past this size, the cache entries used longest ago are dropped (0: no limit) |
| `trash.warn_gb` | 10 | `clean space`, `clean trash` and the dashboard warn when a drive's Trash holds more (0: never) |
| `space.system_caches` | true | `clean space` also measures the system's own caches when a folder is on the system disk |
| `scan.exclude` | (none) | Patterns every scan skips, on top of `-e` |
| `scan.default_excludes` | true | Skip `.git`, `.hg`, `.svn`, `.bzr`, `lost+found`, `.stfolder` (`--no-default-excludes` / `--default-excludes` for one run) |
| `scan.one_file_system` | false | Behave as if `-x` was given (`--no-one-file-system` for one run) |
| `history.keep_days` | 90 | Runs older than this are removed, except the newest `history.keep_runs`. 0 keeps every run |
| `history.keep_runs` | 30 | The newest this many runs always stay, however old. 0: no minimum |
| `paths.data` | (default) | Folder for the run history instead of `~/.local/share/clean` |
| `ui.port` | 0 | Port `clean ui` listens on, on 127.0.0.1 only (0: a free one each time) |
| `ui.open_browser` | true | `clean ui` opens the browser (`--no-browser` for one run) |
| `ui.preview_max_mb` | 200 | The dashboard previews files up to this size; bigger ones show only their details |
| `ui.theme` | auto | The dashboard's colours: `auto` (follow the system), `light` or `dark` |

Where a value comes from, highest priority first:

1. an option on the command line, for that run;
2. an environment variable named after the setting, e.g.
   `CLEAN_DUPES_MIN_SIZE=1M clean dupes ...` (list settings take
   comma-separated items: `CLEAN_SCAN_EXCLUDE=node_modules,build`);
3. the settings file, `~/.config/clean/settings.json` (it follows
   `$XDG_CONFIG_HOME`; `$CLEAN_CONFIG_DIR` overrides the folder);
4. the built-in default.

The file is plain JSON (`{"dupes.min_size": 1048576}`) and is replaced in one
step when `clean config` changes it, so it is never half-written. A bad value,
an unknown name, a bad `CLEAN_*` variable or a broken file never stops a
command: it prints a warning and uses the default. `clean config set` refuses
to touch a file it can't read rather than overwrite it.

## Runs and history

Each run of `empty`, `dupes`, `remove`, `undo`, `trash empty` and `space` is saved in
its own folder under `~/.local/share/clean/runs/`.
That folder follows `$XDG_DATA_HOME`, and the `paths.data` setting moves it.
Each run folder is named after when the run started, e.g.
`2026-10-07_163356-empty`:

| File | Contents |
|---|---|
| `summary.txt` | The summary printed at the end, with the full list |
| `report.html` | The same as a page. For `empty`: each outermost folder once with "+N inside", what wasn't deleted and why, problems. For `dupes`: the families, covered folders, file groups and look-alikes in tabs, with search, filters (type, space, location) and sorting. For `space`: the folder tree to open folder by folder, the caches and clutter. It opens from the file and uses no network; only the `dupes` page runs a script, its own |
| `folders.tsv` | (`empty`) Every folder and what happened to it. A `--delete` run writes each row as it happens, so a stopped run leaves an exact record (after `kill -9`, at most the one folder being removed at that instant is missing). (`dupes`) Every folder result |
| `files.tsv` | (`dupes`) Every duplicate file, by group |
| `actions.tsv` | (`remove`, `undo`, `trash empty`) Every step, written and synced to disk before it happens; this is what `clean undo` reads |
| `data.json.gz` | The full results, for programs |
| `manifest.json.gz` | (`dupes`) What the scan saw, so `clean remove` can act later without scanning again, and check first that nothing changed |
| `selection.json` | (`dupes`) What you ticked in the dashboard; `clean remove` uses it |
| `run.json` | Command, folders (with their drive's UUID), status, times, totals |

```bash
clean history              # the newest 20 runs, 1 = newest (--all: every run)
clean show                 # the last run's summary again
clean show 3 --open        # run 3's report page in the browser
clean history --open       # every run as one page (index.html in the data folder)
ls "$(clean show --path)"  # the last run's folder
```

`clean show RUN` takes `last` (the default), a number from `clean history`,
the start of a run's name that no other run shares (`2026-10-07_1633`), or a
run folder's path.

A run's status is `done`, `interrupted` (Ctrl-C, `kill`, terminal closed),
`drive-gone` (the drive went away or started failing), `failed` (an unexpected
error, saved with the run), or `running`. A run that never got to say how it
ended (`kill -9`, a crash, a power cut) shows as `unfinished`; whatever it
recorded up to then is still in its folder.

**Retention.** After every run, runs older than `history.keep_days` (90 days)
are removed, but the newest `history.keep_runs` (30) always stay;
`history.keep_days 0` keeps everything. A removal run is never pruned while
anything it moved is still in the Trash, because it is that removal's undo.
`clean history --prune --dry-run` shows what would go. Deleting a run folder by
hand is fine too: the history page is rebuilt from the folders.

**One delete at a time.** While `clean empty --delete`, `clean remove`,
`clean undo`, `clean trash restore` or `clean trash empty` is running, a second
one is refused, with a message naming the first. Dry runs, `dupes` scans,
`history` and `trash list` are never blocked.

**The lists.** `folders.tsv` has the columns `status  depth  path  note`:

| Status | Meaning |
|---|---|
| `would-delete` | Dry run: would be deleted |
| `deleted` | Removed |
| `gone` | Already removed by something else |
| `failed` | Could not be removed (the note says why) |
| `kept` | Not tried: something inside it could not be removed |
| `not-done` | Not tried: the run stopped first |
| `error` | Could not be read while scanning (the note says why) |

```bash
grep -hP '^deleted\t' ~/.local/share/clean/runs/*-empty/folders.tsv | cut -f3   # every deletion kept in the history
```

A `dupes` run's `folders.tsv` has the columns
`result  role  files  size_bytes  freed_bytes  path`: one row per folder, with
the result's id (`F3`, `C1`), the role (`keep`, `identical`, `renamed`,
`partial`, `diverged`, `covered`) and the space removing that folder frees.
`files.tsv` has the columns `group  size_bytes  hash  explained_by  path`,
with one row per file; rows with the same group number are copies of each
other, and `explained_by` names the folder result that file is in, if any.

```bash
awk -F'\t' '$2=="identical" || $2=="renamed"' "$(clean show --path)/folders.tsv"   # whole redundant copies
```

`actions.tsv` has the columns `step  when  action  path  to  note`, with one
row per change:

| Action | |
|---|---|
| `trash` | `path` went to the Trash, as `to` |
| `delete` | `path` was deleted for good (`--permanent`) |
| `move` | a merge moved `path` to `to` (same drive) |
| `copy` | a merge copied `path` to `to` on another drive, checked by hash |
| `mkdir` | a merge made the folder `path` |
| `link` | `path` is now a hard link to `to`; the copy it replaced went to the Trash |
| `replace` | `path` was replaced by a hard link to `to` for good (`--permanent --link`) |
| `restore` | (`undo`) `path` came back from the Trash |
| `unlink` | (`undo`) the hard link at `path` made way for the file it replaced |
| `empty` | (`trash empty`) `path` was deleted from the Trash for good |

An undo writes `move` and `delete` rows too, for a merge's files moving back
out and its copies on another drive being deleted. A step that is tried
under another name, because the first was taken, gets a row for each try.

In all of these, tabs, newlines and other control characters in a name are
written as `\t`, `\n`, `\xNN`, so one path is always one row. Bytes that
aren't valid UTF-8 appear as `\xNN`.

## Exit status

| Code | Meaning |
|---|---|
| 0 | Done |
| 1 | Some folders/files couldn't be read or removed, a removal skipped something that changed since the scan, or an undo or `clean trash` couldn't put back or delete something (all listed in the summary); or the drive went away mid-run. `clean ui --if-running`: no dashboard is running |
| 2 | Bad arguments, or refused (see the message): e.g. no terminal to ask on without `-y`, or another delete already running |
| 130 | Interrupted (Ctrl-C, `kill`, or the terminal was closed) |
| 141 | Its output pipe closed before it finished writing (e.g. piped into `head`) |

## Speed

The default of 1 worker is deliberate. On a spinning USB drive the work is
limited by disk seeks: on a 5 TB USB hard drive, `-j8` was 3–5× *slower* than
`-j1` because the head jumps between the workers' folders. `-j4` or more only
helps on SSD/NVMe. `dupes` reads files in path order and tells the kernel not
to keep big files in RAM after hashing them, so a 300 GB scan doesn't push
everything else out of memory.

A progress line appears on the terminal (stderr) during long scans. It is
never written into pipes or files.

## Files

| File | |
|---|---|
| `clean` | Command-line entry point (runs the `cleanup` package) |
| `cleanup/cli.py` | Options, signals, dispatch to the commands |
| `cleanup/commands/` | One module per command: `empty`, `dupes`, `remove`, `undo`, `trash`, `space`, `cache`, `history`, `show`, `config`, `ui` |
| `cleanup/runs.py` | Run folders: status, history, retention, the delete lock, `index.html` |
| `cleanup/report/` | Summaries (`text.py`), report pages (`html.py`), `data.json.gz` (`data.py`), removal and undo reports (`removal.py`, `restore.py`), space reports (`space.py`) |
| `cleanup/empty.py` | Empty-folder engine: walk, then `rmdir` bottom-up |
| `cleanup/space.py` | Space engine: one read-only walk that sizes every folder (each file once, however many names it has), keeps the biggest, and notes clutter |
| `cleanup/caches.py` | The caches `clean space` knows, their commands, and the system's own (logs, apt/dnf/pacman/zypper downloads, old snaps) |
| `cleanup/scan/` | The scan: folder tree (`tree.py`), filters and junk (`filters.py`), hashing (`hashing.py`), the hash cache (`cache.py`) |
| `cleanup/dupes/files.py` | Duplicate files: size → first 4 KB → full hash |
| `cleanup/dupes/folders.py` | Folder families, covered folders, file groups folded into them |
| `cleanup/dupes/diff.py`, `results.py`, `manifest.py` | What differs from the kept folder; the results as saved; what the scan saw, for `clean remove` |
| `cleanup/dupes/media.py`, `lookalikes.py` | Photo and song tags (EXIF, ID3, FLAC, MP4, Ogg), read with bounded reads; finding look-alikes with them |
| `cleanup/selection.py` | What a removal takes: the rules, and the proof that the last copy of anything stays |
| `cleanup/choices.py` | The dashboard's ticks (`selection.json`) and how they become rules |
| `cleanup/web/` | The dashboard: the server and its sign-in (`server.py`), the API (`api.py`), jobs run as `clean` commands (`jobs.py`), the pages (`pages.py`, `view.py`), and `static/` (`app.js`, `app.css`) |
| `cleanup/actions/` | Checking again before acting (`verify.py`), the freedesktop Trash (`trash.py`), removing, merging and hard-linking with the `actions.tsv` log (`remove.py`), undo (`undo.py`) |
| `cleanup/settings.py`, `options.py` | Settings (file, environment, defaults) and the options they feed |
| `cleanup/paths.py` | Where settings and data live |
| `cleanup/volumes.py` | Drives: which filesystem a path is on, found again by UUID after a re-mount |
| `cleanup/safety.py` | Delete guards, drive-loss detection, Ctrl-C handling |
| `cleanup/errors.py` | Errors shared by the commands |
| `cleanup/fsutil.py` | Excludes, sizes, progress, safe printing |
| `nautilus/clean-nautilus` | The Files right-click entries (`install.sh --nautilus` links it in under three names) |
| `install.sh` | Install / uninstall (`--nautilus` too) / test |
| `tests/` | End-to-end tests on real temp folders, by area (`./install.sh test`). `tests/ui/` drives the dashboard and the report pages in headless Chrome, if node 22+ and Chrome are installed |
| `LICENSE` | MIT |

## Development

```bash
./install.sh test          # the whole suite, a few minutes; or:
python3 -m unittest discover -s tests -t tests
CLEAN_SLOW_TESTS=1 ./install.sh test   # also the slow tests on big trees
```

The tests build real folders in a temporary directory and never touch your
settings, data, drives or Trash. Some need more than Python, and are skipped
(with the reason) where it is missing:

- **`unshare -rm`** (util-linux, with unprivileged user namespaces): the
  removal, Trash, undo and drive tests run in a private mount namespace, with
  every real drive and the real Trash hidden. Some systems turn these
  namespaces off (Ubuntu 24.04's AppArmor setting
  `kernel.apparmor_restrict_unprivileged_userns`, or a container).
- **`gio`** and **`dbus-run-session`**: checks that the Files app's Trash
  sees and restores what clean put there.
- **node 22+** and **Google Chrome**: `tests/ui/` drives the dashboard and the
  report pages in headless Chrome and fails on any request that leaves the
  machine.

The code is the standard library only, and so are the tests.

## License

MIT; see [LICENSE](LICENSE). Copyright (c) 2026 Aksh Shah.

The report pages and the dashboard embed two fonts, Clean Sans and Clean
Mono (`cleanup/report/fonts/`): Latin subsets of IBM Plex Sans and IBM Plex
Mono, renamed as their license requires. They are under the SIL Open Font
License 1.1, Copyright 2017-2019 IBM Corp. ([OFL.txt](cleanup/report/fonts/OFL.txt)).
