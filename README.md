# skyq-mcp

An MCP server for controlling Sky Q boxes on your local network: discovery, every remote key,
channel changes, power, opening apps, TV listings, your recordings list, and what's playing now.
Designed to run on a Raspberry Pi.

> Unofficial. Sky Q is a trademark of Sky UK Limited; this project isn't affiliated with Sky.

**Status:** working; not yet published on PyPI.

## Why?

A remote control makes you translate what you want into button presses: look up the channel
number, find the programme in the guide, remember where things are in the menus. An AI assistant
with this server can do that translation for you, because it can combine the box's controls with
everything else it knows or can look up: the box's own channel list, TV listings (the EPG) from
the web, and how the Sky Q menus are laid out.

These examples come from a real conversation with Claude using this server:

> **"Change channel on Sky to Sky Sports F1 UHD"**
>
> Your living room Sky box is now on Sky Sports F1 UHD, channel 407. The box confirmed the switch.

The assistant found the channel by name in the box's lineup (no need to know it's 407), tuned
to it, and checked the box had actually changed channel.

> **"Switch to whatever channel is currently showing The Office"**
>
> I've switched you to U&Gold HD on channel 110. Online TV listings show The Office (UK) on
> U&Gold from 10:20pm, so it should be partway through now. The box confirmed the channel change,
> but it doesn't report programme names, so I couldn't check the title from here.
>
> If you meant the US version, Comedy Central is on channel 112, but I couldn't find a reliable UK
> listing for it tonight. Just say and I'll switch.

The box can't search for a programme, but the assistant can: it looked up tonight's listings
online, found the channel, matched it to your box's lineup, and tuned. It also noticed the request
was ambiguous (UK or US version) and offered the alternative.

> **"Go to my recordings"**
>
> I've opened the Sky Q Home menu and gone to the third item, which is usually Recordings. I can't
> see your TV screen, so I couldn't confirm it worked.
>
> If your menu has been reordered and you've landed somewhere else, tell me what's on screen and
> I'll move up or down from there.

There's no "recordings" button on the remote. The assistant used what it knows about the Sky Q
menu layout to turn the request into a sequence of key presses, and was upfront that it can't see
the screen to check.

**TV listings as a fallback.** The assistant looks up what's on in its usual way (for example a
web search), which is fast. If that fails, the server can read Sky's own online TV guide: what's
on now and next, a channel's schedule for any day this week, and searching for a programme across
channels. That's slower, because Sky's guide is read one channel at a time.

**Recording from the guide.** "Record Gogglebox on Friday" or "series link the F1" become a
single booking of the exact programme from Sky's guide. The server won't double-book, checks the
booking appears on the box, and can cancel scheduled recordings (never finished ones).

**Apps open directly.** "Go to Apple TV" doesn't need any menu navigation: the server reads the
list of apps installed on your box, opens the right one by name, and checks it's on screen. That
works even for apps that aren't on the Apps tray.

**Menus are a last resort.** For screens with no direct command, the assistant can try a menu
route. Sky Q menus vary between boxes and the server can't see the screen, so built-in routes are
marked unverified and the assistant is told to check with you. Once you confirm a route works,
it's saved for your box and used from then on.

The server provides the reliable parts: finding your box on the network, sending keys with the
right timing, changing channel and opening apps and checking they worked, and reporting what's
on. The assistant brings the knowledge and the judgement.

## Quick start (on the Pi)

Get the code:

```bash
git clone https://github.com/JordanPerkins/skyq-mcp.git
```

```bash
cd skyq-mcp
```

Install it into its own virtual environment:

```bash
python3 -m venv ~/skyq-mcp-venv
```

```bash
~/skyq-mcp-venv/bin/pip install .
```

Find your box. Each box is listed with its current IP and any hostnames it has:

```bash
~/skyq-mcp-venv/bin/skyq-mcp discover
```

Save it under a name of your choice, using a hostname from the `hostnames` list
(e.g. `skyq.local`):

