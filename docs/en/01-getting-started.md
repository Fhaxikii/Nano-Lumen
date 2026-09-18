# 01 · Getting Started

**What this page covers**: requirements, installation, launch, and the minimal configuration needed for your first conversation.  
**After reading it you can**: run Nano on your machine and complete one conversation.  
**Prerequisites**: none.

> Language: [中文](../zh/01-getting-started.md) · English

---

## Requirements

- Windows 10 or later. The project uses WebView2 as its rendering layer and
  depends on several Windows APIs; other operating systems are not supported.
- Python 3.10. The version matters: the code relies on 3.10-specific
  guarantees, and newer versions are unverified.
- About 6 GB of disk space. The embedding model `BAAI/bge-m3` alone is
  roughly 2.3 GB.
- 8 GB of RAM or more recommended. Loading the embedding model commits about
  2.3 GB at once; with too little memory, knowledge-base retrieval becomes
  unavailable (see `_classify_model_load_error` in `core/rag.py` for how that
  failure is handled).

## Install

```
install.bat
```

The batch file delegates the real work to `install.ps1`. The dependency list
is `requirements_cpu.txt`, exported from a working environment via
`pip freeze`, with every version pinned.

When changing dependencies, re-export the file from a working environment;
do not hand-edit version numbers.

PyTorch is installed as the CPU build; the installer already appends the
matching extra index.

## Download the embedding model

Knowledge-base retrieval depends on a local embedding model that must be
downloaded separately:

```
py -3.10 _setup_rag_models.py
```

The script supports resumable downloads. Without the model, Nano still
converses normally — only knowledge-base retrieval is unavailable.

## Launch

```
start.bat
```

Equivalent to running `python app.py` from the project root.

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

**Why does it download 2GB after installation?**
That's the local embedding model (bge-m3), used for knowledge-base RAG retrieval — not your LLM. It runs entirely on your local machine with telemetry disabled. You can skip this step if you don't use the knowledge-base feature; the UI will show a missing-environment warning, which is expected.

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
