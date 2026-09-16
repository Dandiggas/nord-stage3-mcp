# Nord Stage 3 MCP

Control a Nord Stage 3 from an AI assistant through the Model Context Protocol
(MCP). Inspect sounds, prepare gig layouts, edit supported patch settings and
restore backed-up programs. Changes require explicit approval and read-back checks.

This repository contains the server implementation and regression tests. It does
not contain research scripts, disassembly, personal patches, sample libraries,
recordings, setlists or development-session history.

## Requirements and installation

Tested on macOS with Python 3.13 and a Nord Stage 3. Other models and platforms
have not been verified. Install the native USB library, libusb, for your system
before connecting. The Python dependencies are pinned in requirements.txt.

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python nord_mcp.py
```

The server communicates over standard input/output. Configure your MCP client's
command as the absolute path to `.venv/bin/python`, with the absolute path to
`nord_mcp.py` as its argument. Connect the keyboard by USB and quit Nord Sound
Manager first; only one process can hold the vendor USB connection at a time.
The project directory must be writable for backups and saved plans.

## Capabilities

- Inspect the keyboard's sound inventory and download program backups.
- Remember song-to-program choices, variants, keys and performance notes.
- Prepare ordered gig layouts, medleys and multiple patches per song, with a
  printable gig sheet. Review and approve a saved plan before writing.
- Copy, move, swap and rename programs with verification.
- Edit supported transpose, split, layer, part-level and controller settings.
- Select an installed piano/sample sound from another program, and edit panel
  reverb and compressor settings.
- Check whether a stored program's required sounds are installed.
- Back up program banks and prepare a reviewed restoration plan. Displaced
  programs are kept in empty parking slots; missing originals can be restored
  from disk with their saved categories.
- Play an audition note through USB MIDI.

## Prepare a gig

1. Choose the songs and existing source programs. Call `nord_prepare_setlist`
   with `gig`, `songs`, `start_bank` and `start_slot`.
2. Inspect the returned plan and gig sheet. Resolve any missing or ambiguous
   choices and occupied destinations. Preparation saves checked source backups.
3. Approve the exact rows, then call `nord_apply_setlist` with the saved `plan_id`
   and `confirm: true`. Sources are checked again before writing; names, contents
   and required sounds are verified afterward.

Bank and slot arguments start at zero. Bank 9, slot 0 is keyboard position J:11;
bank 9, slot 11 is J:32. Some tool outputs also use ordinal labels J01 and J12.
A song's musical key is a note for the player, not automatic transposition.
Use `nord_remember_song` to save deliberately chosen sounds for later plans.
Revised plans use `previous_plan_id` and invalidate the earlier unexecuted plan.

## Edit a sound

Use `nord_inspect_layout` to inspect a downloaded program. For a song-specific
copy, use `nord_prepare_song_patch` with the source, an empty destination, a
musical brief and explicit `changes`. Inspect the preview, then approve
`nord_apply_song_patch`. The new program is verified and remembered.

Supported controls include A/B piano, synth and organ enable/level settings,
one middle split with left/right/full assignments, and wheel, aftertouch and
control-pedal level targets. Levels use the keyboard's 0–127 scale. Setting a
controller target equal to the base removes that level assignment. Unsupported
controls are rejected rather than guessed.

Optional `samples` choose a sound reference from a saved donor program; they do
not copy its entire tone or install new sounds. `effects` supports panel reverb
and compressor. Consult the MCP tool schemas for exact arguments and ranges.

## Restore a layout

1. `nord_snapshot_layout(name, banks)` captures selected program banks.
2. `nord_prepare_restore(snapshot_id, parking_banks)` creates a preview showing
   every move, swap and required recovery upload.
3. Review and approve `nord_apply_restore(restore_id, confirm=True)`.

Enough empty parking space must exist to retain displaced programs. Changes
since preparation block application. Snapshots cover program banks, not sample
memory, song memory, live buffers or instrument settings.

## Safety and limits

- Back up your instrument before use. This is an independent project with no
  Nord affiliation or endorsement.
- A `confirm` argument records approval; the calling assistant must obtain that
  approval from the user. It is not an authentication or access-control system.
- Lost replies and interrupted writes require inspection. They are not blindly
  retried. There is no automatic rollback or delete/erase tool.
- New sample installation, broad synthesis editing, and effects beyond reverb
  and compressor are outside the supported preparation workflow.
- Musical suitability still requires listening. A verified file or matching
  loudness measurement does not establish that a patch suits an arrangement.

Backups and plans are saved beside the server in `downloads/`, `gig-plans/`,
`program-edits/`, `song-patch-plans/` and `layout-snapshots/`. Remembered choices
are in `song-library.json`. These local files are excluded from version control.

## Tests and verification

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The automated suite uses a simulated keyboard and does not require USB hardware.
The implementation has also been checked on a real Stage 3: 38 gig copies,
supported sound edits, disk recovery and a complete two-bank restore/return.
Both restore passes verified all 325 programs in that test inventory. The
research environment additionally passed 281 independent patch-format comparisons;
its proprietary fixtures and research tooling are not distributed here.

## Licensing

No project-wide redistribution licence has been granted yet. Third-party
libraries retain their own licences. No Nord sounds or factory programs are
bundled with this implementation.
