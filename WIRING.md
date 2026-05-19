# Wiring Guide

GPS module to Raspberry Pi 4 GPIO header.

## Pinout

```
            Pi 4 GPIO Header (40-pin)
            
        +3.3V  [ 1] [ 2]  +5V
       GPIO 2  [ 3] [ 4]  +5V    <----  GPS VCC (5V)
       GPIO 3  [ 5] [ 6]  GND    <----  GPS GND
       GPIO 4  [ 7] [ 8]  GPIO14 (TX)  --->  GPS RX
          GND  [ 9] [10]  GPIO15 (RX)  <---  GPS TX
      GPIO 17 [11] [12]  GPIO18         <---  GPS PPS
       GPIO 27 [13] [14]  GND
       GPIO 22 [15] [16]  GPIO 23
        +3.3V [17] [18]  GPIO 24
       GPIO 10 [19] [20]  GND
       GPIO  9 [21] [22]  GPIO 25
       GPIO 11 [23] [24]  GPIO  8
          GND [25] [26]  GPIO  7
        (...)
```

## Connection table

| GPS module pin | Pi physical pin | Pi GPIO | Function |
|---|---|---|---|
| VCC | 4 | - | 5V power |
| GND | 6 | - | Ground |
| RX  | 8 | GPIO 14 | UART TX (Pi sends to GPS) |
| TX  | 10 | GPIO 15 | UART RX (Pi receives from GPS) |
| PPS | 12 | GPIO 18 | Pulse-per-second input |

## Important notes

**RX/TX are swapped between devices.** The Pi's TX (pin 8) connects to the GPS module's RX. The Pi's RX (pin 10) connects to the GPS module's TX. This is correct, not a mistake.

**Some GPS modules want 3.3V instead of 5V.** Check your module's datasheet. The GT-U7 accepts both. If unsure, use pin 1 (3.3V) instead of pin 4 (5V).

**The PPS pin output is typically a 3.3V CMOS signal.** Safe to connect directly to a Pi GPIO input. Pulse width varies by module: 1ms to 200ms is typical.

**Loose wires cause silent failures.** A loose PPS wire will float and pick up noise, producing what looks like random pulses to the kernel. If you see PPS firing more than once per second in `ppstest /dev/pps0` output, suspect wiring first.

## Antenna placement

The most important factor for time accuracy is satellite reception. The GPS module's chipset doesn't matter much if it can only see 3 satellites.

Best to worst:
1. **Outdoor mount with clear sky view** - 10+ satellites, SNR >35
2. **South-facing window, exterior wall** - 6-10 satellites, SNR 25-35
3. **North-facing window** - 4-7 satellites, SNR 20-30 (poorer because most GPS satellites pass to the south at northern latitudes)
4. **Interior room near window** - 2-4 satellites, SNR 15-25 (often insufficient for fix)
5. **Anywhere without exterior wall** - usually no fix

If you can't get good reception indoors, an external active antenna with SMA connector and 5+ meter cable lets you mount the antenna outside while keeping the Pi inside. Total cost is typically $15-25.

## Common wiring issues

**No NMEA data from gpsd**: TX/RX are swapped. Verify Pi pin 8 (Pi's TX) goes to the GPS module's labeled RX pin.

**NMEA works but no PPS**: PPS wire isn't connected, or connected to the wrong GPIO pin. The kernel module is bound to GPIO 18 (Pi pin 12) by the dtoverlay line. If you wired PPS to a different pin, change the overlay accordingly or move the wire.

**PPS fires erratically**: Loose connection at either end. Re-seat. If using a breadboard, suspect the breadboard. Solder a proper connection if the problem persists.

**Voltage on PPS pin without GPS fix**: Some modules pulse PPS at a default rate (1Hz, 10Hz, or floating) before they get a fix. This is normal. After the module gets a real fix, PPS aligns to the actual UTC second.
