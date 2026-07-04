# Home Assistant Integration

`ha` is a genuinely different kind of [Source](Sources) from the other six — it's the only one answering questions about devices and physical state you directly control, and it specifically targets a category of question Home Assistant's own built-in voice assistant doesn't handle well: analytical, multi-entity summaries rather than single-device commands.

## Setup

1. In Home Assistant, go to your **Profile** (click your username in the sidebar)
2. Scroll to **Long-lived access tokens**
3. Click **Create Token**, give it a name, copy the token
4. Set `HA_URL` to your instance's URL (e.g. `http://192.168.1.100:8123`)
5. Set `HA_TOKEN` to the token you just generated

That's the entire setup — no webhook, no custom component to install in HA itself. Mnemolis reads entity states directly through HA's existing REST API using the token.

## What `ha` actually answers

HA's own built-in conversation/intent system is genuinely good at single-entity commands and lookups — "turn on the kitchen light," "is the front door locked." `ha` exists specifically for the category just above that: questions that need to look across *many* entities at once and summarize, which HA's own intent handling isn't designed to do.

- **"house status summary"** — lights, locks, sensors, motion, batteries, all combined into one overview
- **"indoor air quality"** — CO2, temperature, humidity, specifically from indoor sensors
- **"security status"** — locks, doors, and recent motion with relative timing ("2 hours ago"). Motion is recognized through either of Home Assistant's two common conventions — a dedicated `event` domain entity, or a `binary_sensor` with `device_class: motion` (the convention most Zigbee2MQTT/Z-Wave/PIR integrations use) — so either kind of motion sensor is picked up, not just one.
- **"battery status"** — every device's battery level in one summary, not one lookup per device
- **"outdoor conditions"** — weather *station sensor* readings, distinct from [`forecast`](Sources#forecast--weather)'s predictive data — this is "what does my actual outdoor sensor currently read," not "what will the weather be"
- **"how much power am I using"** — current and historical consumption

## Area awareness

`GET /areas` lists every HA area Mnemolis has detected, along with entity counts and the natural-language aliases it recognizes for each — useful for confirming Mnemolis actually sees your area structure the way you expect, and for understanding what phrasing will correctly scope a query to one part of the house rather than the whole thing.

Scoping a query to an area applies the exact same filtering rules as an unscoped query — the same `exclude_entity_keywords`, device-class, and domain logic, just additionally restricted to that area's entities. *"Indoor air quality in the living room"* excludes the same raw device-telemetry noise (processor temperature sensors, ESP32 internals) that a plain *"indoor air quality"* already excludes, not a separate, looser filter just because an area was specified.

## Fusion and conditional detection

`ha` participates in [Fusion](Fusion) like any other source — "house status and what's the weather" automatically fuses `ha` + `forecast` into one response, no special handling needed for this specific combination.

`ha` is also one of exactly three sources [Conditional Query Detection](Conditional-Query-Detection#honest-abstention--the-actual-point-of-this-feature) trusts for a real, structured yes/no verdict — specifically lock/unlock and door open/closed states. *"If the back door is unlocked, let me know"* gets a genuine, confident answer through `ha`, not just a presented-but-uninterpreted result, because lock and door state is exactly the kind of binary, unambiguous signal that feature was built to recognize.

## Why this exists as a separate source instead of just using HA's own assistant

The honest answer is that HA's own conversation/intent system and Mnemolis's `ha` source are solving genuinely different problems, not competing at the same one. HA's strength is fast, reliable single-device control and lookup — exactly what you want for "turn off the lights," where you don't want an LLM-mediated round trip in between you and the actual device action. `ha`'s strength is the analytical summary case HA's intent system isn't built for at all. Whether to eventually route some voice queries more directly to Mnemolis instead of through HA's own assistant layer — keeping HA for device control and audio I/O specifically — remains a real, open architectural question; in practice the current voice path runs *through* HA's conversation agent via the [`mnemolis_intents`](https://github.com/immortalbob/mnemolis_intents) tool, with [answer synthesis](Answer-Synthesis) producing the grounded spoken answer.

---

## Development Notes

Five real bugs were found in this source during a deliberate, full read of the file — a severe word-boundary bug that could silently drop the exact entity a query asked about, a three-bug chain around `binary_sensor`-style motion entity support, a small grammar inconsistency, and an area-scoped query silently skipping real exclusion filtering. See [The Home Assistant Bulletproofing Pass](The-Home-Assistant-Bulletproofing-Pass) for the full account. If "no matching entities found" ever shows up for a question that clearly should have an answer, `GET /areas` and checking the entity's exact name/state in Home Assistant directly are the fastest way to rule out a naming mismatch versus a real, new bug.
