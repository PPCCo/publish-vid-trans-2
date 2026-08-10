# XTTS piper alternatives

I asked claude about the challenges I'm facing and below is the conversation I had.

For en/zh/ar/es/ru, I definitely want voice cloning. For ur/fa, it's not necessary, since there aren't many options.

I would like to try and generate 2-3 samples per language only for en/zh/ar/ur/fa (not es/ru) of @xxxx from 00:00:00 to 00:01:10 (This way, there would be a portion of Quranic verse copied in the source voice, as the current framework behaves). The purpose would be (1) to compare the time its taking, and (2) to compare the voice quality they generate.

# Languages and tools to compare

For ur/fa, I was thinking of trying out these two options:

- Azure Neural TTS
- Meta MMS-TTS

For en/zh/ar/es/ru, I was thinking of trying out these three options:

- DeepSpeed inference
- Use FP16 / half precision
- Fish Speech / OpenAudio S1

I also want to try the different suggestions by Claude, like `Batch your text chunks`, `Reduce unnecessary chunk splitting`, etc.

# Analysis and Recommendation

I want to analyze the conversation and the suggested tools below and tell me what you recommend, then plan and implement (or give me guidance if I need to do manual download for some tools and do a github release).

## My query 1

xtts v2 is really good but really slow. How can I speed it up without compromising the quality?

## Claude Response 1

A few levers actually move the needle on XTTS-v2 without hurting output quality:

**1. DeepSpeed inference (biggest win)**
This is the single most effective speedup — often 2-3x faster. It's built into the Coqui TTS library:

```python
model.load_checkpoint(config, checkpoint_dir="path", use_deepspeed=True)
```

Requires `pip install deepspeed`. On a decent GPU this alone can take you from ~3x realtime to well under 1x realtime for synthesis.

**2. Use FP16 / half precision**

```python
model.half()
```

Cuts memory and often speeds up inference meaningfully on modern GPUs (anything with good FP16 tensor core support — RTX 20-series and up). Quality loss is generally imperceptible for TTS.

**3. Cache the speaker latents / conditioning**
XTTS-v2 recomputes GPT conditioning latents and speaker embedding from the reference audio every call unless you cache them. If you're doing many segments with the same speaker (which you almost certainly are in translate-house), extract once:

```python
gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(audio_path=ref_wav)
```

Then reuse those tensors across all chunks for that speaker instead of recomputing per-sentence. This is a huge win if your current pipeline re-derives conditioning per segment.

**4. Batch your text chunks**
Instead of calling `inference()` sentence-by-sentence in a loop, batch multiple text segments together where the API allows it, or at least run inference in a tight loop with the model kept warm on GPU (avoid reloading between chunks).

**5. Reduce unnecessary chunk splitting**
XTTS-v2 has an internal max token length per inference call. If your text splitter is being overly conservative (very short chunks), you're paying model-load/attention overhead more often than needed. Tune the splitter to use closer to the model's actual limit.

**6. Streaming inference for perceived latency**
If part of your pain is wall-clock-to-first-audio rather than total throughput, `inference_stream()` returns audio chunks progressively rather than waiting for the full generation — doesn't reduce total compute but massively helps pipeline latency if downstream steps (muxing, upload) can start early.

**7. Hardware-level**

- Make sure you're actually on GPU, not silently falling back to CPU (easy to miss in a multi-stage pipeline).
- If you're on an A100/H100/4090-class card, `torch.compile()` on the GPT2-based decoder can give another decent bump, though XTTS's architecture doesn't always play perfectly with compile — test it, some ops fall back to eager mode.

**What I'd avoid for your use case**: switching to a distilled/lighter checkpoint or dropping to XTTS's lower-quality fast mode — given translate-house is producing public-facing multilingual content, the quality floor matters more than shaving another 20%. DeepSpeed + cached latents + FP16 together usually get you 3-5x without any audible quality hit, which should be enough headroom.