```bash
~/skyq-mcp-venv/bin/skyq-mcp boxes save living-room skyq.local
```

Saving by IP also works: the server looks up the box's hostname and stores that instead.

### How boxes are found after an IP change

Most home networks hand out IP addresses with DHCP, so a box's address can change (after a
power cut, for example). The server saves boxes by **hostname**, which it finds in one of two ways:

- **Your router's DNS**, if the router publishes names for DHCP devices (many home routers don't).
- **The box's own mDNS name.** Sky Q boxes announce themselves on the local network as
  `skyq.local`. With more than one box, mDNS gives the others different names (usually by adding
  a number). This needs no setup on the server machine.

When it needs a box, the server tries, in order: the saved hostname, then the last IP it saw,
then a search of the network for the box's unique ID. It always checks that the box answering
is the one you saved, so two boxes swapping names after a power cut can't mix them up.
Whenever it finds a box somewhere new, it updates the saved hostname and IP.

If `discover` shows no hostnames for your box, it's saved by IP with a warning. It will still
be found by searching the network, as long as multicast traffic reaches the box. To be safe,
give the box a fixed IP (a DHCP reservation) or a DNS name on your router, and save it by that.

If discovery finds nothing (some mesh Wi-Fi systems and VLAN setups block multicast), scan
your network range instead:

```bash
~/skyq-mcp-venv/bin/skyq-mcp discover --subnet 192.168.1.0/24
```

Start the server. On first run it creates a token and prints it once:

```bash
~/skyq-mcp-venv/bin/skyq-mcp serve
```

Connect a client, e.g. Claude Code (replace the IP and token):

```bash
claude mcp add --transport http skyq http://192.168.1.50:8765/mcp --header "Authorization: Bearer YOUR_TOKEN"
```

TV listings come from Sky's online guide, so the Pi needs internet access for those; everything
else works on your home network alone.

## Running as a service

To start the server automatically when the Pi boots and restart it if it stops, run it under
systemd. It runs as its own `skyq-mcp` user, with its saved boxes and token in
`/var/lib/skyq-mcp`. These steps use `sudo` and assume you're in the `skyq-mcp` folder you cloned on the Pi.

1. Create the service user and its data folder:

   ```bash
   sudo useradd --system --home-dir /var/lib/skyq-mcp --shell /usr/sbin/nologin skyq-mcp
   ```

   ```bash
   sudo install -d -o skyq-mcp -g skyq-mcp -m 700 /var/lib/skyq-mcp
   ```

2. Install the server into `/opt/skyq-mcp`:

   ```bash
   sudo python3 -m venv /opt/skyq-mcp
   ```

   ```bash
   sudo /opt/skyq-mcp/bin/pip install .
   ```

3. Create the token and save your box as the service user. The first command prints the token;
   keep it for your MCP client.

   ```bash
   sudo -u skyq-mcp /opt/skyq-mcp/bin/skyq-mcp --data-dir /var/lib/skyq-mcp token --generate
   ```

   ```bash
   sudo -u skyq-mcp /opt/skyq-mcp/bin/skyq-mcp --data-dir /var/lib/skyq-mcp discover
   ```

   ```bash
   sudo -u skyq-mcp /opt/skyq-mcp/bin/skyq-mcp --data-dir /var/lib/skyq-mcp boxes save living-room skyq.local
   ```

   Boxes saved with the quick-start commands above live in your own home folder, so the
   service doesn't see them; save them again here.

4. Install and start the service:

   ```bash
   sudo cp deploy/skyq-mcp.service /etc/systemd/system/
   ```

   ```bash
   sudo systemctl daemon-reload
   ```

   ```bash
   sudo systemctl enable --now skyq-mcp
   ```

Check it's running, and follow its log:

```bash
systemctl status skyq-mcp
```

```bash
journalctl -u skyq-mcp -f
```

The token is never written to the log. To see it again:

```bash
sudo -u skyq-mcp /opt/skyq-mcp/bin/skyq-mcp --data-dir /var/lib/skyq-mcp token --show
```

