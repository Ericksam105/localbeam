# LocalBeam

LocalBeam is a lightweight, single-use file-transfer service for a local network. A file streams from the sender's browser, through this computer, to the recipient. File contents are never saved by the server.

## Start it

On Windows, double-click `start.cmd`, or open PowerShell in this folder and run:

```powershell
py -3 server.py
```

The terminal shows two addresses:

- `http://localhost:8765` for this computer.
- A named address such as `http://OFFICE-PC:8765`, used in generated sharing links.
- LAN IP addresses such as `http://192.168.1.25:8765` as fallbacks.

The automatic name is the computer's existing network name. If a phone cannot resolve that name, use an IP fallback or configure a name through your router's local DNS/mDNS service. Windows may ask whether Python can communicate on private networks; allow private-network access for LAN sharing.

## Send a file

1. Choose or drop one file into LocalBeam.
2. Create and share the single-use link.
3. Keep the sender page open.
4. The transfer starts when the recipient clicks **Accept and download**.

Links expire after 10 minutes if they have not been opened. A transfer that has begun is allowed to finish.
New links use a compact path such as `/r/Ab3xY7Qp`; the private upload key is separate and is never included in the shared link.

## Admin terminal

Type these commands in the terminal that is running the server:

```text
limit              Show the current maximum
limit 3GB          Change the maximum for new transfers
name               Show the name used in sharing links
name OFFICE-PC     Use an existing LAN host name
name localbeam.local  Use a custom DNS/mDNS name
name auto          Return to this computer's network name
transfers          List current and recent transfers
cancel <id>        Cancel a transfer (an ID prefix is accepted)
urls               Show the local addresses again
stop               Stop the server
```

The default maximum is **2 GB**. A `limit` change is saved in `config.json` and remains in effect after a restart. It applies to new links; links already created keep their originally approved file size.

The `name` command changes the host placed in newly generated links. It does not create a DNS record by itself: custom names must already resolve through the router, a DNS server, mDNS, or device host-file configuration. Local settings are saved in the ignored `config.json`; copy `config.example.json` if you want to prepare settings before the first launch.

## Notes

- Python 3 is the only requirement; there are no packages to install.
- Memory use is bounded to roughly 8 MB per active transfer, plus small server overhead.
- The current version is intended for trusted local networks. It uses ordinary HTTP, not internet-facing TLS or authentication.
- Speed is limited by the sender, receiver, Wi-Fi/LAN, and the host computer because bytes pass through the host.

## Run the tests

```powershell
py -3 -m unittest discover -s tests -v
```
