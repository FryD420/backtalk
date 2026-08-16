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
- **It answers my previous question instead of the one I just asked**: this is the interrupt-desync bug this codebase specifically armors against (`brain.reset_turn`); if you EVER see it, something has changed in the SDK. Grab `logs/backtalk.log` and file an issue; the log will show whether the stale-turn drain ran.

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
```

Three land mines with warning signs on them; do not "simplify" these away:

1. **The key-repeat filter in `ptt.py`.** The OS fires on_press continuously while a key is held; without the held-state flag, every repeat cancels the reply before it can speak.
2. **The one long-lived output stream in `mouth.py`.** A fresh stream per sentence causes onset blips or dead air on USB interfaces, Bluetooth, and streaming mixers. Interrupts pad silence into the stream; they never close it.
3. **`brain.reset_turn` in `brain.py`.** The SDK has one shared message stream with no query/response pairing; an interrupted turn leaves its leftovers buffered, and without the drain every later answer is off by one question.

## Verify a working install

1. `./run.sh` → greeting speaks.
2. Hold the key, ask something, release → answer within ~2s.
3. Interrupt mid-reply with the key → it stops within a syllable.
4. Interrupt, then ask something NEW → the answer matches the NEW question (repeat 3×: that's the stream drain proving itself).
5. Ask something that needs a tool ("what's in my notes about X") → it speaks filler within a couple of seconds, then the answer.
6. Type a message in the terminal → spoken reply, same conversation.
7. Say "goodbye <name>" → sign-off plays, process exits, music restores.