**Settings for the service** (port, TLS, etc. from the table below) go in `/etc/skyq-mcp.env`,
one `NAME=value` per line. Make it readable by root only, since it may hold secrets:

```bash
sudo install -m 600 /dev/null /etc/skyq-mcp.env
```

Then edit it and restart the service:

```bash
sudo systemctl restart skyq-mcp
```

For TLS, the certificate and key must be readable by the `skyq-mcp` user and outside home
folders (the service can't read `/home`), for example in `/etc/skyq-mcp/`.

**Updating:** reinstall and restart:

```bash
sudo /opt/skyq-mcp/bin/pip install --upgrade .
```

```bash
sudo systemctl restart skyq-mcp
```

Saving or forgetting boxes with the `boxes` command works while the service is running; it
picks up the change straight away.

## Optional: access from anywhere with Tailscale Funnel

[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) gives the server a public HTTPS address
(`https://<pi-name>.<tailnet>.ts.net`) without opening ports on your router. Funnel forwards
HTTP, which works because this server uses MCP's streamable HTTP transport (stdio servers can't
be exposed this way).

**Before you start:** Funnel makes the server reachable by **anyone on the internet**. The
bearer token is then the only thing protecting your TV, so:
- use a token generated by `skyq-mcp token --generate` (never a short or reused one);
- only give the token to MCP clients you trust;
- if you only need access from your own devices, use **Tailscale Serve** instead (step 4),
  which keeps the server private to your tailnet.

Your MCP client must be able to send an `Authorization: Bearer` header. Claude Code can (see
above). Some web-based clients only support OAuth and can't connect to this server.

