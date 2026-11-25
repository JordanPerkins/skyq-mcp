# Sky Q capability study

What a Sky Q box can do, how each capability can be reached from a server on the local network,
and what skyq-mcp does with it. The goal is a feature-complete MCP server, and a clear record of
what isn't possible or shouldn't be exposed.

Study date: 2026-11-26. Test box: Sky Q 1TB, ES340 ("Titan"), software Q370.000.09.00.

## How this was researched

1. **Community knowledge:** the endpoints and key codes used by
   [pyskyqremote](https://github.com/RogerSelwyn/skyq_remote) (the library behind Home
   Assistant's Sky Q integration).
2. **The box itself, read-only:** open TCP ports, REST routes on port 9006, the app-status
   WebSocket, UPnP service descriptions on port 49153, and the UPnP content directory.
   Unknown REST routes were probed with GET only. POSTing without parameters is only safe when
   a route needs parameters (a parameterless destructive route would simply run), so unknown
   POST routes were never probed.
3. **What users can do:** Sky's own list of [voice command categories](https://www.sky.com/help/articles/sky-q-voice-control-commands)
   and reviews ([What Hi-Fi](https://www.whathifi.com/advice/sky-q-tips-tricks-and-features),
   [Trusted Reviews](https://www.trustedreviews.com/opinion/sky-q-tips-tricks-secret-features-box-4k-2942515)),
   as a checklist of user-facing features.

## Interfaces on the box

| Port | What it is | Use |
|---|---|---|
| 49160 | Remote-control protocol (TCP) | Every remote key. Works |
| 9006 | REST API (`/as/...`) and WebSocket (`/as/apps/status`) | Box info, channels, recordings, apps. Works |
| 49153 | UPnP: SkyPlay, SkyBook, SkyBrowse, SkyRC, SkyRemote, SkyPairing, BasicManagement | Status reads work; **control actions are locked** (705) |
| 80, 4730 | Recording streams (`/stream/BOOK:<id>`) | Not used |
| 5000 | AirPlay receiver | Not controllable |
| 8008, 8080, 49155 | Internal servers (app icons; `vws`, refuses requests; closes immediately) | Not useful |

Plus Sky's online TV guide, `http://atlantis.epgsky.com/as/schedule/<YYYYMMDD>/<sid>`, with
headers `x-skyott-territory` (GB/DE/IT), `x-skyott-provider: SKY` and
`x-skyott-proposition: SKYQ`. It returns about 7 days of programmes per channel, with event IDs.
One channel per request.

## Capability matrix

Status: **in MCP**; **verified** (works on the test box, not yet in the MCP); **found** (known
endpoint, not yet tested); **locked** (refused by the box); **no API** (keys/menus only, or not
possible).

### Watching

| Capability | How | Status | Notes |
|---|---|---|---|
| Change channel by number or name | Digit keys + checking via UPnP | in MCP | Clears stray digits, retries |
| Channel up/down, all remote keys | Remote protocol | in MCP | |
| Pause / play / rewind / fast-forward | Remote keys | in MCP (`playback`) | UPnP `Pause`/`Play`/`Seek` are locked |
| What's on screen: channel, recording or app | UPnP status + `/as/pvr/details` + app WebSocket | in MCP | |
| Programme on now / next, with synopsis | Sky online guide by channel `sid` | in MCP (`get_now_playing`) | The box doesn't report live programme titles; the guide does |
| TV listings for a channel and day | Sky online guide | in MCP (`tv_listings`, `whats_on`) | ~7 days ahead |
| Find a programme across channels | Sky online guide, one request per channel | in MCP (`find_programme`) | ~10 s for 2 days on first search, then cached |
| Restart a programme from the beginning | No API | no API | Sky Q's restart is an on-screen option |
| Tune directly by URI, seek, set speed | UPnP SkyPlay | locked | Box returns 705 TRANSPORT IS LOCKED |
| Picture-in-picture | `pipCapable` reported | no API | No key found in the code sweep |

### Recordings

| Capability | How | Status | Notes |
|---|---|---|---|
| List recordings and scheduled ones, storage used | `GET /as/pvr`, `/as/pvr/storage` | in MCP | |
| Recording details (title, synopsis, episode) | `GET /as/pvr/details/P<id>` | in MCP (in now playing) | Could be its own tool |
| **Record a programme** | `POST /as/pvr/action/bookrecording?eid=<event id>` | found | Event ID from the online guide |
| **Record a series** | `POST /as/pvr/action/bookseriesrecording?eid=` | found | |
| Series link / unlink an existing recording | `POST /as/pvr/action/serieslink?pvrid=`, `seriesunlink` | found | |
| Delete to the recycle bin / restore | `POST /as/pvr/action/delete?pvrid=`, `undelete` | found | Reversible: the box has a `recycle` folder |
| Keep (stop auto-delete) / unkeep | `POST /as/pvr/action/keep?pvrid=`, `unkeep` | found | |
| Lock / unlock (PIN-protect) | `POST /as/pvr/action/lock?pvrid=`, `unlock` | found | Unlock weakens parental control: see below |
| Resume position | `POST /as/pvr/action/setlastplayedposition?pos=&pvrid=` | found | Niche |
| Erase permanently | `POST /as/pvr/action/erase?pvrid=` | found | Irreversible |
| **Erase every recording** | `DELETE /as/pvr` | found | **Never expose** |
| Book pay-per-view | `POST /as/pvr/action/bookppvrecording?eid=&offerref=` | found | **Spends money: never expose** |
| Record from an exact time | Remote keys / SkyBook | no API | Record key includes the live buffer; stop key doesn't stop; SkyBook won't browse |
| Play a recording | UPnP `SetAVTransportURI` | locked | Possible via Recordings menu (fallback) |
| Record button on live TV | Remote key | in MCP (`playback`, described) | **Records and sets a series link; a second press makes it a single recording** |

### Apps and menus

| Capability | How | Status | Notes |
|---|---|---|---|
| List installed apps | `GET /as/apps` | in MCP | |
| Open / close an app, confirmed | `POST /as/apps/action/launch` / `close` + WebSocket | in MCP | |
| App on screen | WebSocket `/as/apps/status` | in MCP | |
| Kill an app | `POST /as/apps/action/kill` | not available | Box reports "not implemented" |
| Sky's own areas: TV Guide, Recordings, Catch Up TV, On Demand, Best of Sky, Sky Cinema, Sky Store, Sport, Kids, Apps, Audio and Music, Settings, Help, My Sky | Home menu | in MCP (`navigate_to`, unconfirmed routes) | Menu order from the test box; users confirm and save routes |
| Search (text) | Search screen + on-screen keyboard via arrows | no API | Possible in principle, very fragile |
| Voice search | Voice remote only | no API | |
| Settings, accessibility (subtitles, audio description) | Menus; `text` key may toggle subtitles | no API (menu fallback) | |
| Reminders | Shown on bookings (`X_reminderStatus`) | no API found | |

### Box and household

| Capability | How | Status | Notes |
|---|---|---|---|
| Discover boxes, save by hostname, follow IP changes | SSDP + mDNS + UDN check | in MCP | |
| Power on / off (standby) | Remote key + `activeStandby` | in MCP | |
| Model, software, uptime, UHD/HDR | `GET /as/system/information` (allowed fields only) | in MCP | |
| Local time and time zone | `GET /as/system/time` | in MCP (listings) | Listings shown in the box's local time |
| Country and channel region | `GET /as/system/deviceinformation` (`countryCode` only) | in MCP (guide territory) | **Also returns the household token: only `countryCode` is read** |
| Favourite channels | `GET /as/services/favourites` | verified | Empty on the test box |
| Channel logos, programme images | `imageservice.sky.com` URLs | found | Optional |
| Sky Q Mini boxes (multiroom) | Same protocols (`MULTIROOMSTB`) | found | Supported by pyskyqremote; untested here (one box) |
| Volume / mute | TV over HDMI-CEC or IR | not possible | The box doesn't control volume |
| Reboot, network, Bluetooth | No API found | no API | |

### Deliberately excluded

| Capability | Why |
|---|---|
| Parental PIN (`X_NDS_SetUserPIN`, `X_NDS_SubmitPin`), unlocking recordings without the PIN | Would weaken parental controls |
| Account tokens (`X_NDS_GetJWT`/`SetJWT`/`DeleteJWT`), household token | Account credentials |
| Purchases, entitlements, pay-per-view (`X_NDS_StorePurchase`, `bookppvrecording`) | Spends money |
| Pairing (`SkyPairing`, remote-pairing key codes 223–239) | Could unpair the remote; pairing with the box is a separate decision |
| Erase all recordings | Irreversible, no legitimate assistant use |

## Recommended roadmap

**1. TV guide: done (2026-11-26).** Add Sky's online guide:
`get_now_playing` includes the programme on now and next; new `tv_listings(channel, day)` and
`find_programme(query, channels?, days?)`. Use `system/time` for local times and
`deviceinformation` (allowed fields only) for the territory. Cache schedules.

**2. Booking recordings (writes, reversible).** `record_programme(channel + time or event)`,
with `series: bool`, and `cancel_recording(pvr_id)` for scheduled items. Test on the box first
with a programme the user picks.

**3. Managing recordings (reversible).** `delete_recording` (to the recycle bin) and
`restore_recording`, `keep_recording`, `series_link`. `erase_recording` (permanent) only with an
explicit `confirm` argument, or not at all.

**4. Menus and search.** Add the Sky destinations above as unverified routes for users to confirm
and save; consider on-screen keyboard search later.

**5. Multiroom.** Test discovery and control with a Sky Q Mini.

Each step: test on a real box, add the fake box and tests, update the README, re-run the
dependency audit.
