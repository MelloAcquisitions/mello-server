"""
tune_voice.py — read and set the Vapi speech-timing settings that decide
whether the agent feels fast, patient, and human, or laggy and robotic.

These are ASSISTANT-level engine settings. They are NOT part of the system
prompt — pasting a new prompt changes nothing here. They have been at Vapi's
defaults for this project's entire life.

    python3 tools/tune_voice.py            # show current vs recommended
    python3 tools/tune_voice.py --apply    # set them

Each setting group is patched SEPARATELY and read back, so a field name Vapi
has since renamed fails on its own instead of silently blocking the rest.
If something reports NOT APPLIED, set that one in the dashboard and tell me
the field name Vapi rejected.

WHAT THESE DO, AND THE TENSION BETWEEN THEM
-------------------------------------------
The goal is an agent that is SLOW TO INTERRUPT but FAST TO ANSWER. Those pull
against each other, and the naive fix for one makes the other worse:

  - Raise waitSeconds so it stops talking over people -> it now feels laggy.
  - Lower waitSeconds so it answers fast -> it jumps on every breath.

Smart endpointing is what resolves it. Instead of timing silence, it predicts
whether the sentence is semantically FINISHED. "I was thinking maybe..." and
"I was thinking maybe two thirty" have the same trailing silence and are not
the same turn. With it on, a LOW wait is safe, which is why the recommended
wait below is 0.4s and not the 0.8s that would otherwise be needed.
"""

import copy
import json
import os
import sys

import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID")
APPLY = "--apply" in sys.argv

if not VAPI_API_KEY or not VAPI_ASSISTANT_ID:
    sys.exit("Set VAPI_API_KEY and VAPI_ASSISTANT_ID first (they are in .env).")

BASE = f"https://api.vapi.ai/assistant/{VAPI_ASSISTANT_ID}"
HEADERS = {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}


# Patched one group at a time, so one rejected field name does not lose the rest.
GROUPS = [
    (
        "backchanneling OFF",
        {"backchannelingEnabled": False},
        "No 'mhm' or 'uh huh' while the seller is talking. A human's "
        "backchannels land in gaps; a model's land on a timer, and a mistimed "
        "one is the single most obviously robotic thing a voice agent does.",
    ),
    (
        "no artificial delay before speaking",
        {"responseDelaySeconds": 0},
        "Any padding here is pure added latency. Naturalness comes from "
        "endpointing and short turns, not from stalling.",
    ),
    (
        "kill dead calls fast",
        {"silenceTimeoutSeconds": 30},
        "One of your real calls ran 295 seconds and ended 'silence-timed-out'. "
        "That is five minutes of billed nothing. 30s is plenty — a seller who "
        "has said nothing for half a minute is gone.",
    ),
    (
        "hard call length cap",
        {"maxDurationSeconds": 600},
        "A stuck call should not be able to bill for an hour.",
    ),
    (
        "start speaking: fast but not interrupting",
        {"startSpeakingPlan": {
            "waitSeconds": 0.4,
            "smartEndpointingEnabled": True,
            "transcriptionEndpointingPlan": {
                "onPunctuationSeconds": 0.1,
                "onNoPunctuationSeconds": 1.0,
                "onNumberSeconds": 0.5,
            },
        }},
        "0.4s wait is snappy. It is only SAFE because smart endpointing "
        "predicts whether the sentence is finished rather than just timing "
        "silence. onNoPunctuationSeconds 1.0 is the one that matters most: "
        "when someone trails off ('I mean, the roof, it's...') there is no "
        "terminal punctuation, and a short timeout makes the agent pounce. "
        "onNumberSeconds 0.5 because people pause mid-number: 'two... thirty'.",
    ),
    (
        "stop speaking: instant barge-in",
        {"stopSpeakingPlan": {
            "numWords": 0,
            "voiceSeconds": 0.2,
            "backoffSeconds": 1.0,
        }},
        "numWords 0 means ANY sound stops the agent mid-word. This is almost "
        "certainly what broke the first test call: the seller said 'Hello?' "
        "while the agent was 13 seconds into a long greeting, the one-word "
        "interruption was discarded, and 18 seconds of dead air followed. "
        "backoffSeconds stops it resuming the instant you pause for breath.",
    ),
]


def fetch():
    r = requests.get(BASE, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def show(assistant):
    print("=" * 66)
    print("CURRENT SPEECH SETTINGS")
    print("=" * 66)
    current = {
        "backchannelingEnabled": assistant.get("backchannelingEnabled"),
        "responseDelaySeconds": assistant.get("responseDelaySeconds"),
        "silenceTimeoutSeconds": assistant.get("silenceTimeoutSeconds"),
        "maxDurationSeconds": assistant.get("maxDurationSeconds"),
        "startSpeakingPlan": assistant.get("startSpeakingPlan"),
        "stopSpeakingPlan": assistant.get("stopSpeakingPlan"),
    }
    print(json.dumps(current, indent=2, default=str))
    if all(v is None for v in current.values()):
        print("\n  Everything is None — these have never been set, so Vapi is using "
              "its defaults for all of them.")
    voice = assistant.get("voice") or {}
    print(f"\n  voice: {voice.get('provider')} / {voice.get('voiceId')} "
          f"(model={voice.get('model')}, speed={voice.get('speed')})")
    print("  NOTE: this script does NOT touch the voice. Which voice you use is "
          "a bigger lever than every setting below combined, and you have to "
          "pick it by listening — on a phone, not laptop speakers.\n")
    return current


def main():
    assistant = fetch()
    current = show(assistant)

    print("=" * 66)
    print("RECOMMENDED" + ("  — APPLYING" if APPLY else "  — DRY RUN"))
    print("=" * 66)

    applied, failed, unchanged = [], [], []

    for label, patch, why in GROUPS:
        key = next(iter(patch))
        now = current.get(key)
        want = patch[key]

        print(f"\n### {label}")
        print(f"  {key}")
        print(f"    now  : {json.dumps(now, default=str)}")
        print(f"    want : {json.dumps(want, default=str)}")
        for line in _wrap(why, 62):
            print(f"    {line}")

        if now == want:
            print("    -> already correct")
            unchanged.append(label)
            continue
        if not APPLY:
            continue

        r = requests.patch(BASE, headers=HEADERS, json=copy.deepcopy(patch), timeout=30)
        if not r.ok:
            print(f"    -> NOT APPLIED ({r.status_code}): {r.text[:220]}")
            failed.append((label, key, r.text[:150]))
            continue

        saved = r.json().get(key)
        if saved == want or (isinstance(want, dict) and isinstance(saved, dict)
                             and all(saved.get(k) == v for k, v in want.items()
                                     if not isinstance(v, dict))):
            print("    -> APPLIED and verified")
            applied.append(label)
        else:
            print(f"    -> PATCH accepted but read back as {json.dumps(saved, default=str)}")
            failed.append((label, key, "value did not persist as sent"))

    print("\n" + "=" * 66)
    if not APPLY:
        print("DRY RUN — nothing changed. Re-run with --apply.")
    else:
        print(f"{len(applied)} applied, {len(unchanged)} already correct, {len(failed)} failed.")
        for label, key, err in failed:
            print(f"  FAILED  {label} ({key}): {err}")
        if failed:
            print("\n  A failure here usually means Vapi renamed the field. Set those "
                  "in the dashboard and report the exact error — the rest are live.")
    print("=" * 66)
    print("\nAfter applying, place ONE call and change nothing else. Two changes at "
          "once and you cannot attribute the difference.")


def _wrap(text, width):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
