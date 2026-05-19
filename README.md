# GPS-Disciplined Stratum 1 NTP Server on Raspberry Pi

A self-hosted, GPS-disciplined Stratum 1 time server running on a Raspberry Pi 4, with a live mission-control style web dashboard, PTP master support, and NTS (Network Time Security) authentication.

Built for accuracy, transparency, and learning. Achieves sub-microsecond offset and ~200ns PPS jitter using a $15 GPS module and the Pi's built-in GPIO PPS support.

https://ntp.pilg0re.net

## What it does

- **Stratum 1 NTP server** synchronized directly from GPS satellites
- **Hardware PPS discipline** via GPIO for nanosecond-grade precision
- **Mission-control web dashboard** with live satellite sky view, telemetry, and historical PPS jitter
- **PTP (Precision Time Protocol) master** for sub-microsecond LAN-local time distribution
- **NTS (Network Time Security)** server for cryptographically authenticated time over the internet
- **Auto-renewing TLS certificate** via acme.sh wildcard, synced from a separate reverse proxy host
- **CPU isolation** dedicating one Pi core to time-critical work
- **Internet NTP fallback** when GPS signal is lost

## Performance

Real numbers from a running instance:

| Metric | Value |
|---|---|
| Stratum | 1 |
| Reference | GPS via PPS |
| Typical last offset | 100-1000 nanoseconds |
| PPS sample standard deviation | ~200 ns |
| RMS offset | <1 microsecond |
| Root delay | 1 nanosecond (minimum reported) |
| Frequency stability | <0.05 ppm skew |
| CPU usage | <5% on isolated core |
| Cost | ~$90 total hardware |

## Hardware

- **Raspberry Pi 4 Model B** (4GB recommended)
- **GT-U7 GPS module** (NEO-6M based, ~$10-15 on Amazon)
- **Active GPS antenna** with U.FL connector (often included with the module)
- **MicroSD card** 32GB+
- **Case with passive cooling**

The GT-U7 is a budget module but produces clean PPS pulses. For better holdover or sub-100ns precision, consider a u-blox NEO-M8T or NEO-F9T timing-grade module.

## Wiring

GPS module to Pi 4 GPIO header:

| GPS Module | Pi Pin | Pi Function |
|---|---|---|
| VCC (3.3V or 5V) | 4 | 5V |
| GND | 6 | Ground |
| RX | 8 | GPIO 14 / UART TX |
| TX | 10 | GPIO 15 / UART RX |
| PPS | 12 | GPIO 18 |

See [WIRING.md](WIRING.md) for full details and notes on the bluetooth/UART conflict workaround.

## Architecture

```
+-------------+      +----------+      +---------+
| GPS Module  |--+-->| /dev/    |----->| chrony  |
| (NMEA)      |  |   | serial0  |      | refclock|
+-------------+  |   +----------+      | NMEA    |
                 |                     +---------+
                 |   +----------+
                 +-->| gpsd     |
                     +----------+
+-------------+      +----------+      +---------+
| GPS Module  |----->| /dev/    |----->| chrony  |
| (PPS pulse) |      | pps0     |      | refclock|
+-------------+      +----------+      | PPS     |
                                       +---------+
                                            |
                                            v
                                       +---------+
                                       | System  |
                                       | Clock   |
                                       +---------+
                                            |
                            +---------------+---------------+
                            |               |               |
                            v               v               v
                       +--------+      +--------+      +---------+
                       | NTP    |      | NTS    |      | PTP     |
                       | Server | <--- | KE     |      | Master  |
                       | UDP    |      | TCP    |      | UDP     |
                       | 123    |      | 4460   |      | 319/320 |
                       +--------+      +--------+      +---------+
```

## Quick start

See [INSTALL.md](INSTALL.md) for full step-by-step setup.

The short version:

1. Wire the GPS module to the Pi
2. Disable the bluetooth/UART conflict in `/boot/firmware/config.txt`
3. Install chrony, gpsd, linuxptp, pps-tools
4. Copy the example configs from `config/` and adjust for your environment
5. Deploy the dashboard with `dashboard/chrony-webpage.service`
6. (Optional) Configure NTS with a wildcard cert
7. (Optional) Convert other LAN clients with `scripts/convert-to-chrony.sh`

## Why I built this

I wanted a real Stratum 1 time reference on my home network, both for the technical satisfaction and to learn how GPS-based timing actually works at the systems level. Commercial Stratum 1 references start around $500 and don't teach you anything. A Pi with a $15 GPS module produces sub-microsecond accuracy and forces you to understand every layer of the stack.

The dashboard exists because every existing chrony monitoring tool is either ugly, requires Prometheus, or doesn't visualize the satellite constellation. Mine does all of the above in a single Python file with zero dependencies beyond the standard library.

## License

MIT. See [LICENSE](LICENSE).

## Acknowledgments

- The [chrony](https://chrony.tuxfamily.org/) project for being the cleanest NTP daemon to work with
- The [linuxptp](https://linuxptp.nwtime.org/) project for PTP support
- The [acme.sh](https://github.com/acmesh-official/acme.sh) project for sane TLS automation
