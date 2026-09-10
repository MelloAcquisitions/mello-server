# Making Skylar sound human

Everything here is Vapi assistant configuration, not code. The system prompt
controls WHAT is said; these settings control HOW it lands — timing,
interruption, silence. The first test call failed entirely on this layer
while the prompt and the tool path were both fine.

Check every field name against Vapi's current dashboard before trusting it —
their API has renamed things between versions, and I cannot reach it to
verify.

---

## The three settings that do what you asked for

### 1. Silence while the seller talks — no "mhm"

```
backchannelingEnabled: false
```

This is what makes the agent say "mhm" and "uh huh" over you. Turn it off.

Worth knowing you are making a real trade: backchannels *are* a human quirk,
and a person who says nothing for thirty seconds can read as absent. But a
mistimed "mhm" landing in the middle of someone's sentence is far worse than
silence — it is the single most obviously robotic thing a voice agent does,
because a human's backchannels land in gaps and a model's land on a timer.
Silence is the safer default. Revisit once the rest is solid.

### 2. The agent must not talk over the seller

```
startSpeakingPlan:
  waitSeconds: 0.8                    # default is ~0.4
  smartEndpointingEnabled: true       # or the provider Vapi currently offers
  transcriptionEndpointingPlan:
    onPunctuationSeconds: 0.3
    onNoPunctuationSeconds: 1.2       # a sentence with no clear end
    onNumberSeconds: 0.6              # people pause mid-number: "two... thirty"
```

`waitSeconds` is how long the agent waits after the seller stops before it
starts. Too low and it jumps on every breath. 0.8 feels attentive rather
than eager, and costs you almost nothing — your measured turn latency was
1.68s, so this puts you around 2.1s, still well inside natural range.

`onNoPunctuationSeconds` matters most. When someone trails off — "I mean,
the roof, it's… " — that has no terminal punctuation, and a short timeout
makes the agent leap in. Give it over a second.

`smartEndpointingEnabled` predicts whether a sentence is actually finished
rather than just timing the silence. If Vapi offers it, use it — it is worth
more than any of the numbers above.

### 3. The seller must be able to interrupt the agent

```
stopSpeakingPlan:
  numWords: 0          # ANY sound stops the agent
  voiceSeconds: 0.2
  backoffSeconds: 1.0  # after being cut off, wait before resuming
```

**This is the opposite of setting 2, and both are correct.** The agent
should be slow to start and instant to yield. `numWords: 0` means any sound
from the seller stops it mid-word.

This is almost certainly what broke the first call. The seller said "Hello?"
at second 16 while the agent was 13 seconds into a 32-word greeting. With
`numWords` above 0, a one-word interruption is discarded — the agent kept
talking, then waited for input already given, and 18 seconds of dead air
followed. `dump_tools.py` now prints and flags this.

`backoffSeconds` stops the agent resuming the instant you pause for breath.

---

## The biggest lever is the voice itself

No prompt fixes a robotic voice. Before tuning anything else, place three
calls to yourself with three different voices and keep the one that makes
you least uncomfortable. This is worth an hour and it beats every other
change combined.

What to look for:

- **Latency class.** ElevenLabs Turbo/Flash and Cartesia Sonic are built for
  real-time. Higher-quality slower models add hundreds of milliseconds to
  every single turn, and turn latency is the thing sellers feel.
- **Stability vs variation.** High stability = flat and even, which reads as
  synthetic. Lower stability = more pitch movement, which reads as human but
  occasionally mangles a word. Mid-low is usually the sweet spot for sales.
- **Speed.** Most defaults are slightly fast. Nudging down ~5% reads as
  relaxed and confident rather than rushed.
- **Does it breathe?** The tell is a voice that starts each sentence at
  identical volume with no intake. Some models model breath; those win.

**Listen on a phone, not laptop speakers.** Phone audio is 8kHz narrowband
and it destroys detail. A voice that sounds great in a browser preview can
sound completely synthetic through a real call, and the seller only ever
hears the second one.

---

## Smaller knobs, in the order worth trying

| Setting | Try | Why |
|---|---|---|
| `model.temperature` | 0.7–0.8 | 0.6 produces same-y phrasing. Higher varies wording, which is most of what "sounds scripted" means. Watch that it doesn't drift off-script. |
| `responseDelaySeconds` | 0.1–0.3 | An instant reply is uncanny — humans take a beat. Small, but real. |
| `backgroundSound` | test both | Faint office noise reads as a real person at a desk to some ears and as a fake call center to others. Try it on yourself over a phone. |
| `firstMessage` | keep it tiny | "Hey, is this {{seller_name}}?" and nothing else. |
| `maxDurationSeconds` | 600 | A stuck call should not bill for an hour. |

---

## How to actually test this

Change **one thing per call**. Two changes and you cannot attribute the
difference — you will convince yourself something helped when it did not.

A repeatable script, so calls are comparable:

1. Answer normally.
2. Pause two seconds before your first real answer — does it jump in?
3. Trail off mid-sentence: "I mean, the roof, it's…" — does it wait?
4. Interrupt it mid-sentence — does it stop immediately?
5. Say something with real feeling ("my mom passed, it's her place") — does
   it react before continuing, or plough on to the next question?
6. Give a number with a pause: "maybe… two thirty?" — does it cut you off
   at the pause?
7. Let it reach an offer. Does it say "two thirty-two" or "two hundred
   thirty-two thousand dollars"?

Score each one yes/no and write it down. Six of seven is launchable; a
"no" on 4 or 5 is what makes people hang up.

**Listen to the recording afterwards, not just the transcript.** The
transcript of the first test call reads almost fine. The audio is where the
eighteen seconds of nothing lives.

---

## Where the ceiling is

Two things a prompt cannot buy:

**The model is `claude-haiku-4-5`.** That is the right call for latency —
your 659ms LLM time is genuinely good, and a larger model would add several
hundred milliseconds to every turn, which sellers feel far more than they
feel slightly better word choice. Keep it unless the agent starts making
judgment errors rather than phrasing errors.

**Turn-taking is the hard part and it is not solved.** Humans overlap,
finish each other's sentences, and know when a pause means "thinking" versus
"done". No current voice stack does this well. You can get to "sounds like a
person reading from a script they know well." Getting to "sounds like a
person" is not available yet at any price, so aim for the first one and stop
paying for the gap.
