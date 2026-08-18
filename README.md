# backtalk

> **Never used Claude Code?** Start at [jaredrhod.com](https://jaredrhod.com): pick your situation and it routes you to the right path.

Talk to your Claude Code agent out loud. Hold a key, say the thing, and it answers through your speakers in a real voice about a second later, with all its tools, your project context, and its own personality. Your AI finally has something to say back.

The hearing and the voice run local: free, offline models on your machine, no voice API keys, no per-word costs. The brain is the Claude Code you already have. On a Claude subscription, talking works like any other session and uses your plan's usage, with nothing extra to buy. This is the same voice loop I run every day, the one you see answering in about a second on my videos, shipped as working code so your agent's job is pointing it at your setup, not building it from scratch.

## What it does

- **Hold a key, talk, release.** Your words are transcribed locally and handed to a live Claude Code session. The reply is spoken sentence by sentence as it's generated, with first audio in about 1 to 2 seconds on warm turns.
- **It's YOUR agent talking.** The session runs in the folder whose CLAUDE.md defines your assistant: same name, same personality, same memory as your terminal sessions. backtalk has no personality of its own; it's a mouth and ears for whoever you already have. (No agent yet? The [ai-memory-vault](https://github.com/jaredrhod/ai-memory-vault) build ships with mine, Jarvis, ready to use.)
- **Interrupt it.** Press the key while it's talking and it shuts up and listens. No headphones needed, because the mic only opens while you hold the key, so it never hears the speakers.
- **Type instead whenever you want.** Typing in the terminal is the same conversation, and the reply is still spoken.
- **It asks before it acts.** When your agent wants to do something real (write a file, run a command), it asks out loud and waits. An exact spoken yes approves; anything else denies, with your words passed back as the reason, so "no, put that in drafts instead" actually steers it. Most read-only work passes without interrupting. Prefer fully hands-free? Say "go hands free" and confirm; it changes its own config.
- **The voice console.** Session control by voice, so you never go back to the keyboard: "clear the session", "compact the session", "switch to the deep model" / "back to the fast model", "set effort to low" (or medium, high, max), "usage report". Exact phrases, spoken alone. (Credit where due: this grew out of a community member's own build shared in the Discord.)
- **Music ducks while it speaks** (Spotify, macOS) and comes back up after.
- **It thinks out loud.** While the agent works, you hear the processing sound from my videos, so a pause never reads as a dead line. Silence it with `"thinking_sound": ""` in the config.

## Install

```
git clone https://github.com/jaredrhod/backtalk
cd backtalk
./install.sh
```

The installer sets up a Python environment, the two local AI models (speech-to-text and the voice), and the one system library they need. First run downloads the models (about 1 GB total); everything after is instant. Prerequisites: [Claude Code](https://claude.com/claude-code) with a Claude subscription, and `uv` (the installer offers to install it).

**The easy way to configure it:** open this folder in Claude Code and say *"read backtalk.md and set me up."* The wizard picks your agent folder, your key, and your voice with you, then test-fires the whole loop.

**Already in a Claude Code session with your agent?** One sentence does the whole install: *"clone https://github.com/jaredrhod/backtalk.git, then read backtalk/backtalk.md and set me up."* Your agent runs the installer and the wizard for you.

**The manual way:** edit `backtalk.json`. Point `agent_dir` at the folder whose CLAUDE.md is your agent, set `name` to your agent's name, pick a `ptt_key`. Then:

```
./run.sh
```

Hold the key. Talk. Let go.

## Windows

Windows is the newest lane, and the setup runs through the wizard instead of the shell scripts (`install.sh` and `run.sh` are Mac and Linux). Open this folder in Claude Code and say *"read backtalk.md and set me up"*: the wizard installs uv, espeak-ng, the environment, and the models natively, then launches with `uv run python -m backtalk.main`. The ElevenLabs key lives in the `ELEVENLABS_API_KEY` environment variable on Windows for now (Credential Manager support is planned). Hit something rough? The Windows notes in `TROUBLESHOOTING.md` carry the known quirks, and issues are welcome.

## The voice

The default voice is `bm_lewis`, a British male with exactly the butler register, from Kokoro, a local open-source TTS. Around 60 voices ship free; set `voice` in `backtalk.json` (the first letter picks the language: `a` is American, `b` is British, and there are Spanish, French, Hindi, Italian, Japanese, Portuguese, and Chinese voices too).

**Want a premium voice?** ElevenLabs works on your own API key: set `elevenlabs.enabled` and your `voice_id` in the config, and have `ffmpeg` installed. **The key never goes in a file.** On macOS, seed it into the Keychain once with `security add-generic-password -a "$USER" -s backtalk-elevenlabs -T /usr/bin/security -w` (it prompts for the secret) and backtalk reads it from there. Linux: `secret-tool store --label backtalk service backtalk-elevenlabs`. The `ELEVENLABS_API_KEY` environment variable works as a last resort, but an export in a shell profile is a plaintext key on disk; the keychain is the grown-up path. Kokoro stays wired in as the automatic fallback, so if the cloud fails the voice degrades instead of going mute.

## Give it a face (optional)

backtalk writes tiny state files while it listens, thinks, and speaks, so anything can watch them and react in real time.

- **[ai-visualizer](https://github.com/jaredrhod/ai-visualizer)** is the matching face: four full-screen visualizers, including the living circuit board from my videos. Point its `bus_dir` at this folder (or set `signals_dir` here to its folder) and it performs your actual conversation, idling, listening, thinking, and speaking along with the voice.
- **[barehands](https://github.com/jaredrhod/barehands)**: point `barehands_state_dir` at its `state/` folder and the on-screen ring becomes your agent's face, breathing while idle, spinning while thinking, and pulsing with the voice while it talks.

Mind ([ai-memory-vault](https://github.com/jaredrhod/ai-memory-vault)), mouth (this), face (ai-visualizer), hands (barehands).

## The fine print that matters

- **Usage:** every spoken turn is a real Claude Code turn, so a long voice session uses your plan the same way a long typing session does. The config pins the fast model tier on purpose; it's most of the speed, and it's the lighter draw.
- **Permissions: ask first, hands-free by choice.** The default is `"ask"`: gated actions get a spoken permission check, answered by voice or by typing, and silence for about 75 seconds means no. `"bypassPermissions"` is fully hands-free: the agent acts without asking, exactly like a terminal session with approvals off. Never hand-edit the file to switch; tell your agent to change it (takes effect the next time the voice line starts), or say "go hands free" / "start asking again" in a voice session for a flip that happens immediately and saves itself.
- **The mic is closed except while you hold the key.** Nothing records in the background, ever. The `--open-mic` flag exists for always-listening mode if you want it, tradeoffs documented in `TROUBLESHOOTING.md`.
- Something misbehaving? `TROUBLESHOOTING.md` covers the classics, and `logs/backtalk.log` has the receipts.

## Credits

Speech recognition by [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) running [OpenAI Whisper](https://github.com/openai/whisper) models (MIT). Voice by [Kokoro](https://github.com/hexgrad/kokoro) (Apache 2.0) with [espeak-ng](https://github.com/espeak-ng/espeak-ng) (GPL-3.0, used as a system tool) for phonemization. Built on the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview).

## The rest of it

A voice is better with a face and a memory. The visualizer performs the conversation on screen while you talk, and the memory vault is what your agent actually speaks from, so it remembers you between sessions.

- **The whole stack, one command.** [fullstack-agent](https://github.com/jaredrhod/fullstack-agent) installs the memory, the voice, the face, and the hands, and wires them together for you. Pick only the pieces you want: https://jaredrhod.com
- **The videos.** Free series on all of it: https://youtube.com/@jaredrhod
- **The Discord.** Thousands of builders, and the fastest place to get unstuck: https://discord.gg/YSdsqMv3V8
- **Everything else,** free and open source: https://jaredrhod.com

## Support

Free to use, and always will be. If this helped you out, you can buy me a coffee:

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/jaredrhod)

## License

Copyright (c) 2026 Jared Rhodenizer.

Licensed under the GNU Affero General Public License, version 3 or later (AGPL-3.0-or-later). **Use it in your business, commercially, for free.** Run it, change it, build your workflow on top of it, and charge for the work you do with it. The one rule is that it stays open: if you hand it to someone else, or run a modified version as a service other people use, your version ships under this same license with its source available. Credit me when you build on it. Want it inside a closed-source commercial product? Email license@jaredrhod.com. Full terms are in the LICENSE file and at https://www.gnu.org/licenses/agpl-3.0.html