If your bottleneck is specifically long-form content (full speeches, not short clips), the conditioning-latent caching (#3) combined with proper chunking (#5) is usually where people find the pipeline was doing 5-10x more redundant work than necessary — worth checking your current implementation first before reaching for more infrastructure.

---

## My query 2

I need to generate Spiritiual and religious lectures in multiple languages, including en, zh, ar, fa, ur, fr, es, pt, and ru.

What are the best options. I tried piper but don't like the quality it generates. Voice volume and tone changes a lot

I prefer voice cloning, but not a must. XTTS-v2 is very good but it's quite slow and doesn't support ur

## Claude Response 2

Good news and bad news: none of the "big" open multilingual cloning models (XTTS-v2, Fish Speech/OpenAudio S1) actually cover both Farsi and Urdu — that's not a niche gap, it's a real hole in the open-source landscape for those two languages specifically. Here's how the field breaks down for your 9-language set:

### The core problem

- **XTTS-v2** officially supports: en, es, fr, de, it, pt, pl, tr, ru, nl, cs, ar, zh-cn, ja, hu, ko, hi — no `fa`, no `ur`. Confirms what you found.
- **Fish Speech / OpenAudio S1** (the other major open-source cloning model, similar architecture class to XTTS but **noticeably faster** — real-time factor around 1:7 on an RTX 4090) officially supports English, Chinese, Japanese, German, French, Spanish, Korean, Arabic, Russian, Dutch, Italian, Polish, Portuguese — same gap, no `fa`/`ur`.
- Their newer **S2 Pro** model claims much broader coverage (trained on over 10 million hours of audio across approximately 50 languages), but I can't confirm fa/ur are in that list from what's published — worth testing directly rather than assuming.

So the split isn't "which single model do I switch to" — it's "one model (or two) for your 7 well-covered languages, plus a separate solution for fa/ur."

### For en, zh, ar, fr, es, pt, ru (7 of your 9)

**Test OpenAudio S1/S2 alongside XTTS-v2.** It's open-source, self-hostable, and benchmarks ahead of XTTS-v2 on both speed and error rate (~0.4% CER and ~0.8% WER), with zero-shot cloning from a similarly short reference clip. Given the volume/tone consistency problem you had with Piper, pay attention to whether S1 holds prosody steady across long lecture-length passages — that's exactly where VITS-family models (Piper's architecture) tend to drift, and it's the thing to stress-test before committing.

### For fa and ur (the gap)

Three real options, in order of what fits your self-hosted/deterministic philosophy vs. what actually works today:

1. **ElevenLabs** — confirmed voice cloning support for both Urdu and Persian on their multilingual models. This is the pragmatic answer: your open-source options for these two languages specifically are weak, and for religious content where mispronunciation of names/terms is a real concern, a cloud model with mature language coverage may be worth the exception to your "self-hosted by default" rule — scoped narrowly to just these 2 of 9 languages rather than the whole pipeline.

2. **Meta MMS-TTS** — free, self-hosted, covers 1100+ languages including both Urdu and Farsi. But it's VITS-based per-language, single fixed voice, **no cloning** — and given VITS is the same family as Piper, you'd likely hit the same volume/tone inconsistency issues you're trying to escape. I'd treat this as a fallback, not a first choice.

3. **Community XTTS fine-tunes for Urdu** — there's at least one open-source Urdu/Hindi TTS project with voice cloning built as an XTTS-adjacent fine-tune. Quality and stability are unverified at your scale, but if maintaining a single model family matters for pipeline consistency, this is worth evaluating before defaulting to ElevenLabs for that one language.

### My suggested setup

Given translate-house's gate-based architecture already handles per-language routing, I'd wire this as: OpenAudio S1 (or XTTS-v2 with the caching/DeepSpeed optimizations from before) for your 7 covered languages, ElevenLabs API called only for fa/ur segments, with the human gate before publish acting as your QA checkpoint on the two languages you have less control over. That keeps the deterministic/self-hosted default everywhere it's achievable and isolates the one necessary cloud dependency to a narrow, auditable slice of the pipeline.

---

## My query 3

let's say I don't need voice cloning for ur/fa. What are my best options?

## Claude Response 3

This actually opens things up quite a bit, since fixed-voice neural TTS is a much more mature field than cloning.

Best options for fa/ur specifically

Azure Neural TTS — the strongest fit for your case:

Urdu: ur-PK-UzmaNeural (female) and ur-PK-AsadNeural (male), both natively trained (not multilingual fallback voices)
Persian: Azure added Farsi as part of a broader 2024 language expansion, with dedicated fa-IR neural voices
Full SSML support — <prosody>, <break>, <emphasis> tags let you directly control pitch/rate/volume per phrase, which is the actual fix for the Piper problem you described. Piper's volume/tone drift is a VITS architecture issue; Azure's neural voices are a different, more stable model class and give you explicit levers instead of hoping the model behaves.
Can run in containers for on-prem/offline deployment if you want to keep it out of a pure cloud-call pipeline, though the container route needs a licensing agreement with Microsoft.

Google Cloud TTS — also has dedicated Urdu and Persian neural voices with strong SSML support and custom pronunciation dictionaries for fine-grained pronunciation control, which matters for you given Quranic/Arabic loanwords inside Urdu and Persian text. Slightly less warm-sounding than Azure's Neural2/HD tier by most comparisons, but very stable and fast.
AssemblyAI

Meta MMS-TTS — the self-hosted option, free, covers both fa and ur (and 1100+ other languages). Worth naming since it fits your no-cloud-dependency default. But it's VITS-based — same architecture family as Piper — so I'd genuinely expect it to carry similar prosody/volume inconsistency risk on long lecture-form text. Test it on a full 20+ minute passage before trusting it, not just a short clip.

Worth considering: simplifying the whole pipeline

Since you don't need cloning for fa/ur, and Azure/Google both cover ar, fr, es, pt, ru, zh, en as well with mature, stable neural voices — you could collapse this from "XTTS/Fish for 7 languages + cloud-only for 2" down to a single Azure or Google TTS call for all 9, with SSML as your consistency/pronunciation control layer throughout. That trades your self-hosted default for one clean, deterministic API surface — worth weighing against how much you value keeping the bulk of the pipeline off cloud dependencies versus the operational simplicity of one voice engine everywhere.

If you want, I can sketch out how the SSML-based approach would slot into translate-house's gate structure — pronunciation dictionary as a pre-synthesis gate, for instance, so proper nouns and Quranic terms get locked in before audio generation rather than caught after.
