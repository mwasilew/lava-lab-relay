# lava-lab-relay

A tiny TCP relay, run as a LAVA **Test Services** container, that lets a LAVA
**docker test action** reach the lab services on the dispatcher's Docker network:

- the board's serial console (`ser2net`, per-board port, telnet)
- the TAC / Alpaca REST service (`tac-api:80`, pytactl routes)

Docker test containers run on the default bridge and cannot resolve those names. The
relay joins the dispatcher network (`lava-dispatcher_default`) and publishes one port
per service on the worker, which the test container reaches at the bridge gateway
(for example `172.17.0.1`).

```
test container ──> 172.17.0.1:17085 ──> relay ──> ser2net:<board port>
               ──> 172.17.0.1:17080 ──> relay ──> tac-api:80
```

## Use in a job

Start the relay early. Test services stay up until the job ends.

```yaml
- test:
    timeout:
      minutes: 200
    services:
    - name: relay
      from: git
      repository: https://github.com/mwasilew/lava-lab-relay.git
      path: docker-compose.yml
```

Then drive the board from a docker test action. With `disconnect_connection: true`
LAVA releases the console, so the container can own it:

```yaml
- test:
    timeout:
      minutes: 100
    disconnect_connection: true
    docker:
      image: ghcr.io/foundriesio/lava-lmp-sign:main
    definitions:
    - from: git
      repository: https://github.com/mwasilew/lava-lab-relay.git
      path: examples/boot-loop.yaml
      name: boot-loop
      parameters:
        TAC_SERIAL: NNPMP28T002L
        BOOTS: "60"
```

The per-board console port is not known when the job is submitted. Set
`SER2NET_PORT` in the device dictionary or job `environment:` if it isn't `7085`.
LAVA writes `environment:` to `.env` at the repository root, which is why
`docker-compose.yml` lives there.

| Variable             | Default                                   | Meaning                          |
|----------------------|-------------------------------------------|----------------------------------|
| `SER2NET_NETWORK`    | `lava-dispatcher_default`                 | external network with the services |
| `SER2NET_HOST`/`PORT`| `ser2net` / `7085`                        | console endpoint                 |
| `TAC_HOST`/`PORT`    | `tac-api` / `80`                          | TAC REST endpoint                |
| `RELAY_CONSOLE_PORT` | `17085`                                   | worker port for the console      |
| `RELAY_TAC_PORT`     | `17080`                                   | worker port for the TAC API      |
| `RELAY_IMAGE`        | `ghcr.io/foundriesio/lava-lmp-sign:main`  | any image with `python3`         |

## Talking to the services from the container

- **Gateway IP**: read the default route from `/proc/net/route`. Don't hard-code
  `172.17.0.1`.
- **Console**: plain TCP to `gw:17085`. It is telnet, so strip IAC negotiation bytes
  (`\xff[\xfb-\xfe].` and `\xff[\xf0-\xfa]`).
- **TAC** (pytactl REST, boards keyed by serial):
  - `PUT /<serial>/quick/<method>` runs a quick method, such as `powerOn`,
    `powerOff` or `bootToEDL`.
  - `PUT /<serial>/command/<pin>` sets a pin. Send the body `{"value": 1}` with
    `Content-Type: application/json`. A bare `?value=` query gets **HTTP 415**.
  - Only drive pins that the board's `.tcnf` enables.
- Holding `kpd_pwr` for about 20 s makes the PMIC do a warm reset. With
  `qcom_scm.download_mode=full` on the kernel command line, that lands in crashdump
  (USB `05c6:900e`), and a `boot: qdl` action with `ramdump: true` collects the dump.
- A TAC `powerOff`/`powerOn` cuts the supply. That wipes the PMIC fault record
  (XBL prints `PM: xVdd reset`). To read a PMIC fault such as `OCP Occured`, power on
  with a short `kpd_pwr` press instead.

See `examples/boot_loop.py` for a complete client: console reader, TAC calls,
power-cycle loop and per-boot classification.

## Notes

- Don't use pytactl directly in the test container. The host's TAC service holds the
  FTDI GPIO interfaces (EBUSY), and detaching drivers from the FTDI UART interfaces
  breaks ser2net for the whole worker.
- The relay never interprets traffic, so it works for any TCP service on that network.
  To add one, extend `FORWARDS` in `docker-compose.yml`.
