# Troubleshooting

Written for humans AND for AI assistants. If you're an AI helping someone debug backtalk: read this whole file first, then `logs/backtalk.log`; every load-bearing event (what was heard, what was said, interrupts, engine fallbacks, session rebuilds) is in there. Diagnose from the log, not from guesses.

## Quick fixes

- **Nothing happens when I hold the key (macOS)**: the terminal app needs **Input Monitoring** permission: System Settings → Privacy & Security → Input Monitoring → add your terminal (Terminal, iTerm, etc.), then restart the terminal. The mic prompt is separate and appears on first recording.
- **Mic permission never appeared / recording is silent**: launch from a normal terminal window, not a background service or launcher daemon: the process inherits the *terminal's* microphone permission. Check the input device: `python -m sounddevice` lists them.
- **It hears me but answers slowly**: check `model` in `backtalk.json`. Full-size deep-work models make every reply noticeably slower; the fast tier is the point of a voice loop. Also confirm the model id is the FULL id, never a bare alias; aliases can silently resolve to an older model through the SDK's bundled CLI.
- **First reply after launch is slow**: that's the one-time prompt-cache toll, mostly hidden behind the greeting. Warm turns are the real speed.
- **The voice sounds robotic**: you're hearing Kokoro's base register, or the wrong voice for the language. Try `bm_george`, `bm_daniel`, `am_michael`, `af_heart`. Remember the first letter must match the language pipeline (`b…` British, `a…` American).
- **`espeak` errors when the voice loads**: the system `espeak-ng` package is missing (the pip-bundled build inside the voice engine is broken (known upstream); the system package is the supported path). `brew install espeak-ng` / `sudo apt install espeak-ng`, then re-run.
- **Choppy or slow-motion audio on a weak machine**: lower `stt_model` to `base.en` or `tiny.en`. The playback side already buffers 0.75s ahead specifically so slow machines don't garble.
- **Two voices answering at once**: two copies are running. `./run.sh` kills the previous instance on launch; if you started one some other way, kill it. One body, one mouth.
- **Spotify stays quiet after it stops talking**: the restore is debounced ~0.5s; if the process was force-killed mid-speech the restore can be lost. It self-corrects on the next duck, or nudge the volume by hand.
- **ElevenLabs sounds worse than their website**: their site previews are mastered demo clips; the raw API never matches them. The shipped `master` ffmpeg chain closes the gap; make sure `ffmpeg` is installed, and don't set the style parameter or switch to the multilingual model for English (both make delivery slow and dull).
- **It started asking permission out loud after an update**: that is the new default (safe by default, hands-free by choice). Say "go hands free" in a voice session and confirm for an immediate, saved flip; or tell your agent to set `"permission_mode": "bypassPermissions"`, which takes effect the next time the voice line starts. The agent writes the config, never you.
- **It asked permission, then said "no answer, so I didn't do it"**: the spoken ask waits about 75 seconds, then treats silence as no. Hold the key and answer with an exact "yes" (or "go ahead", "approved") to approve; anything else denies and is passed back to the agent as the reason, so spoken redirections work.
- **A voice command didn't trigger**: console phrases match exactly, spoken alone: "clear the session", "compact the session", "switch to the deep model", "back to the fast model", "set effort to low" (or medium, high, max), "usage report", "go hands free", "start asking again". Extra words around them make a normal sentence for the agent instead. That guard is deliberate.
- **It answers my previous question instead of the one I just asked**: this is the interrupt-desync bug this codebase specifically armors against (`brain.reset_turn`); if you EVER see it, something has changed in the SDK. Grab `logs/backtalk.log` and file an issue; the log will show whether the stale-turn drain ran.

## Windows notes

- **No install.sh or run.sh:** they are Mac and Linux shell scripts. The wizard (`backtalk.md`) performs the install natively on Windows; launch with `uv run python -m backtalk.main`.
- **espeak-ng:** install it with winget or the official installer. backtalk looks for `libespeak-ng.dll` in the usual Program Files locations; if yours lives elsewhere, set `PHONEMIZER_ESPEAK_LIBRARY` to the dll's full path.
- **The ElevenLabs key** lives in the `ELEVENLABS_API_KEY` environment variable for now; Credential Manager support is planned.
- **One copy at a time:** run.sh's single-instance guard is Mac and Linux; on Windows, close the old window before starting a new one, or two voices answer one mic.
- **Speed:** `stt_device: "auto"` uses CUDA when present and CPU otherwise; CPU with `small.en` is plenty fast on a normal machine.

## The open-mic tradeoff

`--open-mic` listens continuously with voice-activity detection instead of hold-to-talk. Know what you're trading: any speech in the room (a video, music with vocals, another voice assistant) can be transcribed and answered as if it were you. Hold-to-talk is the default because the button is a perfect voice-activity detector and the mic is *closed* the rest of the time. `--barge-in` (interrupting it by talking over it) additionally requires headphones, or it hears its own reply and interrupts itself.

## For AI assistants: the architecture in six lines

```
hold key -> ears.record_held (sounddevice, 16kHz int16)
         -> ears.transcribe (faster-whisper, in-process, local)
         -> brain.ask_stream (warm Claude Agent SDK session,
                              cwd = agent_dir, streams sentences)
         -> mouth.say_chunk (kokoro in-process -> one long-lived
                             OutputStream; ElevenLabs optional)
signals.py mirrors state to .voice_* files (+ optional barehands state/)
permission_mode "ask": gated tools pause the turn and route to a spoken
                       yes/no (main.make_permission_gate). The LIVE
                       hands-free switch is a gate flag; a session
                       BOOTED hands-free is real SDK bypassPermissions
                       and never consults the gate
```

Three land mines with warning signs on them; do not "simplify" these away:

1. **The key-repeat filter in `ptt.py`.** The OS fires on_press continuously while a key is held; without the held-state flag, every repeat cancels the reply before it can speak.
2. **The one long-lived output stream in `mouth.py`.** A fresh stream per sentence causes onset blips or dead air on USB interfaces, Bluetooth, and streaming mixers. Interrupts pad silence into the stream; they never close it.
3. **`brain.reset_turn` in `brain.py`.** The SDK has one shared message stream with no query/response pairing; an interrupted turn leaves its leftovers buffered, and without the drain every later answer is off by one question.
4. **The pending-permission routing in `main.py`.** While a spoken permission ask is waiting, the next utterance is the ANSWER: it must never be treated as an interrupt or a new turn, or the paused turn gets cancelled out from under the SDK. The same goes for the live hands-free switch: the CLI refuses a live flip INTO bypassPermissions (it needs the danger flag at launch), which is why hands-free is a gate flag instead of an SDK mode change.

## Verify a working install

1. `./run.sh` → greeting speaks.
2. Hold the key, ask something, release → answer within ~2s.
3. Interrupt mid-reply with the key → it stops within a syllable.
4. Interrupt, then ask something NEW → the answer matches the NEW question (repeat 3×: that's the stream drain proving itself).
5. Ask something that needs a tool ("what's in my notes about X") → it speaks filler within a couple of seconds, then the answer.
6. Type a message in the terminal → spoken reply, same conversation.
7. Say "usage report" → it speaks turns and tokens (plus cost when the API reports one).
8. In ask mode: request a small file write → the spoken permission check plays → "yes" proceeds, and a second attempt answered "no" stands down.
9. Say "goodbye <name>" → sign-off plays, process exits, music restores.
