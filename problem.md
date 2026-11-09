# Beep Locator: the problem

## The situation

Something in a room is beeping and I can't find it.

The beep is short and intermittent: a smoke alarm with a dying battery, a forgotten gadget, a toy at the bottom of a box. Each beep is too brief and too far apart for my ears to home in on. By the time I've turned my head it has stopped, and reflections off walls and furniture make the direction ambiguous anyway.

## The idea

Use the devices I already have (laptops, phones, iPads) as a temporary array of microphones. Spread them around the room, have them all listen at the same time, and use the tiny differences in *when* each one hears the beep to work out where it came from.

## What I want to build

A Python web app that coordinates everything, using only web browsers on the devices. Nothing to install on the phones.

- **Server**: a Python web app running on a laptop on the same network. It serves the pages, holds a WebSocket connection to every device, runs calibration and listening, and does the maths.
- **Coordinator page**: opened on one device, probably a laptop. Shows the join URL and a QR code, the connected listeners, a map of the room with each listener's position, live status, and the estimated location of the beep.
- **Listener page**: any device that opens the join URL becomes a listener. After one tap (browsers need a user gesture to unlock audio) it captures the microphone through WebAudio, plays calibration sounds when asked, and reports back to the server over a WebSocket.

The device showing the coordinator page should be able to act as a listener too.

### Intended flow

1. Start the server on a laptop.
2. Open the coordinator page. It shows a QR code.
3. On each phone, tablet or laptop: scan the QR code, tap "Start listening", put the device down somewhere in the room. Each listener gets a name and a big colour ("Blue", "Orange", ...) so I can match devices to their dots on the map.
4. Press "Calibrate". The devices take turns playing short chirps, and the system works out where every device is relative to the others and draws them on a map.
5. Wait. Each time the mystery device beeps, the map shows an estimate of where the sound came from, and the estimate sharpens as more beeps arrive.
6. Go and look there.

## The hard part: no manual measuring

The system has to know where the listeners are before it can locate anything. I could drag icons around a floor plan, but measuring and entering every device's position is tedious and error-prone, and errors of tens of centimetres matter. Ideally the devices find *each other* by playing sounds to each other.

So there are two sub-problems:

1. **Self-calibration**: where are the listeners, relative to one another?
2. **Source localisation**: given the listener positions, where did the beep come from?

## Is self-calibration possible?

**Yes, in principle.** The technique is known and has been demonstrated on phones. The open question is whether browser audio stacks will cooperate.

### Why the obvious approach doesn't work

The obvious approach: device A plays a sound at a known time, device B notes when it hears it, and distance = speed of sound × time of flight.

Sound travels at about 343 m/s, so **1 ms of timing error is about 34 cm of distance error**. The obvious approach would need:

- **Clocks synchronised across devices to well under a millisecond.** NTP-style ping/pong over Wi-Fi and WebSockets is rarely better than a few milliseconds, which is a metre or more of error.
- **Each device's audio output and input latency**: the time from "play this now" to sound leaving the speaker, and from sound reaching the mic to samples reaching JavaScript. These are tens of milliseconds, differ by device, OS and browser, and browsers don't report them reliably.

Either error on its own swamps the measurement.

### The trick: two-way acoustic ranging

The "BeepBeep" system (Peng et al., SenSys 2007) solved this for phones of that era and reported centimetre-level ranging. The key idea is that **every device records continuously, including the sounds it plays itself**, and only ever measures time intervals *within its own recording*, by counting samples.

For two devices A and B:

1. A plays a chirp. Both A and B record it.
2. A little later, B plays a chirp. Both record it.
3. A measures Δ_A, the time between its own chirp and B's chirp in A's recording.
4. B measures Δ_B, the time between A's chirp and its own chirp in B's recording.

Then:

```
distance(A, B) = c/2 × (Δ_A − Δ_B) + (d_AA + d_BB)/2
```

Here `c` is the speed of sound, and `d_AA` and `d_BB` are the distances between each device's own speaker and microphone. Those are a few centimetres on a phone, more on a laptop, and are a small source of error.

The awkward unknowns all cancel:

- *When* each device actually played its chirp. No shared clock is needed.
- **Output latency.** Each device hears its own chirp the moment it leaves the speaker.
- **Input latency.** It is constant within one recording, so it cancels when you subtract two times from the same recording.

What remains is counting samples. At 48 kHz one sample is about 7 mm.

### From distances to a map

If each of N devices chirps once in turn and every device hears every chirp, that gives all N(N−1)/2 pairwise distances from only N chirps. Turning pairwise distances into 2D coordinates is a standard problem: multidimensional scaling followed by least-squares refinement.

- 3 devices is the minimum for a 2D layout. 4 or more gives redundancy, so a bad measurement can be spotted and thrown out.
- Distances only fix the layout up to translation, rotation and **mirror image**. That doesn't matter for the maths, but the map I'm shown has to match the real room, so the UI needs "rotate" and "flip" controls, or a way to say "Blue is next to the door". An "identify" button that makes one device chirp or flash would help.

### Bonus: the same chirps synchronise the recordings

Locating the beep means comparing arrival times across devices, which needs a shared timeline. Once the distances are known, the arrival time of each calibration chirp in each recording gives the offset between every pair of recording streams. So calibration produces both the map *and* the clock sync, measured acoustically rather than over the network.

### Risks to test early