1. Install Tailscale on the Pi and log in (see [tailscale.com/download](https://tailscale.com/download)),
   then in the Tailscale admin console enable HTTPS certificates and allow Funnel for this device.

2. Find the Pi's Tailscale name (the `DNSName` line, without the trailing dot):

   ```bash
   tailscale status --json | grep -m1 DNSName
   ```

3. Allow that name. Funnel passes the public `*.ts.net` name through to the server as the
   request's `Host` header, and the server refuses names it doesn't know (this protects against
   DNS-rebinding attacks). Add it to `/etc/skyq-mcp.env` (replace the example name):

   ```bash
   echo 'SKYQ_MCP_ALLOWED_HOSTS=pi.tail1234.ts.net' | sudo tee -a /etc/skyq-mcp.env
   ```

   Funnel connects to the server locally, so you can also stop it listening on your home
   network. Only do this if every client will use the Funnel address:

   ```bash
   echo 'SKYQ_MCP_HOST=127.0.0.1' | sudo tee -a /etc/skyq-mcp.env
   ```

   Then restart:

   ```bash
   sudo systemctl restart skyq-mcp
   ```

4. Publish the server. `--bg` keeps it running in the background and across reboots:

   ```bash
   sudo tailscale funnel --bg 8765
   ```

   Or, to keep it private to your tailnet instead:

   ```bash
   sudo tailscale serve --bg 8765
   ```

5. Check it's up (no token needed for the health check):

   ```bash
   curl https://pi.tail1234.ts.net/healthz
   ```

6. Connect your MCP client to the public address. Funnel handles HTTPS, so you don't need the
   TLS settings below:

   ```bash
   claude mcp add --transport http skyq https://pi.tail1234.ts.net/mcp --header "Authorization: Bearer YOUR_TOKEN"
   ```

To see what's published, or stop publishing:

```bash
tailscale funnel status
```

```bash
sudo tailscale funnel reset
```

If requests are refused with `Invalid Host header`, the name in `SKYQ_MCP_ALLOWED_HOSTS`
doesn't match the address the client is using. If you use a custom domain in front of Tailscale,
add that name too.

## Security

What the server protects against:
- **No access without the token.** Every request except `GET /healthz` needs the bearer token.
  WebSocket and any other non-HTTP traffic is refused. Tokens are compared in constant time,
  must be strong (at least 24 characters, not repetitive), and are never written to logs.
- **Browser attacks.** Requests from web pages (with an `Origin` header) are refused, and the
  `Host` header must be one of this machine's names or one you allow, which blocks DNS rebinding.
- **Being turned against your network.** The server only contacts Sky Q boxes on local network
  ranges (192.168.x.x, 10.x.x.x, 172.16–31.x.x, 169.254.x.x), plus one fixed internet address:
  Sky's TV guide (`atlantis.epgsky.com`, over HTTPS), for listings. It won't save a box on a public
  address, scan public ranges, or follow a hostname that starts pointing outside your network.
  A device on your network pretending to be a box can't redirect the server's requests to other
  addresses, and oversized replies are cut off.
- **Remote unpairing.** Key codes that open the remote-control pairing screens are blocked unless
  you start the server with `--allow-unsafe-keys`, including inside saved menu routes.
- **Parental controls.** Apps blocked on the box are refused. PIN-locked apps still ask for the
  PIN on the TV; the server never sees or enters it.
- **Leaking box secrets.** The box reports its serial number, viewing card number and a household
  token; the server never reads those fields, so they can't reach an MCP client.

**Dependencies** were audited on 2026-11-25 with `pip-audit` (PyPI and OSV databases). The
minimum versions in `pyproject.toml` are set so that no allowed version has a known
vulnerability, including packages that come in through the MCP SDK. To check your own install
again later:

```bash
/opt/skyq-mcp/bin/pip install pip-audit
```

```bash
/opt/skyq-mcp/bin/pip-audit
```

On 32-bit Raspberry Pi OS, `cryptography` may need to be built from source if no ready-made
package exists yet for the required version; 64-bit Raspberry Pi OS is recommended.

What it doesn't protect against:
- **Anyone who has the token** has full control of your saved boxes. Keep it secret, give it only
  to clients you trust, and replace it if it leaks (`token --generate --force`, then restart).
- **Plain HTTP on your home network.** Without TLS, someone who can watch your network traffic
  can read the token. Use the TLS settings, or Tailscale (Funnel/Serve handle HTTPS for you).
- **Other devices on your home network.** Sky Q boxes accept remote-control commands from any
  device on the network without a password, so anything on your LAN can already control the
  box directly. A device on the LAN can also pretend to be your box. This server can't change that.
- **Text from broadcasters.** Channel names and programme descriptions are shown to your AI client
  as data. They're length-limited and the client is told not to treat them as instructions, but
  a client that follows instructions found in tool results could still be misled by them.

## Settings

| Environment variable | Flag | Default |
|---|---|---|
| `SKYQ_MCP_TOKEN` / `SKYQ_MCP_TOKENS` (comma-separated) | | token file in the data dir |
| `SKYQ_MCP_HOST` | `--host` | `0.0.0.0` |
| `SKYQ_MCP_PORT` | `--port` | `8765` |
| `SKYQ_MCP_DATA_DIR` | `--data-dir` | `~/.local/share/skyq-mcp` |
| `SKYQ_MCP_ALLOWED_HOSTS` (comma-separated) | `--allowed-host` | extra hostnames clients use to reach the server (e.g. a Tailscale `*.ts.net` name); this machine's hostname, `.local` name and LAN IP are always allowed |
| `SKYQ_MCP_SSL_CERTFILE` / `SKYQ_MCP_SSL_KEYFILE` | `--ssl-certfile` / `--ssl-keyfile` | off (plain HTTP) |
| `SKYQ_MCP_ALLOW_UNSAFE_KEYS` | `--allow-unsafe-keys` | off (blocks remote-pairing codes 223–239) |
| `SKYQ_MCP_IFACE` | `--iface` | automatic (local IP to search for boxes from, if the machine has several networks) |

Without TLS, the token travels unencrypted on your network.

## Development

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e '.[dev]'
```

```bash
.venv/bin/pytest
```

## License

MIT — see [LICENSE](LICENSE).
