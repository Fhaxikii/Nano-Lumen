# 01 · Getting Started

**What this page covers**: requirements, installation, launch, and the minimal configuration needed for your first conversation.  
**After reading it you can**: run Nano on your machine and complete one conversation.  
**Prerequisites**: none.

> Language: [中文](../zh/01-getting-started.md) · English

---

## Requirements

- Windows 10 or later. The project uses WebView2 as its rendering layer and
  depends on several Windows APIs; other operating systems are not supported.
- About 6 GB of disk space.
- 8 GB of RAM or more recommended. Loading the embedding model commits about
  2.3 GB at once; with too little memory, knowledge-base retrieval becomes
  unavailable.

## Install from source (developer)

This page is for developers who want to run Nano from source or contribute.
You need **Python 3.10** (version-sensitive; newer versions are unverified).

```
install.bat
```

The batch file delegates the real work to `install.ps1`. The dependency list
is `requirements_cpu.txt`.

## Launch

```
start.bat
```

RAG models (bge-m3, bge-reranker-v2-m3) are downloaded automatically on
first launch. Without them, Nano still converses normally — only
knowledge-base retrieval is unavailable.

## First-conversation configuration

After launch, click the three colored dots in the top-left corner of the
window to open Settings, go to **General → Environment**, and fill in at
least two fields:

- **Vendor**: the model provider.
- **API Key**: the key for that vendor.

Settings are written to `.env` in the project root. You can also edit that
file directly; the available keys are listed in
[03-configuration.md](03-configuration.md).

## FAQ

**Knowledge-base retrieval unavailable, complaining about missing model files**

First check that `~/.cache/huggingface/hub/models--BAAI--bge-m3/` contains
`model.safetensors` at roughly 2.27 GB. If the file is intact and loading
still fails, the cause is usually insufficient memory; in that case the error
message explicitly says the file is fine and does not need re-downloading.

**Blank window after launch**

Check the console output. The rendering layer is WebView2 and requires the
Microsoft Edge WebView2 Runtime to be installed.

**Where is the settings menu?**

The settings entry is the three colored dots (red, yellow, green) in the top-left corner of the window.

**Where are chat history and memories stored?**

Everything is stored locally in the `data/` directory. Nothing is uploaded to the cloud. To clear everything, just delete that directory. You can also export readable chat history from the settings menu.

**How do I use this software? Is there a tutorial?**

Nano doesn't have a traditional tutorial — it has its own built-in manual that it can consult. You don't need to read any guides. Just ask it directly: "Where's the settings menu?" "How do I enable OS permissions?" "How do I export my chats?" It knows the answers.

**Does it track my computer usage? Is there a privacy risk?**

Nano collects minimal necessary behavioral metadata for trajectory awareness: foreground window / process switching, keyboard events (only key types like Ctrl+S / Backspace, not what you type), and CPU sampling. All traces store only a one-line summary of "which app + what it's doing" — no raw events or plaintext content, and records expire after 18 hours. All data stays local, nothing is uploaded to the cloud.

---

## How to verify you got it right

1. `start.bat` launches and the window renders properly.
2. You can complete a conversation and receive a reply.
3. Run the tests: `bash run_tests.sh`. The expected last line is
   `OK 真·全量 0 失败` (literally "OK — truly full suite, 0 failures"). If the
   only failing test is `tests/t_f5_live.py` with return code 139, that is
   usually the embedding model load running out of memory and crashing the
   process — unrelated to your change.

---

← Back to [README](README.md)