- **Echo cancellation would erase a device's own chirp** from its recording. The page must request `echoCancellation: false`, `noiseSuppression: false` and `autoGainControl: false`, then check that the browser actually honoured them. Safari has historically been inconsistent here.
- **Playing loudly while recording on iOS.** iOS has a history of lowering output volume or changing audio routing while the microphone is open.
- **Gaps in the sample stream.** Counting samples assumes an unbroken stream. Dropped buffers (from CPU load, a backgrounded tab or a sleeping screen) would silently corrupt measurements, so capture needs to detect discontinuities.
- **Reverberation.** Reflections arrive a few milliseconds after the direct sound, and can be louder than it if the direct path is blocked. Detection has to find the *first* arrival, not the strongest one.
- **Speaker and mic quality** varies a lot. Chirps need to sit in a band every device can play and record. Somewhere around 2–8 kHz is probably safe. Near-ultrasonic chirps would be less annoying but less reliable.

## Locating the beep

With known listener positions and synchronised recordings, this is classic time-difference-of-arrival (TDOA) multilateration: find the point (x, y) and emission time t₀ that best explain the arrival times at each device. 3 listeners make it solvable, and 4 or more allow an error estimate.

What makes *this* sound difficult:

- **We don't know what it sounds like in advance.** One option is to detect bursts of energy in a frequency band. Another is to let me "teach" it: when the first beep happens, I mark it in the UI and the system uses that recording as a template for matching later beeps.
- **Beeps are poor timing signals.** Calibration chirps can be designed to give a sharp correlation peak. A typical alarm beep is a near-pure tone (smoke alarms are often around 3 kHz), and its correlation repeats every cycle, which is about 11 cm at 3 kHz. Timing will mostly come from the beep's onset, which reverberation smears. Expect locating the beep to be noticeably less precise than calibration.
- **Clocks drift.** Device audio clocks differ by tens of parts per million. At 50 ppm, one minute of drift is 3 ms, about a metre. Recordings need re-syncing close to each beep: periodic sync chirps, a quick sync round triggered right after each detected beep, or a drift rate estimated from repeated rounds.
- **Beeps repeat.** A smoke alarm might chirp once a minute or less often. Combining many beeps should converge on an answer, and the map should show a confidence region that shrinks over time rather than one falsely precise dot.
- **The room is 3D.** A smoke alarm on the ceiling or something under the sofa is at a different height from the listeners, and so are listeners on a shelf versus the floor. A 2D floor-plan answer is what I want, but height differences add error to both calibration and localisation.
- **Geometry matters.** Accuracy is best when the beep is inside the area enclosed by the listeners, and worst when the listeners are bunched together or in a line. The app should nudge me to spread devices around the room.

### Fallback: loudness

Even if precise timing fails, a much simpler signal is useful: **which device heard it loudest?** Each listener can report the level of each beep. Gain control will be off, but mic sensitivity still differs between devices, so this is rough. A "warmer/colder" mode, where I walk around holding one phone that shows a live meter for the beep's frequency band, may be what actually finds the thing.

This is cheap to build and should be there from the start.

## Platform constraints

- **HTTPS is required for microphone access** and for AudioWorklet. `localhost` counts as secure, but a phone loading `http://192.168.x.x` gets no microphone. Options are a self-signed certificate, a locally trusted certificate (e.g. mkcert) or a tunnel. Whichever is chosen must be painless to set up.
- **Audio needs a user gesture** to start, hence the "Start listening" tap.
- **Devices must stay awake.** Use the Screen Wake Lock API, and warn on the coordinator page if a listener's tab gets backgrounded.
- **Raw sample access** through an AudioWorklet, with a running sample count acting as each device's clock.
- **Sample rates differ** (44.1 kHz on some devices, 48 kHz on others), and the processing must handle that.
- **Bandwidth.** Raw mono 16-bit audio at 48 kHz is about 100 KB/s per device, which is trivial on home Wi-Fi for a handful of devices. That might make server-side processing in Python (numpy/scipy) the simplest design. The alternative is processing on each device and sending only detections.

## Success criteria

- Getting from "start the server" to "all devices listening" takes a couple of minutes, with no typing on phones.
- Calibration needs no tape measure, and with 3–6 devices in a typical room the distances between devices on the map are within roughly 10–20 cm of reality.
- After a few beeps the estimate narrows the search to about half a metre: "it's in that corner, on the shelf" rather than "it's somewhere in the room".
- When it can't give a confident answer it says so, and the loudness fallback still helps.

## Non-goals (for now)

- Multiple rooms, or working out which room the beep is in.
- Full 3D localisation.
- Native apps.
- Tracking sound sources that move.
- Accounts, persistence, or hosting on the public internet.

## First experiments

Before building the whole app, test the unknowns:

1. **Loopback**: one device plays a chirp while recording with echo cancellation off. Does the chirp show up cleanly in its own recording? Try every browser/OS combination available: desktop Chrome, desktop Safari, iOS Safari, Android Chrome.
2. **Two-device ranging**: two devices, BeepBeep-style, at 1 m, 2 m and 4 m measured with a tape measure. How accurate and how repeatable are the estimates?
3. **Stream continuity and drift**: record for 10 minutes. Are any samples dropped? How far do two devices' sample clocks drift apart?
4. **Beep timing**: record a known beeper with 3–4 calibrated devices and see how precisely its onset can be timed.

## Open questions

- Should audio be processed on the server (stream everything) or on the devices (send detections)? A hybrid is possible: devices keep a rolling buffer and the server asks for clips around interesting moments.
- Calibration chirp design: frequency band, duration, audible or near-ultrasonic, devices taking turns or chirping at the same time with distinguishable signals.
- The least painful way to get HTTPS on a local network.
- How the UI should let me orient the map to match the real room.
- Which Python web framework. It needs async and WebSockets.

## References

- Chunyi Peng, Guobin Shen, Yongguang Zhang, Yanlin Li, Kun Tan. [BeepBeep: A High Accuracy Acoustic Ranging System using COTS Mobile Devices](https://www.cs.purdue.edu/homes/chunyi/pubs/sensys106-beepbeep.pdf). SenSys 2007.
