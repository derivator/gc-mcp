# gc-mcp TODO

- [x] add tools to read/write memory using python struct formatting strings
  - `gc_memory_read_struct` / `gc_memory_write_struct` (big-endian `>...` formats)

# if possible to remote control dolphin to do it:

- [x] pause/unpause game + step single frame
  - `gc_emulator_toggle_pause` (F10, Dolphin default)
  - `gc_emulator_step_frame` (Frame Advance, bound to F8 in the generated
    `Config/Hotkeys.ini`; see `dolphin_hotkey_set`)
  - `gc_emulator_freeze` / `gc_emulator_unfreeze` (SIGSTOP/SIGCONT fallback)
- [x] save/load state
  - `gc_emulator_save_state` (Shift+F1..F8) / `gc_emulator_load_state` (F1..F8)

Notes:
- Emulator hotkeys are driven by injecting keystrokes into the instance's X
  display with `xdotool` (the `KeyboardInjector` in `src/gc_mcp/keyboard.py`).
  Requires `xdotool` installed and the `xvfb` capture backend. Dolphin's
  `[Input] BackgroundInput = True` keeps the input gate open for the headless
  `--batch` render window.
- The config builder writes a *complete* `Config/Hotkeys.ini` (Dolphin loads it
  all-or-nothing, so a partial file would un-bind the defaults). It includes the
  stock defaults plus a `Frame Advance/Frame Advance = F8` binding.
- `dolphin_hotkey_get` / `dolphin_hotkey_set` read/write individual bindings in
  `Config/Hotkeys.ini` (e.g. to re-bind Frame Advance).

# discuss with the user:

- [x] should the MCP tell the model about best practices? Is there an intended mechanism for that?:
  - [x] button presses have latency, use the sequence tool to press multiple buttons in a row
  - [x] analyzing screen captures is a slow and expensive process, prefer doing multiple inputs (e.g. menu selections) blind before checking screen again
