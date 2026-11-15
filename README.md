# slotwise

Prototype: Check a used GPU against its listing – slotwise

**Use it free, no account:** https://app-slotwise.manykiln.com/  
**Try the demo (made-up data):** https://try-slotwise.manykiln.com/  
**Product page:** https://slotwise.manykiln.com/

> Built and maintained by AI agents at [Manykiln](https://manykiln.com), a studio whose products are written, tested and operated by AI agents. This is a working prototype, not a finished product. No payment is taken anywhere: any checkout, waitlist or payment code in this project runs against fakes only, and the hosted demo uses made-up data.

- Status: prototype; run the 28 automated tests yourself with `python3 -m unittest discover`
- Runs on the Python 3.12 standard library; run the usable app with `python3 app.py` (see below)
- Licence: MIT. Free to use, fork and improve.
- Issues and pull requests are not monitored yet.

---

# CardCheck MVP

CardCheck is a buyer-run, read-only used-NVIDIA-GPU delivery check. It compares listing claims, device identity, idle samples, a small reference table and an optional untrusted seller baseline. It exports canonical JSON with an accidental-change digest and a standalone printable HTML record. It generates no GPU load.

Requires Python 3.12 and its standard library only. No installation or external assets are required.

## Usable app

Set a persistent writable data directory and the public URL, then run:

```sh
DATA_DIR=./cardcheck-data PUBLIC_BASE_URL=http://127.0.0.1:8080 python3 app.py
```

Open `http://127.0.0.1:8080`. Production may additionally set `APP_HOST`, `PORT`, `APP_HTTPS_TERMINATED` and `APP_DATA_CAP_MB`. `GET /healthz` returns `ok`.

Start a private workspace and bookmark its URL. The URL token is the only key and cannot be recovered. The token is never stored: its SHA-256 digest names the workspace directory. Checks survive restarts, can be exported together, and can be deleted individually or with the entire workspace. Deletion writes a tombstone so an in-flight check cannot recreate the workspace.

Paste or upload one of these locally generated inputs:

```sh
nvidia-smi -q
nvidia-smi --query-gpu=name,pci.device_id,pci.sub_device_id,memory.total,vbios_version,serial,driver_version,pcie.link.gen.max,pcie.link.width.max,pcie.link.gen.current,pcie.link.width.current,temperature.gpu,power.draw,fan.speed,clocks.sm,clocks.mem,clocks_event_reasons.active --format=csv -l 1
```

Run the CSV command for about 60 seconds and stop it with Ctrl+C. The app also accepts `cardcheck.record/1` CLI JSON and unverified `cardcheck.baseline/1` seller files. Each field is capped at 256 KB and each request at 1 MB. Raw pastes and uploads are never stored.

Results use deterministic rules R0–R11 and the bundled reference table; there is no AI step, network access, hardware access, analytics, account, email capture, payment flow or share link. Seller files remain unverified. Downloads include canonical JSON with an accidental-change SHA-256 digest, printable self-contained HTML, and a seller baseline.

Workspaces hold at most 100 checks and 5 MB, allow at most 30 new checks per hour, and expire after 90 idle days. Empty workspaces expire after 24 hours. The service admits at most 3,000 workspaces, 60 new workspaces per hour and the configured global data cap. A startup and daily sweep enforce expiry.

The private URL is a bearer secret. Do not enter other people's names, addresses, emails, phone numbers or order numbers. Keep downloaded records private and keep your own copy of anything important.

## Hosted demo

```sh
python3 demo.py
```

Open `http://localhost:8080`. The legacy demo remains unchanged and uses only made-up scenarios, an in-memory 30-minute store, fake telemetry, fake payment links and a write-only fake vault. Nothing is charged.

## CLI

```sh
python3 cli.py --scenario honest-3090 --samples 1 --out record.json
python3 cli.py --scenario honest-3090 --make-baseline --out baseline.json
python3 cli.py --scenario honest-3090 --baseline baseline.json --out record.json --force
```

For an installed NVIDIA GPU on Windows:

```sh
python3 cli.py --source nvml --model "RTX 3090" --vram 24 --partner EVGA --out record.json
```

The CLI's NVML adapter is read-only and generates no load. It has not been validated on physical Windows hardware here. Hardware validation and NVML licence review remain release gates; no hosted CLI binary is offered.

## Tests

```sh
python3 -m unittest discover -s tests
python3 -m unittest tests.test_e2e_demo
```

The suite uses standard-library HTTP clients, temporary storage, fake device data and loopback sockets. No external service or GPU is required.

## Limitations and release gates

This buyer-generated record does not prove condition at shipment, detect physical tampering such as die-stripping, re-engraving or swapped memory, or cover faults that appear only under load. No marketplace or manufacturer acceptance is claimed. Even running a read-only tool may count as alteration under marketplace rules; check those rules before using diagnostics.

The digest detects accidental changes to the canonical JSON payload. It is not a signature, certification or tamper-proof seal. There are no overall pass/fail badges. Reference rows that have not received human review remain not checkable. AMD and Intel cards, load testing, hosted binaries, accounts, sharing, payments and outbound network fetches are out of scope.
