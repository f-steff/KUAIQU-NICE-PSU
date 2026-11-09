# KUAIQU-NICE-POWER SPPS Series - Serial Protocol Reference

> **Upstream source:** [f-steff/KUAIQU-NICE-PSU](https://github.com/f-steff/KUAIQU-NICE-PSU)
> (`scripts/kuaiqus_psu_protocol.md`). This file is copied into other repositories
> together with the `scripts/` and `skills/` files it documents. The GitHub
> repository is authoritative: fix issues there and re-copy, so the mirrors do
> not drift apart.

This document is the working reference for the ASCII serial protocol used by the
KUAIQU-NICE-POWER SPPS series programmable DC power supply. The published
information is scarce and inconsistent, so it was pieced together from the
vendor manual (Borui Communication Technology Co., Ltd., 2022-12-30),
frame-by-frame analysis of that manual's examples, the vendor PC software's
formatting, various sources and forums around the web, and bench verification
against a real SPPS3010D unit. Treat it as community knowledge rather than
official manufacturer specifications, and prefer the bench-verified findings
where sources disagree.

The implementing codec lives in `scripts/kuaiqus_psu_protocol.py`; this document
is the human-readable source of truth that the codec is built and tested against.

> **Bench-verification status:** verified against a real SPPS3010D unit and
> cross-checked with the vendor PC software. The milli-unit value encoding below
> is authoritative; a few manual claims are wrong and several function codes were
> undocumented. See [Verified findings and manual
> discrepancies](#verified-findings-and-manual-discrepancies).

---

## Transport

The instrument was sold as supporting **RS232**, **USB (CP2102 serial)**, and
**RS485**. RS232 and USB are confirmed; the RS485 path is unconfirmed. All three
are assumed to speak the same 8N1 ASCII protocol and to look like a plain serial
COM port to software.

| Parameter | Value |
| --- | --- |
| Baud rate | 1200, 2400, 4800, 9600, 19200 - **default 9600** |
| Start bits | 1 |
| Data bits | 8 |
| Parity | none |
| Stop bits | 1 |

The backend deasserts RTS and DTR before opening the port. On many RS-485
adapters RTS is the transmit-direction control; leaving it asserted keeps the
driver enabled and the reply is never heard. USB and RS-232 links are unaffected.

---

## Frame structure

Every message is an ASCII line delimited by `<` (`0x3C`) and `>` (`0x3E`) with
exactly **11 payload characters** between them:

```text
< [0] [1] [2] [3] [4] [5] [6] [7] [8] [9] [10] >
  |    |   +-------------------------------------+
  |    |      9-character data field
  |    +-- function code (1..9)
  +-- address / direction
```

| Position | Field | Meaning |
| --- | --- | --- |
| 0 | `<` | start of frame |
| 1 | address / direction | `0` = PC -> device ("broadcast"), `1` = device -> PC (CV), `C` = device -> PC (CC) |
| 2 | function code | `1`..`9`, see below |
| 3-11 | data field | 9 ASCII characters (fixed width, zero padded) |
| 12 | `>` | end of frame |

### Timing

- A message must be preceded by a pause of at least **3.5 character times**.
- The entire frame must be sent as one continuous stream. A pause of more than
  3.5 character times inside a frame causes the receiver to flush the partial
  frame and treat the next byte as the address of a new message.
- A new message starting less than 3.5 character times after the previous one is
  treated as a continuation of the previous message.

These rules are satisfied in software by writing each complete frame in a single
`write()` call and parsing responses with a `<` ... `>` stream parser (see
`FrameParser` in the codec).

### Address / direction field (position 1)

The vendor manual states (via the hex examples) that ASCII `0` (`0x30`) is the
default **broadcast address** and `1` (`0x31`) is **data returned by the MCU**.

The same field is overloaded on read responses to carry the regulation state:

| Char | Direction | Meaning |
| --- | --- | --- |
| `0` | PC -> device | command |
| `1` | device -> PC | response, CV state (constant voltage) |
| `C` | device -> PC | response, CC state (constant current) |

The CV/CC state is **automatic** (load-determined) and is only reported, never
explicitly selected over serial.

---

## Function codes

| Code | Name | Direction | Data field |
| --- | --- | --- | --- |
| `1` | Set voltage | PC -> device | target voltage (mV) |
| `2` | Read voltage | PC -> device | query (zero filled) |
| `3` | Set current | PC -> device | target current (mA) |
| `4` | Read current | PC -> device | query (zero filled) |
| `5` | *undocumented* | PC -> device | no response (appears unused) |
| `6` | Read firmware version | PC -> device | returns version string (e.g. `V1.3`) |
| `7` | Power on | PC -> device | zero filled |
| `8` | Power off | PC -> device | zero filled |
| `9` | Remote / local control mode | PC -> device | `1` = remote, `2` = local |

---

## Value encoding (milli-units)

The 9-character data field carries a numeric value as **six right-aligned,
zero-padded digits in milli-units, followed by three literal `"000"` padding
characters**:

- **Voltage is in millivolts (mV)** - divide by 1,000 for volts.
- **Current is in milliamperes (mA)** - divide by 1,000 for amperes.

This matches the vendor PC software's `{:07.3f}` formatting (`8.0` -> `008.000` ->
`008000`) with `"000"` appended. Resolution is therefore 0.001 V / 0.001 A.

| Value | Milli-units | Encoded field | Frame |
| --- | --- | --- | --- |
| 4.580 V | 4,580 mV | `004580000` | `<02004580000>` |
| 12.000 V | 12,000 mV | `012000000` | `<12012000000>` |
| 12.100 V | 12,100 mV | `012100000` | `<01012100000>` |
| 0.183 A | 183 mA | `000183000` | `<14000183000>` |
| 6.92 A | 6,920 mA | `006920000` | `<03006920000>` |
| 3.3 A | 3,300 mA | `003300000` | `<04003300000>` |

The field is **fixed width**: the value is zero-padded on the left to six digits,
and the last three characters are always `"000"`.

---

## Command reference

### Set voltage - function `1`

```
PC:     <0 1 <6-digit mV>000>
Device: <1 1 OK 0000000>
```

Example - set 4.58 V:

```text
TX: <01004580000>
RX: <11OK0000000>
```

### Set current - function `3`

```
PC:     <0 3 <6-digit mA>000>
Device: <1 3 OK 0000000>
```

Example - set 6.92 A:

```text
TX: <03006920000>
RX: <13OK0000000>
```

### Read voltage - function `2`

```
PC:     <0 2 000000000>
Device: <1 2 <6-digit mV>000>   (CV)   or   <C 2 <6-digit mV>000>  (CC)
```

Example - read 4.58 V in CV state:

```text
TX: <02000000000>
RX: <12004580000>
```

> The response reports the **actual measured output**, not the configured setpoint.
> With the output off it returns 0; after enabling the output it returns the live
> output value. The CV/CC direction character (`1`/`C`) reflects the regulation
> state at the time of the read.

### Read current - function `4`

```
PC:     <0 4 000000000>
Device: <1 4 <6-digit mA>000>   (CV)   or   <C 4 <6-digit mA>000>  (CC)
```

Example - read 0.183 A in CV state:

```text
TX: <04000000000>
RX: <14000183000>
```

> In CV state (`1`) the value is the actual load current (0 A with no load); in CC
> state (`C`) it is the current limit that is being enforced.

### Read firmware version - function `6` (undocumented)

The vendor function table lists codes `5` and `6` without describing them. On the
real device:

```
PC:     <0 6 000000000>
Device: <1 6 V1.300000>
```

Function `6` returns the firmware version. The data field is not a number here but
a literal string, observed as `V1.300000` (firmware "V1.3") on the test unit.

Function `5` produces **no response** for any tested data field and appears unused.

### Power on / off - functions `7` / `8`

```text
Power on:   <07000000000>
Power off:  <08000000000>
```

No acknowledgement is documented for these commands.

### Remote / local control mode - function `9`

The first data character selects the mode; the remaining eight are zero.

```text
Remote control mode:  <09100000000>
Local control mode:   <09200000000>
```

> **Firmware difference.** On the V1.3 bench unit these frames are silent. A
> V3.00 SPPS-S305-232 acknowledges the documented 091/092 forms with
> `<19OK0000001>` / `<19OK0000000>`; the vendor PC app's `<09000000000>`, used
> for both link and unlink, is silent there. Do not depend on an acknowledgement
> either way.

The vendor manual shows remote mode being entered before the first set commands,
and the vendor PC software sends it before any set command ("needed for set
commands"):

```text
PC:  <09100000000>   enter remote control mode
PC:  <01004580000>   set voltage 4.58 V
PC:  <03006920000>   set current 6.92 A
```

---

## Response acknowledgements

Successful **set** commands are acknowledged with a literal `OK` prefix in the
data field:

| Command | Acknowledge |
| --- | --- |
| Set voltage | `<11OK0000000>` |
| Set current | `<13OK0000000>` |

The function code is echoed, the direction field is `1`, and the data field is
`OK` followed by seven zeros.

---

## Worked examples from the manual

### Read voltage, CV state

```
TX: <02000000000>
RX: <12004580000>      -> 4,580 mV = 4.580 V, CV
```

### Read voltage, CC state

```
RX: <C2012000000>      -> 12,000 mV = 12.000 V, CC
```

### Read current, CV state

```
RX: <14000183000>      -> 183 mA = 0.183 A, CV
```

### Zero / off states

```
RX: <12000000000>      -> 0 V, CV
RX: <C4000000000>      -> 0 A, CC
```

---

## Verified findings and manual discrepancies

The following was confirmed by driving a real SPPS3010D unit and cross-checking
with the vendor PC software. The original manual wording is quoted where it is
wrong, followed by the observed reality.

1. **Units are milli, not micro.**
   The manual's examples are consistent with a **millivolt/milliamp** value field
   (six digits + `"000"`), not a micro-unit field. The vendor software confirms
   this (`{:07.3f}` formatting). Resolution is 0.001 V / 0.001 A.

2. **"9.3 A" current example is wrong (off by 10x).**
   The manual states `Read current ... <14009300000>` is "9.3 A". The milli-unit rule
   decodes `000930` as 930 mA = **0.93 A**. Verified: the mA encoding round-trips
   exactly (set 0.5 A -> read 0.5 A; set 1.00 A -> read 1.00 A), so the manual's
   "9.3 A" label is a typo for 0.93 A.

3. **"12.100100 V" example is wrong.**
   The manual states `set voltage <01012100100>` is "12.100100 V". The correct
   reading is `012100` = **12.100 V**; the manual's trailing `100` is a typo for
   `000`. Verified: the mV encoding round-trips exactly (set 3.00 V -> read 3.00 V;
   5.00 V -> 5.00 V; 12.00 V -> 12.00 V).

4. **Read commands return the measured output, not the dial/setpoint.**
   The manual describes read voltage/current as "reading the dial" value. In
   practice the responses carry the live measured output (0 with the output off,
   and the actual value once enabled). Reading with the output off is therefore not
   a way to read back a setpoint.

5. **Query data field is ignored.**
   The manual shows read queries carrying non-zero data (`<02012200000>`,
   `<04003300000>`). The device ignores that data; zero-filled queries
   (`<02000000000>`, `<04000000000>`) return the same response. The implementation
   sends zero-filled queries.

6. **Function `6` = firmware version; function `5` = unused.**
   Both were blank in the manual. Function `6` returns `V1.300000`; function `5`
   returns nothing for every tested data field. See the command reference above.

7. **Set commands are acknowledged; the rest are normally silent.**
   Set voltage/current return `<11OK0000000>` / `<13OK0000000>`. Power on/off
   and the function-9 mode switch return no response on the V1.3 unit. A V3.00
   unit also acknowledges the documented 091/092 forms with `<19OK0000001>` /
   `<19OK0000000>`, so do not depend on silence.

8. **Remote mode is required for set commands.**
   The vendor software sends `<09100000000>` (remote control mode) before any set
   command; treat it as a required prerequisite for setting voltage/current, and
   send `<09200000000>` to return the device to front-panel (local) control.

9. **Output ramp and no-load discharge.**
   The output ramps up over roughly a second after enable (soft-start), and with no
   load attached the output capacitor discharges slowly when the setpoint is
   lowered, so a read taken immediately after lowering the voltage can still report
   the previous, higher value. This is a hardware characteristic, not a protocol
   quirk; allow the output to settle before asserting a value.

10. **CC<->CV changeover leap.**
    Near the CC<->CV changeover the measured voltage can appear to leap by about
    30 mV instantly (reported by the field and consistent with this supply family).

---

## Firmware variants and field reports

The protocol is shared across the SPPS family, but firmware generations differ:

| Model | Output | Firmware | Notes |
| --- | --- | --- | --- |
| SPPS3010D | 30 V / 10 A | V1.3 (`V1.300000`) | Bench unit for this reference |
| SPPS-S305-232 | 30 V / 5 A | V3.00 | Same protocol; reported from another bench |

On the healthy SPPS3010D bench unit, function `2` returned `10.000 V` and
`25.000 V` with the output enabled in CV, matching the commanded values (a
multimeter on the terminals confirmed both points). This codec applies no scale
factor: the raw milli-unit value is used directly. The complete frames captured
on the bench were:

```text
TX <02000000000>   RX <12010000000>   -> 10,000 mV = 10.000 V (CV)
TX <06000000000>   RX <16V1.300000>   -> firmware V1.300000 = V1.3
TX <02000000000>   RX <12025000000>   -> 25,000 mV = 25.000 V (CV)
```

A second bench reported an SPPS-S305-232 whose front panel and function `2`
readback were both about **9.3x low with no offset** (31 V displayed as 3.34 V),
while the output itself regulated accurately. That is consistent with lost
voltmeter gain calibration, not a protocol scaling difference. No software
correction is applied here, and none should be added without the vendor's
calibration procedure.

### Undocumented function codes - do not probe

On the affected SPPS-S305-232, function codes `C`, `E`, `F`, `H`, and `I`
returned `OK` and behaved as writes. One of them is suspected of having altered
the voltmeter calibration; the exact payloads were not recorded.

**Never send function codes outside `1`, `2`, `3`, `4`, `6`, `7`, `8`, `9`.**
The codec deliberately exposes only documented commands, and `5` is known to be
dead. No calibration or service commands are documented.

### Address vs padding (bench evidence)

This reference treats the last three data characters as literal `000` padding.
An alternative reading is a 3-digit device address. Normal traffic uses `000`,
where both interpretations produce identical bytes.

A read-only bench test on the SPPS3010D (V1.3) varied only that tail:

```text
TX <02000000000>   RX <12000000000>   (tail 000, output off)
TX <02000000001>   RX <12000000000>   (tail 001)
TX <02000000002>   RX <12000000000>   (tail 002)
TX <02000000100>   RX <12000000000>   (tail 100)
TX <02000000255>   RX <12000000000>   (tail 255)
TX <06000000000>   RX <16V1.300000>   (tail 000)
TX <06000000001>   RX <16V1.300000>   (tail 001)
```

Every tail produced the same response, so **the trailing digits are ignored on
read queries on this firmware**. A safe write test agrees: setting 0 V with the
output off produced `<11OK0000000>` for tails `000`, `001`, and `255`. On this
unit, a non-zero tail is not a way to silence either the read or the write path.

The same field behaves differently on a V3.00 SPPS-S305-232: its read replies end
`...001` while remote control is linked and `...000` after unlink, and the
documented 091/092 forms are acknowledged with `<19OK0000001>` /
`<19OK0000000>`. So in the reply direction the field tracks remote/local state,
not a device address. The vendor PC software's "485 Add" box still suggests the
field is a device address in RS-485 multi-drop requests; that use is untested
here. This codec always sends `000` and implements no addressing.

### If the unit goes silent

1. **Check the baud rate first.** The device supports 1200, 2400, 4800, 9600,
   and 19200 baud (default 9600). A mismatch is the most likely cause of silence.
2. **Check the port and driver.** Confirm the CP2102 port with `-ListPorts`, and
   make sure no other process (including a second backend) owns it.
3. **Address scan (last resort).** The V1.3 bench unit ignores the trailing
   digits on both reads and writes, so a changed address is not a supported
   failure mode there. The V3.00 unit uses the reply tail as a link-state flag,
   which is not an address either. If the vendor confirms that RS-485 multi-drop
   uses the field as a device address, the bounded search is 5 baud rates x 256
   addresses - a few minutes of scripted probing. This tool does not implement
   address scanning. Stay within the documented function codes above; do not
   walk the function characters.

---

## Reference

- Vendor: Shenzhen Borui Communication Technology Co., Ltd.
- Document date: 2022-12-30
- Original title: Programmable regulated power supply communication protocol
